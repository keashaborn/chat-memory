#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Routes one exact V5.2 packet and exercises controlled
# atom-level stage admission on a disposable production clone. Production,
# Qdrant, claims, retrieval, and prompts remain unchanged.

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

container=brains-postgres-1
production=memory
clone="memory_v5_2_evidence_context_admission_${$}"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
packet=25561cba-6891-51b6-94e9-c4a0db8eb031
evidence=049205b4-9a6c-5e1a-bb8f-2ab9f05f8964
packet_sha=715c9ffc4d2e8080b18785ee22dc4532ca40d5cade7d360174cbef5371e19981
proposal_sha=b2e1fe4ed0524317ad6c52a582b1f618a849a51f45a17babdf5cb7aec56c3825
manifest=manifests/memory_v1_v5_2_evidence_context_atom_admission_20260726.json
router=scripts/memory_v1_v5_2_exact_review_route.py
admission=scripts/memory_v1_v5_2_atom_admission_apply_v2.py
migration=ops/sql/20260725_memory_v1_v5_2_router_zero_call_review_compat.sql
python_bin=/opt/chat-memory/venv/bin/python

backup=$(mktemp /tmp/memory-v5-2-evidence-context-admission.XXXXXX.dump)
protected_before=$(mktemp /tmp/memory-v5-2-evidence-context-admission.XXXXXX.before)
protected_after=$(mktemp /tmp/memory-v5-2-evidence-context-admission.XXXXXX.after)
route_output=$(mktemp /tmp/memory-v5-2-evidence-context-admission.XXXXXX.route)
route_replay=$(mktemp /tmp/memory-v5-2-evidence-context-admission.XXXXXX.route-replay)
isolation_output=$(mktemp /tmp/memory-v5-2-evidence-context-admission.XXXXXX.isolation)
plan_output=$(mktemp /tmp/memory-v5-2-evidence-context-admission.XXXXXX.plan)
preflight_output=$(mktemp /tmp/memory-v5-2-evidence-context-admission.XXXXXX.preflight)
apply_output=$(mktemp /tmp/memory-v5-2-evidence-context-admission.XXXXXX.apply)
replay_output=$(mktemp /tmp/memory-v5-2-evidence-context-admission.XXXXXX.replay)
review_root=$(mktemp -d /tmp/memory-v5-2-evidence-context-admission.XXXXXX.reviews)
chmod 0600 "$backup" "$protected_before" "$protected_after" \
  "$route_output" "$route_replay" "$isolation_output" "$plan_output" \
  "$preflight_output" "$apply_output" "$replay_output"
chmod 0700 "$review_root"
phase=initialization

cleanup() {
  rc=$?
  trap - EXIT
  if [[ "$rc" -ne 0 ]]; then
    printf 'memory_v1_v5_2_evidence_context_route_admission_clone: FAIL phase=%s\n' \
      "$phase" >&2
    for output in "$route_output" "$preflight_output" "$apply_output" \
      "$replay_output"; do
      if [[ -s "$output" ]]; then
        jq -c '{
          outcome,mode,apply,plans,results,write_counts,persistent_writes,
          zero_write_replay_proved,external_model_calls
        }' "$output" >&2 || true
      fi
    done
  fi
  docker exec "$container" dropdb -U sage --if-exists "$clone" \
    >/dev/null 2>&1 || true
  rm -f "$backup" "$protected_before" "$protected_after" \
    "$route_output" "$route_replay" "$isolation_output" "$plan_output" \
    "$preflight_output" "$apply_output" "$replay_output"
  rm -rf "$review_root"
  exit "$rc"
}
trap cleanup EXIT

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

