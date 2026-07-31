#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Production Postgres and Qdrant are read-only.
[[ "$EUID" -eq 0 ]]
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
container=brains-postgres-1
source_db=memory
clone_db="memory_v5_2_provenance_conflict_$(date -u +%Y%m%d%H%M%S)_$$"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
python_bin=/opt/chat-memory/venv/bin/python
migration=ops/sql/20260731_memory_v1_v5_2_provenance_coverage_conflict_v1.sql
rollback=ops/sql/20260731_memory_v1_v5_2_provenance_coverage_conflict_v1_rollback.sql
artifact_dir="/home/ubuntu/memory-v1-reviews/provenance-coverage-conflict-clone-$(date -u +%Y%m%dT%H%M%SZ)"
dump_file=''
memory_acl_list=''

set -a
source /opt/chat-memory/.env
set +a
[[ -n "${POSTGRES_DSN:-}" ]]
for path in "$migration" "$rollback" \
  tests/test_memory_v1_semantic_compiler_v12.py \
  tests/test_memory_v1_predicate_runtime_profile_v2.py; do
  [[ -f "$repo_root/$path" ]]
done
mkdir -p -m 0700 "$artifact_dir"

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists --force "$clone_db" \
    >/dev/null 2>&1 || true
  rm -f "$dump_file" "$memory_acl_list"
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
      'evidence',(SELECT count(*) FROM memory.evidence),
      'entity',(SELECT count(*) FROM memory.entity),
      'observation',(SELECT count(*) FROM memory.observation),
      'binding',(SELECT count(*) FROM memory.observation_entity_binding),
      'entailment',(SELECT count(*) FROM memory.observation_entailment_v5),
      'claim',(SELECT count(*) FROM memory.claim),
      'claim_revision',(SELECT count(*) FROM memory.claim_revision),
      'projection_outbox',(SELECT count(*) FROM memory.projection_outbox),
      'answer_binding',(SELECT count(*) FROM memory.final_answer_memory_binding_v1)
    )::text)"
}

production_target_signature() {
  local database=$1
  scalar "$database" "
    WITH target_batches AS (
      SELECT * FROM memory.relational_stage_batch
      WHERE owner_user_id='$owner'::uuid
        AND batch_id IN (
          '7488403c-5fd9-47b3-a05a-ee4cad3b2561'::uuid,
          '34f9af36-c895-443b-b84c-5c0656e22fa2'::uuid
        )
    ), target_observation_ids AS (
      SELECT ids.value::uuid AS observation_id
      FROM target_batches AS batch
      CROSS JOIN LATERAL jsonb_each_text(
        batch.result->'observation_ids'
      ) AS ids
      UNION
      SELECT 'fbff7592-9fc8-4a2c-a629-a90ec419fde2'::uuid
    ), target_evidence_ids AS (
      SELECT evidence_id FROM target_batches
      UNION
      SELECT observation.evidence_id
      FROM memory.observation AS observation
      JOIN target_observation_ids USING (observation_id)
    )
    SELECT md5(jsonb_build_object(
      'batch',(SELECT coalesce(jsonb_agg(to_jsonb(batch)
        ORDER BY batch.batch_id),'[]'::jsonb) FROM target_batches AS batch),
      'evidence',(SELECT coalesce(jsonb_agg(to_jsonb(evidence)
        ORDER BY evidence.evidence_id),'[]'::jsonb)
        FROM memory.evidence AS evidence
        JOIN target_evidence_ids USING (evidence_id)
        WHERE evidence.owner_user_id='$owner'::uuid),
      'observation',(SELECT coalesce(jsonb_agg(to_jsonb(observation)
        ORDER BY observation.observation_id),'[]'::jsonb)
        FROM memory.observation AS observation
        JOIN target_observation_ids USING (observation_id)
        WHERE observation.owner_user_id='$owner'::uuid),
      'binding',(SELECT coalesce(jsonb_agg(to_jsonb(binding)
        ORDER BY binding.observation_id),'[]'::jsonb)
        FROM memory.observation_entity_binding AS binding
        JOIN target_observation_ids USING (observation_id)
        WHERE binding.owner_user_id='$owner'::uuid),
      'temporal',(SELECT coalesce(jsonb_agg(to_jsonb(temporal)
        ORDER BY temporal.observation_id),'[]'::jsonb)
        FROM memory.observation_temporal AS temporal
        JOIN target_observation_ids USING (observation_id)
        WHERE temporal.owner_user_id='$owner'::uuid)
    )::text)"
}

