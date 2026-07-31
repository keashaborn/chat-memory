#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores current production into a disposable clone and
# proves the generic reviewed-observation novelty guard. Production Postgres
# and Qdrant remain read-only.

[[ "$EUID" -eq 0 ]]
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
container=brains-postgres-1
source_db=memory
clone_db="memory_v5_2_reviewed_stage_$(date -u +%Y%m%d%H%M%S)_$$"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
python_bin=/opt/chat-memory/venv/bin/python
migration=ops/sql/20260731_memory_v1_v5_2_reviewed_stage_novelty_guard_v1.sql
rollback=ops/sql/20260731_memory_v1_v5_2_reviewed_stage_novelty_guard_v1_rollback.sql
worker=scripts/memory_v1_v5_2_reviewed_observation_stage.py
unit_test=tests/test_memory_v1_v5_2_reviewed_observation_stage.py
artifact_dir="/home/ubuntu/memory-v1-reviews/reviewed-stage-novelty-guard-clone-$(date -u +%Y%m%dT%H%M%SZ)"

set -a
source /opt/chat-memory/.env
set +a
[[ -n "${POSTGRES_DSN:-}" ]]
for path in "$migration" "$rollback" "$worker" "$unit_test"; do
  [[ -f "$repo_root/$path" ]]
done
mkdir -p -m 0700 "$artifact_dir"

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists --force "$clone_db" \
    >/dev/null 2>&1 || true
}
trap cleanup EXIT