capture_protected() {
  local database=$1 output=$2 table signature
  : >"$output"
  for table in \
    v5_2_local_packet_route_event \
    v5_2_atom_admission_proposal \
    v5_2_atom_admission_review \
    v5_2_atom_admission_apply \
    v5_2_atom_admission_operation \
    relational_stage_batch \
    entity_mention \
    observation \
    claim; do
    signature=$(docker exec "$container" psql -U sage -d "$database" -X -Atqc "
      SELECT count(*)::text || ':' ||
        encode(public.digest(convert_to(coalesce(string_agg(
          to_jsonb(value)::text,E'\\n' ORDER BY to_jsonb(value)::text
        ),''),'UTF8'),'sha256'),'hex')
      FROM memory.$table AS value
    ")
    printf '%s\t%s\n' "$table" "$signature" >>"$output"
  done
}

set -a
source /opt/chat-memory/.env
set +a

[[ -z "$(git status --porcelain)" ]]
[[ -x "$python_bin" && -x "$router" && -x "$admission" ]]
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(curl -sS -o /dev/null -w '%{http_code}' \
  http://127.0.0.1:8088/docs)" == 200 ]]
"$python_bin" -m py_compile "$router" "$admission"
PYTHONPATH="$repo_root" "$python_bin" -m unittest \
  tests/test_memory_v1_v5_2_exact_review_route.py \
  tests/test_memory_v1_v5_2_atom_admission_apply_v2.py

production_head_before=$(git -C /opt/chat-memory rev-parse HEAD)
qdrant_before=$(qdrant_signature)
capture_protected "$production" "$protected_before"

phase=clone_restore
docker exec "$container" pg_dump -U sage -d "$production" -Fc >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --exit-on-error <"$backup"

phase=router_compatibility
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null

clone_dsn=$("$python_bin" -c \
  'import sys; from urllib.parse import urlsplit,urlunsplit; u=urlsplit(sys.argv[1]); print(urlunsplit((u.scheme,u.netloc,"/"+sys.argv[2],u.query,u.fragment)))' \
  "$POSTGRES_DSN" "$clone")

run_router() {
  local actor=$1 output=$2 apply=${3:-false}
  local -a command=(
    "$python_bin" "$router"
    --owner-user-id "$actor"
    --packet-id "$packet"
    --review-root "$review_root"
  )
  if [[ "$apply" == true ]]; then
    command+=(--apply)
  fi
  POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  MEMORY_V1_V5_2_EXACT_REVIEW_ROUTE_APPLY=memory_v1_v5_2_exact_review_route_apply_v1 \
    "${command[@]}" >"$output"
}

phase=exact_route
run_router "$owner" "$route_output" true
jq -e '
  .apply==true and .outcome=="manual_review_artifacts_ready" and
  (.plans|length)==1 and
  .plans[0].route=="manual_review_artifact_ready" and
  .write_counts.route_events==1 and
  .write_counts.restricted_review_artifacts==2 and
  .write_counts.stage==0 and .write_counts.claims==0 and
  .write_counts.qdrant==0 and .write_counts.prompt_influence==0 and
  .transactional_apply_proved==true and
  .zero_write_replay_proved==true and .external_model_calls==0
' "$route_output" >/dev/null

phase=route_replay_and_isolation
run_router "$owner" "$route_replay"
jq -e '
  .apply==false and (.plans|length)==1 and
  .plans[0].route=="no_work" and
  .database_writes==0 and .filesystem_writes==0 and
  .stage_writes==0 and .claim_writes==0 and
  .qdrant_writes==0 and .prompt_influence==0 and
  .external_model_calls==0
' "$route_replay" >/dev/null
run_router "$other" "$isolation_output"
jq -e '
  .apply==false and (.plans|length)==1 and
  .plans[0].route=="no_work" and
  .database_writes==0 and .filesystem_writes==0 and
  .stage_writes==0 and .claim_writes==0 and
  .qdrant_writes==0 and .prompt_influence==0 and
  .external_model_calls==0
' "$isolation_output" >/dev/null

phase=atom_admission_plan
POSTGRES_DSN="$clone_dsn" "$python_bin" - "$owner" "$packet" >"$plan_output" <<'PY'
import asyncio
import json
import os
import sys

import asyncpg


async def main() -> None:
    connection = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    transaction = connection.transaction(isolation="repeatable_read", readonly=True)
    try:
        await transaction.start()
        await connection.execute(
            "SELECT set_config('app.user_id',$1,true)", sys.argv[1]
        )
        plan = await connection.fetchval(
            "SELECT memory.plan_owner_v5_2_atom_admission_v2($1::uuid)",
            sys.argv[2],
        )
        if isinstance(plan, str):
            plan = json.loads(plan)
        print(json.dumps(plan, sort_keys=True))
        await transaction.rollback()
    finally:
        await connection.close()


asyncio.run(main())
PY
jq -e \
  --arg packet "$packet" \
  --arg evidence "$evidence" \
  --arg packet_sha "$packet_sha" \
  --arg proposal_sha "$proposal_sha" '
  .contract_version=="memory_v1_v5_2_atom_admission_proposal_v2" and
  .policy_version=="memory_v1_v5_2_atom_admission_policy_v2" and
  .source_packet_id==$packet and .source_evidence_id==$evidence and
  .source_packet_storage_sha256==$packet_sha and
  .proposal_sha256==$proposal_sha and
  .counts=={
    "source_atom_count":2,
    "admitted_entity_mention_count":1,
    "admitted_observation_count":1,
    "admitted_comparison_hint_count":0,
    "deferred_atom_count":0,
    "retained_source_only_count":0,
    "rejected_atom_count":0
  } and
  (.stage_projection.entity_mentions|length)==1 and
  .stage_projection.entity_mentions[0].entity_type=="self" and
  (.stage_projection.observations|length)==1 and
  .stage_projection.observations[0].predicate=="stance.reported" and
  .stage_projection.observations[0].projection_class=="reported_stance" and
  .stage_projection.deferrals==[] and
  .stage_projection.packet_findings==[]
' "$plan_output" >/dev/null

downstream_before=$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.relational_stage_batch),
    (SELECT count(*) FROM memory.entity_mention),
    (SELECT count(*) FROM memory.observation),
    (SELECT count(*) FROM memory.claim)
  )