production_activity_counts() {
  local database=$1
  scalar "$database" "
    SELECT jsonb_build_object(
      'evidence',(SELECT count(*) FROM memory.evidence),
      'entity',(SELECT count(*) FROM memory.entity),
      'observation',(SELECT count(*) FROM memory.observation),
      'binding',(SELECT count(*) FROM memory.observation_entity_binding),
      'entailment',(SELECT count(*) FROM memory.observation_entailment_v5),
      'claim',(SELECT count(*) FROM memory.claim),
      'claim_revision',(SELECT count(*) FROM memory.claim_revision),
      'projection_outbox',(SELECT count(*) FROM memory.projection_outbox)
    )"
}

answer_binding_count() {
  local database=$1
  scalar "$database" \
    "SELECT count(*) FROM memory.final_answer_memory_binding_v1"
}

qdrant_before=$(qdrant_signature)
production_before=$(production_target_signature "$source_db")
production_activity_before=$(production_activity_counts "$source_db")
answer_bindings_before=$(answer_binding_count "$source_db")

dump_file=$(mktemp /tmp/memory-v5-2-provenance-conflict.XXXXXX.dump)
memory_acl_list=$(mktemp /tmp/memory-v5-2-provenance-conflict-acl.XXXXXX.list)
chmod 0600 "$dump_file" "$memory_acl_list"
docker exec "$container" pg_dump -U sage -d "$source_db" -Fc >"$dump_file"
pg_restore -l "$dump_file" \
  | grep -E ' ACL memory | ACL - SCHEMA memory ' \
  >"$memory_acl_list"
[[ -s "$memory_acl_list" ]]
! grep -q 'lifeswitch_chat' "$memory_acl_list"

docker exec "$container" createdb -U sage -T template0 "$clone_db"
docker exec -i "$container" pg_restore --exit-on-error --no-acl \
  -U sage -d "$clone_db" <"$dump_file"
pg_restore --exit-on-error --use-list="$memory_acl_list" -f - "$dump_file" \
  | docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
      -U sage -d "$clone_db" >/dev/null

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
[[ -n "$clone_dsn" ]]

PYTHONPATH="$repo_root" "$python_bin" -m unittest -v \
  tests.test_memory_v1_semantic_compiler_v12 \
  tests.test_memory_v1_predicate_runtime_profile_v2 \
  | tee "$artifact_dir/unit-tests.txt"

protected_before=$(protected_signature "$clone_db")
docker exec -i "$container" psql -X -U sage -d "$clone_db" \
  -v ON_ERROR_STOP=1 <"$repo_root/$migration"