scalar() {
  local database=$1
  local sql=$2
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$sql" | sed -n '1p'
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

protected_signature() {
  local database=$1
  scalar "$database" "
    SELECT md5(jsonb_build_object(
      'entity',(SELECT count(*) FROM memory.entity),
      'observation',(SELECT count(*) FROM memory.observation),
      'binding',(SELECT count(*) FROM memory.observation_entity_binding),
      'entailment',(SELECT count(*) FROM memory.observation_entailment_v5),
      'claim',(SELECT count(*) FROM memory.claim),
      'claim_revision',(SELECT count(*) FROM memory.claim_revision),
      'claim_observation',(SELECT count(*) FROM memory.claim_observation),
      'projection_plan',(SELECT count(*) FROM memory.projection_plan),
      'projection_outbox',(SELECT count(*) FROM memory.projection_outbox),
      'projection_dispatch',(SELECT count(*) FROM memory.projection_dispatch_v5),
      'answer_binding',(SELECT count(*) FROM memory.final_answer_memory_binding_v1),
      'retrieval_trace',(SELECT count(*) FROM memory.retrieval_trace),
      'retrieval_trace_item',(SELECT count(*) FROM memory.retrieval_trace_item)
    )::text)"
}

qdrant_before=$(qdrant_signature)
production_stage_before=$(scalar "$source_db" "
  SELECT concat_ws(':',
    (SELECT count(*) FROM memory.v5_local_packet_stage_admission
      WHERE owner_user_id='$owner'),
    (SELECT count(*) FROM memory.v5_2_reviewed_observation_stage_admission
      WHERE owner_user_id='$owner'))")

docker exec "$container" createdb -U sage -T template0 "$clone_db"
docker exec "$container" pg_dump -U sage -d "$source_db" -Fc \
  | docker exec -i "$container" pg_restore -U sage -d "$clone_db"

clone_dsn=$(SOURCE_DSN="$POSTGRES_DSN" CLONE_DB="$clone_db" python3 - <<'PY'
import os
from urllib.parse import urlsplit, urlunsplit

value = urlsplit(os.environ["SOURCE_DSN"])
print(urlunsplit((
    value.scheme,
    value.netloc,
    "/" + os.environ["CLONE_DB"],
    value.query,
    value.fragment,
)))
PY
)

PYTHONPATH="$repo_root" "$python_bin" "$repo_root/$unit_test"
docker exec -i "$container" psql -X -U sage -d "$clone_db" \
  -v ON_ERROR_STOP=1 <"$repo_root/$migration"

acl_report=$(scalar "$clone_db" "
  SELECT jsonb_build_object(
    'successor_owner',(SELECT proowner::regrole::text FROM pg_proc
      WHERE oid='memory.v5_2_resolution_successor_source_v1(uuid)'::regprocedure),
    'feedback_owner',(SELECT proowner::regrole::text FROM pg_proc
      WHERE oid='memory.owner_packet_stage_eligible_v1(uuid)'::regprocedure),
    'brains_successor',has_function_privilege('brains_app',
      'memory.v5_2_resolution_successor_source_v1(uuid)','EXECUTE'),
    'brains_feedback',has_function_privilege('brains_app',
      'memory.owner_packet_stage_eligible_v1(uuid)','EXECUTE'),
    'maintainer_successor',has_function_privilege(
      'memory_v5_2_reviewed_observation_stage_maintainer',
      'memory.v5_2_resolution_successor_source_v1(uuid)','EXECUTE'),
    'maintainer_feedback',has_function_privilege(
      'memory_v5_2_reviewed_observation_stage_maintainer',
      'memory.owner_packet_stage_eligible_v1(uuid)','EXECUTE'),
    'novelty_owner',(SELECT proowner::regrole::text FROM pg_proc
      WHERE oid='memory.owner_batch_has_unentailed_observation_v1(uuid)'::regprocedure),
    'brains_novelty',has_function_privilege('brains_app',
      'memory.owner_batch_has_unentailed_observation_v1(uuid)','EXECUTE'),
    'maintainer_novelty',has_function_privilege(
      'memory_v5_2_reviewed_observation_stage_maintainer',
      'memory.owner_batch_has_unentailed_observation_v1(uuid)','EXECUTE')
  )")
printf 'CLONE_ACL_REPORT=%s\n' "$acl_report"
jq -e '.successor_owner=="memory_v5_writer" and
  .feedback_owner=="sage" and .brains_successor==false and
  .brains_feedback==false and .maintainer_successor==true and
  .maintainer_feedback==true and .novelty_owner=="memory_v5_writer" and
  .brains_novelty==false and .maintainer_novelty==true' \
  <<<"$acl_report" >/dev/null

protected_before=$(protected_signature "$clone_db")
stage_before=$(scalar "$clone_db" "
  SELECT concat_ws(':',
    (SELECT count(*) FROM memory.v5_local_packet_stage_admission
      WHERE owner_user_id='$owner'),
    (SELECT count(*) FROM memory.v5_2_reviewed_observation_stage_admission
      WHERE owner_user_id='$owner'))")

dry_json="$artifact_dir/dry.json"
apply_json="$artifact_dir/apply.json"
replay_json="$artifact_dir/replay.json"

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  "$python_bin" "$repo_root/$worker" \
  --owner-user-id "$owner" --max-records 20 >"$dry_json"
jq -e '.selected_records == 2 and .database_rows_created == 0 and
  .local_model_calls == 0 and .external_model_calls == 0 and
  .claims == 0 and .qdrant_writes == 0 and .prompt_influence == 0' \
  "$dry_json" >/dev/null

novelty_report=$(docker exec -i "$container" psql -X -q -A -t \
  -v ON_ERROR_STOP=1 -U sage -d "$clone_db" <<SQL | tr -d '\n'
BEGIN;
SET LOCAL SESSION AUTHORIZATION brains_app;
SET LOCAL app.user_id='$owner';
SELECT jsonb_build_object(
  'identity_already_entailed',count(*) FILTER (
    WHERE route_event_id='4736f9e6-552e-5e3f-a57d-dd8736fcb2e2'
  ),
  'stance_unentailed',count(*) FILTER (
    WHERE route_event_id='e116fe96-083c-58a2-8b99-b4621bebcda1'
  ),
  'father_unentailed',count(*) FILTER (
    WHERE route_event_id='a30fe750-873d-5c10-8097-c74625395dbb'
  ),
  'total',count(*)
)
FROM memory.plan_owner_v5_2_reviewed_observation_stage_v1(20);
ROLLBACK;
SQL
)
printf 'CLONE_NOVELTY_REPORT=%s\n' "$novelty_report"
jq -e '.identity_already_entailed==0 and .stance_unentailed==1 and
  .father_unentailed==1 and .total==2' <<<"$novelty_report" >/dev/null

CLONE_DSN="$clone_dsn" OWNER="$owner" OTHER_OWNER="$other_owner" \
REPO_ROOT="$repo_root" PYTHONPATH="$repo_root" "$python_bin" - <<'PY'
import asyncio
import os
import uuid

import asyncpg

from scripts.memory_v1_v5_2_reviewed_observation_stage import (
    POLICIES,
    operation_ids,
)


async def main() -> None:
    conn = await asyncpg.connect(os.environ["CLONE_DSN"], ssl=False)
    try:
        owner = uuid.UUID(os.environ["OWNER"])
        other = uuid.UUID(os.environ["OTHER_OWNER"])
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
            target = await conn.fetchrow(
                "SELECT * FROM memory.plan_owner_v5_2_reviewed_observation_stage_v1(20) LIMIT 1"
            )
        if target is None:
            raise RuntimeError("owner plan unexpectedly empty")
        target = dict(target)
        admission_id, operation_id = operation_ids(owner, target)
        values = (
            admission_id,
            operation_id,
            target["route_event_id"],
            target["atom_apply_id"],
            target["batch_id"],
            target["stage_manifest_sha256"],
            target["resolution_state_sha256"],
            target["observation_state_sha256"],
            POLICIES[target["source_kind"]],
        )
        try:
            async with conn.transaction(isolation="serializable"):
                await conn.execute("SELECT set_config('app.user_id',$1,true)", str(other))
                await conn.fetchrow(
                    "SELECT * FROM memory.register_owner_v5_2_reviewed_observation_stage_v1("
                    "$1,$2,$3,$4,$5,$6,$7,$8,$9)",
                    *values,
                )
        except asyncpg.PostgresError as exc:
            if exc.sqlstate not in {"42501", "P0002"}:
                raise
        else:
            raise RuntimeError("cross-owner stage registration was accepted")
    finally:
        await conn.close()


asyncio.run(main())
PY

MEMORY_V1_V5_2_REVIEWED_OBSERVATION_STAGE_APPLY=memory_v1_v5_2_reviewed_observation_stage_apply_v1 \
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  "$python_bin" "$repo_root/$worker" \
  --owner-user-id "$owner" --max-records 20 --apply >"$apply_json"
jq -e '.selected_records == 2 and
  .outcome == "reviewed_observations_admitted" and
  .database_rows_created == 4 and .zero_write_replay_proved == true and
  .write_counts.packet_stage_admission == 2 and
  .write_counts.reviewed_stage_admission == 2 and
  .claims == 0 and .qdrant_writes == 0 and .prompt_influence == 0' \
  "$apply_json" >/dev/null

MEMORY_V1_V5_2_REVIEWED_OBSERVATION_STAGE_APPLY=memory_v1_v5_2_reviewed_observation_stage_apply_v1 \
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  "$python_bin" "$repo_root/$worker" \
  --owner-user-id "$owner" --max-records 20 --apply >"$replay_json"
jq -e '.selected_records == 0 and .outcome == "no_work" and
  .database_rows_created == 0 and .zero_write_replay_proved == true' \
  "$replay_json" >/dev/null

stage_after=$(scalar "$clone_db" "
  SELECT concat_ws(':',
    (SELECT count(*) FROM memory.v5_local_packet_stage_admission
      WHERE owner_user_id='$owner'),
    (SELECT count(*) FROM memory.v5_2_reviewed_observation_stage_admission
      WHERE owner_user_id='$owner'))")
IFS=: read -r packet_before reviewed_before <<<"$stage_before"
IFS=: read -r packet_after reviewed_after <<<"$stage_after"
printf 'CLONE_STAGE_COUNTS=%s->%s\n' "$stage_before" "$stage_after"
[[ $((packet_after-packet_before)) -eq 2 ]]
[[ $((reviewed_after-reviewed_before)) -eq 2 ]]
protected_after=$(protected_signature "$clone_db")
printf 'CLONE_PROTECTED_SIGNATURE=%s->%s\n' "$protected_before" "$protected_after"
[[ "$protected_after" == "$protected_before" ]]

entailment_plan=$(docker exec -i "$container" psql -X -q -A -t \
  -v ON_ERROR_STOP=1 -U sage -d "$clone_db" <<SQL | tr -d '\n'
BEGIN;
SET LOCAL SESSION AUTHORIZATION brains_app;
SET LOCAL app.user_id='$owner';
SELECT jsonb_build_object(
  'count',count(*),
  'items',coalesce(jsonb_agg(jsonb_build_object(
    'predicate',planned.observation_payload->>'predicate',
    'projection_class',planned.observation_payload->>'projection_class'
  ) ORDER BY planned.observation_created_at,planned.observation_id),'[]'::jsonb)
)
FROM memory.plan_owner_v5_local_entailment_v1(20) AS planned;
ROLLBACK;
SQL
)
printf 'CLONE_ENTAILMENT_PLAN=%s\n' "$entailment_plan"
[[ "$(jq -r '.count' <<<"$entailment_plan")" == 3 ]]

observation_gate_report=$(docker exec -i "$container" psql -X -q -A -t \
  -v ON_ERROR_STOP=1 -U sage -d "$clone_db" <<SQL | tr -d '\n'
BEGIN;
WITH recent_stage AS (
  SELECT stage.admission_id,stage.owner_user_id,stage.decision,
    stage.created_at,reviewed.batch_id
  FROM memory.v5_local_packet_stage_admission AS stage
  JOIN memory.v5_2_reviewed_observation_stage_admission AS reviewed
    ON reviewed.owner_user_id=stage.owner_user_id
   AND reviewed.admission_id=stage.admission_id
  WHERE stage.owner_user_id='$owner'::uuid
    AND stage.created_at>=statement_timestamp()-interval '10 minutes'
    AND stage.decision IN (
      'v5_2_atom_reviewed_stage','v5_2_reviewed_route_stage'
    )
), rows AS (
  SELECT stage.admission_id,observation.observation_id,
    observation.predicate,observation.projection_class::text,
    evidence.status='active' AS evidence_active,
    evidence.content IS NOT NULL
      AND memory.v5_digest_text(evidence.content)=evidence.content_sha256
      AS evidence_valid,
    EXISTS (
      SELECT 1 FROM memory.entity_mention AS mention
      WHERE mention.owner_user_id=observation.owner_user_id
        AND mention.evidence_id=observation.evidence_id
        AND mention.mention_id=observation.subject_mention_id
    ) AS subject_present,
    EXISTS (
      SELECT 1 FROM memory.observation_entity_binding AS binding
      WHERE binding.owner_user_id=observation.owner_user_id
        AND binding.observation_id=observation.observation_id
    ) AS binding_present,
    EXISTS (
      SELECT 1 FROM memory.observation_entailment_v5 AS prior
      WHERE prior.owner_user_id=observation.owner_user_id
        AND prior.observation_id=observation.observation_id
        AND prior.policy_version='memory_v1_predicate_entailment_v5_1'
    ) AS prior_entailment,
    EXISTS (
      SELECT 1 FROM memory.v5_local_entailment_assessment AS prior
      WHERE prior.owner_user_id=observation.owner_user_id
        AND prior.observation_id=observation.observation_id
        AND prior.policy_version='memory_v1_v5_local_entailment_policy_v1'
    ) AS prior_assessment
  FROM recent_stage AS stage
  JOIN memory.relational_stage_batch AS batch
    ON batch.owner_user_id=stage.owner_user_id
   AND batch.batch_id=stage.batch_id
  JOIN memory.observation AS observation
    ON observation.owner_user_id=batch.owner_user_id
   AND observation.evidence_id=batch.evidence_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=observation.owner_user_id
   AND evidence.evidence_id=observation.evidence_id
)
SELECT jsonb_build_object(
  'count',count(*),
  'items',coalesce(jsonb_agg(jsonb_build_object(
    'observation_sha256',memory.v5_digest_text(observation_id::text),
    'predicate',predicate,
    'projection_class',projection_class,
    'evidence_active',evidence_active,
    'evidence_valid',evidence_valid,
    'subject_present',subject_present,
    'binding_present',binding_present,
    'prior_entailment',prior_entailment,
    'prior_assessment',prior_assessment
  ) ORDER BY predicate,observation_id),'[]'::jsonb)
)
FROM rows;
ROLLBACK;
SQL
)
printf 'CLONE_OBSERVATION_GATES=%s\n' "$observation_gate_report"
[[ "$(jq -r '.count' <<<"$observation_gate_report")" == 3 ]]

# Prove the seven unsuperseded owner-rejected packets are absent from the
# current planner rather than repeatedly poisoning the writer.
rejected_planned=$(docker exec -i "$container" psql -X -q -A -t -v ON_ERROR_STOP=1 \
  -U sage -d "$clone_db" <<SQL | tr -d '[:space:]'
BEGIN;
SET LOCAL SESSION AUTHORIZATION brains_app;
SET LOCAL app.user_id='$owner';
SELECT count(*)
FROM memory.plan_owner_v5_2_reviewed_observation_stage_v1(20);
ROLLBACK;
SQL
)
printf 'CLONE_REMAINING_PLANNER_COUNT=%q\n' "$rejected_planned"
[[ "$rejected_planned" == 0 ]]

docker exec -i "$container" psql -X -U sage -d "$clone_db" \
  -v ON_ERROR_STOP=1 <"$repo_root/$rollback"
protected_rollback=$(protected_signature "$clone_db")
helper_absence=$(scalar "$clone_db" "SELECT (to_regprocedure('memory.owner_batch_has_unentailed_observation_v1(uuid)') IS NULL)::text")
base_helpers_present=$(scalar "$clone_db" "SELECT (to_regprocedure('memory.v5_2_resolution_successor_source_v1(uuid)') IS NOT NULL AND to_regprocedure('memory.owner_packet_stage_eligible_v1(uuid)') IS NOT NULL)::text")
printf 'CLONE_ROLLBACK_PROTECTED_SIGNATURE=%s\n' "$protected_rollback"
printf 'CLONE_ROLLBACK_HELPERS_ABSENT=%q\n' "$helper_absence"
printf 'CLONE_ROLLBACK_BASE_HELPERS_PRESENT=%q\n' "$base_helpers_present"
[[ "$protected_rollback" == "$protected_before" ]]
[[ "$helper_absence" == true ]]
[[ "$base_helpers_present" == true ]]

qdrant_after=$(qdrant_signature)
production_stage_after=$(scalar "$source_db" "
  SELECT concat_ws(':',
    (SELECT count(*) FROM memory.v5_local_packet_stage_admission
      WHERE owner_user_id='$owner'),
    (SELECT count(*) FROM memory.v5_2_reviewed_observation_stage_admission
      WHERE owner_user_id='$owner'))")
printf 'QDRANT_SIGNATURE=%s->%s\n' "$qdrant_before" "$qdrant_after"
printf 'PRODUCTION_STAGE_COUNTS=%s->%s\n' "$production_stage_before" "$production_stage_after"
[[ "$qdrant_after" == "$qdrant_before" ]]
[[ "$production_stage_after" == "$production_stage_before" ]]

jq -n \
  --arg contract_version memory_v1_v5_2_reviewed_stage_novelty_guard_clone_report_v1 \
  --arg migration_sha256 "$(sha256sum "$repo_root/$migration" | awk '{print $1}')" \
  --arg rollback_sha256 "$(sha256sum "$repo_root/$rollback" | awk '{print $1}')" \
  --arg qdrant_sha256 "$qdrant_after" \
  --argjson selected 2 \
  --argjson rows 4 \
  --argjson entailment_ready 3 \
  '{contract_version:$contract_version,clone_passed:true,
    selected_records:$selected,database_rows_created:$rows,
    entailment_ready_observations:$entailment_ready,
    zero_write_replay:true,cross_owner_rejected:true,
    already_entailed_batches_excluded:true,
    owner_rejected_packets_excluded:true,protected_stores_unchanged:true,
    production_unchanged:true,qdrant_unchanged:true,
    acl_verified:true,
    model_calls:0,claims:0,prompt_influence:0,
    migration_sha256:$migration_sha256,rollback_sha256:$rollback_sha256,
    qdrant_sha256:$qdrant_sha256}' \
  | tee "$artifact_dir/report.json"

printf 'ARTIFACT_DIR=%s\n' "$artifact_dir"