")

phase=admission_preflight
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  "$python_bin" "$admission" \
  --mode preflight --manifest "$manifest" --output "$preflight_output"
jq -e '
  .mode=="preflight" and .persistent_writes==0 and
  (.results|length)==1
' "$preflight_output" >/dev/null

phase=admission_apply
MEMORY_V1_V5_2_ATOM_ADMISSION_APPLY=authorized \
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  "$python_bin" "$admission" \
  --mode apply --manifest "$manifest" --output "$apply_output"
jq -e '
  .mode=="apply" and .persistent_writes==6 and
  (.results|length)==1 and
  .results[0].proposal_outcome=="applied"
' "$apply_output" >/dev/null

phase=admission_replay
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  "$python_bin" "$admission" \
  --mode replay --manifest "$manifest" --output "$replay_output"
jq -e '
  .mode=="replay" and .persistent_writes==0 and
  (.results|length)==1 and
  .results[0].proposal_outcome=="replayed"
' "$replay_output" >/dev/null

phase=postflight
[[ "$(find "$review_root" -maxdepth 1 -type f -name '*.json' | wc -l)" == 2 ]]
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.v5_2_atom_admission_proposal
      WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid),
    (SELECT count(*) FROM memory.v5_2_atom_admission_review
      WHERE owner_user_id='$owner'::uuid
        AND proposal_id='9ab3a7bb-4ca5-5f28-a6e2-8b41c7a9d1c7'::uuid),
    (SELECT count(*) FROM memory.v5_2_atom_admission_apply
      WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid),
    (SELECT count(*) FROM memory.v5_2_atom_admission_operation
      WHERE owner_user_id='$owner'::uuid
        AND operation_id IN (
          '0e14c700-a70e-51ca-9f21-b5b98ac37191'::uuid,
          '52543e55-2b60-5a25-9a3d-1f2979d3548e'::uuid,
          'fc8d2b7e-0b80-524b-b4d2-8722f56817dd'::uuid
        ))
  )
")" == 1,1,1,3 ]]
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc "
  SELECT count(*) FROM memory.v5_2_local_packet_route_event
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
    AND route='manual_review_artifact_ready'
")" == 1 ]]
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc "
  SELECT count(*) FROM memory.v5_2_local_packet_route_event
  WHERE owner_user_id='$other'::uuid AND packet_id='$packet'::uuid
")" == 0 ]]
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.relational_stage_batch),
    (SELECT count(*) FROM memory.entity_mention),
    (SELECT count(*) FROM memory.observation),
    (SELECT count(*) FROM memory.claim)
  )
")" == "$downstream_before" ]]

capture_protected "$production" "$protected_after"
cmp -s "$protected_before" "$protected_after"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(git -C /opt/chat-memory rev-parse HEAD)" == "$production_head_before" ]]
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(curl -sS -o /dev/null -w '%{http_code}' \
  http://127.0.0.1:8088/docs)" == 200 ]]

phase=complete
printf '%s\n' \
  'memory_v1_v5_2_evidence_context_route_admission_clone: PASS' \
  "manifest_sha256=$(jq -r '.manifest_sha256' "$manifest")" \
  'clone_route_rows=1' \
  'clone_admission_rows=1,1,1,3' \
  'downstream_rows=0' \
  "production_head=$production_head_before" \
  "qdrant_signature=$qdrant_before"