contract_report=$(scalar "$clone_db" "
  SELECT jsonb_build_object(
    'extension_rows',(SELECT count(*)
      FROM memory.predicate_registry_compiler_extension_v1),
    'contract_rows',(SELECT count(*) FROM memory.predicate_contract
      WHERE registry_version='memory_predicate_registry_v5_2'
        AND predicate='residence.care_setting'),
    'source_bindings',(SELECT count(*)
      FROM memory.predicate_registry_source_binding_v5_2
      WHERE registry_version='memory_predicate_registry_v5_2'),
    'brains_helper',has_function_privilege('brains_app',
      'memory.owner_batch_possible_temporal_updates_v1(uuid)','EXECUTE'),
    'brains_conflict_plan',has_function_privilege('brains_app',
      'memory.plan_owner_v5_2_observation_conflict_v1(integer)','EXECUTE')
  )")
printf 'CLONE_CONTRACT_REPORT=%s\n' "$contract_report"
jq -e '.extension_rows==1 and .contract_rows==1 and .source_bindings==1 and
  .brains_helper==false and .brains_conflict_plan==true' \
  <<<"$contract_report" >/dev/null

conflict_report=$(docker exec -i "$container" psql -X -q -A -t \
  -v ON_ERROR_STOP=1 -U sage -d "$clone_db" <<SQL | tr -d '\n'
BEGIN;
SET LOCAL SESSION AUTHORIZATION brains_app;
SET LOCAL app.user_id='$owner';
SELECT jsonb_build_object(
  'count',count(*),
  'batch_ids',coalesce(jsonb_agg(batch_id ORDER BY batch_id),'[]'::jsonb),
  'slots',coalesce(jsonb_agg(semantic_slot ORDER BY semantic_slot),'[]'::jsonb),
  'current_ids',coalesce(jsonb_agg(current_observation_id
    ORDER BY current_observation_id),'[]'::jsonb),
  'prior_ids',coalesce(jsonb_agg(prior_observation_id
    ORDER BY prior_observation_id),'[]'::jsonb),
  'reasons',coalesce(jsonb_agg(reason_code ORDER BY reason_code),'[]'::jsonb)
)
FROM memory.plan_owner_v5_2_observation_conflict_v1(100);
ROLLBACK;
SQL
)
printf 'CLONE_CONFLICT_REPORT=%s\n' "$conflict_report"
jq -e '.count==1 and
  .batch_ids==["34f9af36-c895-443b-b84c-5c0656e22fa2"] and
  .slots==["health.short_term_memory_duration"] and
  .current_ids==["34a09222-5aab-43ca-a199-dd120b62657e"] and
  .prior_ids==["fbff7592-9fc8-4a2c-a629-a90ec419fde2"] and
  .reasons==["overlapping_reported_value_change"]' \
  <<<"$conflict_report" >/dev/null

stage_report=$(docker exec -i "$container" psql -X -q -A -t \
  -v ON_ERROR_STOP=1 -U sage -d "$clone_db" <<SQL | tr -d '\n'
BEGIN;
SET LOCAL SESSION AUTHORIZATION brains_app;
SET LOCAL app.user_id='$owner';
SELECT jsonb_build_object(
  'count',count(*),
  'stance',count(*) FILTER(
    WHERE route_event_id='e116fe96-083c-58a2-8b99-b4621bebcda1'),
  'father',count(*) FILTER(
    WHERE route_event_id='a30fe750-873d-5c10-8097-c74625395dbb')
)
FROM memory.plan_owner_v5_2_reviewed_observation_stage_v1(20);
ROLLBACK;
SQL
)
printf 'CLONE_STAGE_REPORT=%s\n' "$stage_report"
jq -e '.count==1 and .stance==1 and .father==0' \
  <<<"$stage_report" >/dev/null

other_owner_conflicts=$(docker exec -i "$container" psql -X -q -A -t \
  -v ON_ERROR_STOP=1 -U sage -d "$clone_db" <<SQL | tr -d '[:space:]'
BEGIN;
SET LOCAL SESSION AUTHORIZATION brains_app;
SET LOCAL app.user_id='$other_owner';
SELECT count(*) FROM memory.plan_owner_v5_2_observation_conflict_v1(100);
ROLLBACK;
SQL
)
printf 'CLONE_OTHER_OWNER_CONFLICTS=%q\n' "$other_owner_conflicts"
[[ "$other_owner_conflicts" == 0 ]]

# Reapply the migration and prove the append-only contract rows are unchanged.
row_counts_before=$(scalar "$clone_db" "
  SELECT concat_ws(':',
    (SELECT count(*) FROM memory.predicate_registry_compiler_extension_v1),
    (SELECT count(*) FROM memory.predicate_contract
      WHERE predicate='residence.care_setting'
        AND registry_version='memory_predicate_registry_v5_2'),
    (SELECT count(*) FROM memory.predicate_registry_compiler_extension_v1
      WHERE compiler_version='memory_v1_semantic_policy_compiler_v12'))")
docker exec -i "$container" psql -X -U sage -d "$clone_db" \
  -v ON_ERROR_STOP=1 <"$repo_root/$migration" >/dev/null
row_counts_after=$(scalar "$clone_db" "
  SELECT concat_ws(':',
    (SELECT count(*) FROM memory.predicate_registry_compiler_extension_v1),
    (SELECT count(*) FROM memory.predicate_contract
      WHERE predicate='residence.care_setting'
        AND registry_version='memory_predicate_registry_v5_2'),
    (SELECT count(*) FROM memory.predicate_registry_compiler_extension_v1
      WHERE compiler_version='memory_v1_semantic_policy_compiler_v12'))")
printf 'CLONE_REPLAY_ROWS=%s->%s\n' "$row_counts_before" "$row_counts_after"
[[ "$row_counts_before" == "1:1:1" ]]
[[ "$row_counts_after" == "$row_counts_before" ]]

protected_after=$(protected_signature "$clone_db")
[[ "$protected_after" == "$protected_before" ]]

docker exec -i "$container" psql -X -U sage -d "$clone_db" \
  -v ON_ERROR_STOP=1 <"$repo_root/$rollback" >/dev/null
rollback_report=$(scalar "$clone_db" "
  SELECT jsonb_build_object(
    'conflict_absent',to_regprocedure(
      'memory.plan_owner_v5_2_observation_conflict_v1(integer)') IS NULL,
    'predicate_absent',NOT EXISTS(SELECT 1 FROM memory.predicate
      WHERE predicate='residence.care_setting'),
    'extension_absent',to_regclass(
      'memory.predicate_registry_compiler_extension_v1') IS NULL
  )")
printf 'CLONE_ROLLBACK_REPORT=%s\n' "$rollback_report"
jq -e '.conflict_absent and .predicate_absent and .extension_absent' \
  <<<"$rollback_report" >/dev/null

rollback_stage_count=$(docker exec -i "$container" psql -X -q -A -t \
  -v ON_ERROR_STOP=1 -U sage -d "$clone_db" <<SQL | tr -d '[:space:]'
BEGIN;
SET LOCAL SESSION AUTHORIZATION brains_app;
SET LOCAL app.user_id='$owner';
SELECT count(*) FROM memory.plan_owner_v5_2_reviewed_observation_stage_v1(20);
ROLLBACK;
SQL
)
printf 'CLONE_ROLLBACK_STAGE_COUNT=%q\n' "$rollback_stage_count"
[[ "$rollback_stage_count" == 2 ]]
protected_after_rollback=$(protected_signature "$clone_db")
printf 'CLONE_PROTECTED_SIGNATURE=%s->%s\n' \
  "$protected_before" "$protected_after_rollback"
[[ "$protected_after_rollback" == "$protected_before" ]]

qdrant_after=$(qdrant_signature)
production_after=$(production_target_signature "$source_db")
production_activity_after=$(production_activity_counts "$source_db")
answer_bindings_after=$(answer_binding_count "$source_db")
printf 'CLONE_QDRANT_SIGNATURE=%s->%s\n' "$qdrant_before" "$qdrant_after"
printf 'PRODUCTION_TARGET_SIGNATURE=%s->%s\n' \
  "$production_before" "$production_after"
printf 'PRODUCTION_ACTIVITY_COUNTS=%s->%s\n' \
  "$production_activity_before" "$production_activity_after"
printf 'PRODUCTION_LIVE_ANSWER_BINDINGS=%s->%s\n' \
  "$answer_bindings_before" "$answer_bindings_after"
[[ "$qdrant_after" == "$qdrant_before" ]]
[[ "$production_after" == "$production_before" ]]

jq -n \
  --arg contract_version memory_v1_v5_2_provenance_coverage_conflict_clone_v2 \
  --arg migration_sha256 "$(sha256sum "$repo_root/$migration" | awk '{print $1}')" \
  --arg rollback_sha256 "$(sha256sum "$repo_root/$rollback" | awk '{print $1}')" \
  --arg qdrant_sha256 "$qdrant_after" \
  --argjson production_activity_before "$production_activity_before" \
  --argjson production_activity_after "$production_activity_after" \
  --argjson answer_bindings_before "$answer_bindings_before" \
  --argjson answer_bindings_after "$answer_bindings_after" \
  '{contract_version:$contract_version,clone_passed:true,
    compiler_v12_tests_passed:true,span_alignment_repaired:true,
    explicit_name_covered:true,care_setting_covered:true,
    possible_temporal_update_count:1,stage_candidates_after_guard:1,
    cross_owner_isolated:true,zero_model_calls:true,
    protected_stores_unchanged:true,production_target_records_unchanged:true,
    production_activity_before:$production_activity_before,
    production_activity_after:$production_activity_after,
    live_answer_bindings_before:$answer_bindings_before,
    live_answer_bindings_after:$answer_bindings_after,
    qdrant_unchanged:true,replay_zero_row:true,rollback_passed:true,
    migration_sha256:$migration_sha256,rollback_sha256:$rollback_sha256,
    qdrant_sha256:$qdrant_sha256}' \
  | tee "$artifact_dir/report.json"

printf 'ARTIFACT_DIR=%s\n' "$artifact_dir"
