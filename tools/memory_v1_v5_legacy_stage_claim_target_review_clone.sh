#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Reviews the exact accepted legacy-stage observation set
# against the authoritative V5.2 claim-target resolver in a disposable clone.
# It never writes production, creates claims, calls a model, or touches Qdrant.

if [[ "$EUID" -ne 0 ]]; then
  echo 'run through sudo' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
container=brains-postgres-1
source_db=memory
clone_db="memory_v5_2_claim_target_review_$(date -u +%Y%m%d%H%M%S)_$$"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
first_admission=8c751d72-073c-4b33-9431-72838f374003
second_admission=fd48e387-406e-47a8-b87a-b15320d63487
policy_held=1a498c5b-29ee-4b91-a038-7cc3987162f9
expected_target_sha256=a8400e1d233c9ecde4a22420e6705d31166a14e4013cffbc487e13bbaa951885
migration=ops/sql/20260730_memory_v1_v5_2_legacy_stage_projection_source_compat_v1.sql
rollback=ops/sql/20260730_memory_v1_v5_2_legacy_stage_projection_source_compat_v1_rollback.sql
migration_sha256=f065af7daa69b2d5e396e73def8f1c6aa66807d447ac49dd37d91651318433b3
rollback_sha256=c0f8b984e7f434c04bb04ebd8ce49dee72219d1b55c357da0ed9532d0b941cf6
python_bin=/opt/chat-memory/venv/bin/python
review_dir=/home/ubuntu/memory-v1-reviews
run_id=$(date -u +%Y%m%dT%H%M%SZ)-$$
artifact_dir="$review_dir/legacy-stage-claim-target-review-$run_id"
summary="$artifact_dir/summary.json"
cross_owner="$artifact_dir/cross-owner.json"
cross_owner_log="$artifact_dir/cross-owner.log"

if [[ -r "$repo_root/.env" ]]; then
  set -a
  source "$repo_root/.env"
  set +a
elif [[ -r /opt/chat-memory/.env ]]; then
  set -a
  source /opt/chat-memory/.env
  set +a
fi
[[ -n "${POSTGRES_DSN:-}" ]]
[[ "$(stat -c '%a' "$review_dir")" == 700 ]]
install -d -m 0700 "$artifact_dir"
[[ "$(sha256sum "$repo_root/$migration" | cut -d' ' -f1)" == \
  "$migration_sha256" ]]
[[ "$(sha256sum "$repo_root/$rollback" | cut -d' ' -f1)" == \
  "$rollback_sha256" ]]

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists --force "$clone_db" \
    >/dev/null 2>&1 || true
}
trap cleanup EXIT

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | cut -d' ' -f1
}

production_signature() {
  docker exec "$container" psql -X -A -t -U sage -d "$source_db" \
    -v ON_ERROR_STOP=1 -c "
      SELECT jsonb_build_object(
        'claims',(SELECT count(*) FROM memory.claim),
        'claim_revisions',(SELECT count(*) FROM memory.claim_revision),
        'claim_links',(SELECT count(*) FROM memory.claim_observation),
        'entities',(SELECT count(*) FROM memory.entity),
        'observations',(SELECT count(*) FROM memory.observation),
        'entailments',(SELECT count(*) FROM memory.observation_entailment_v5),
        'projection_plans',(SELECT count(*) FROM memory.projection_plan)
      )::text;" | sha256sum | cut -d' ' -f1
}

production_before=$(production_signature)
qdrant_before=$(qdrant_signature)
production_function_before=$(
  docker exec "$container" psql -X -A -t -U sage -d "$source_db" \
    -v ON_ERROR_STOP=1 -c "
      SELECT pg_get_functiondef(
        'memory.preflight_projection_source_v5_2(uuid)'::regprocedure
      );" | sha256sum | cut -d' ' -f1
)

docker exec "$container" createdb -U sage -T template0 "$clone_db"
docker exec "$container" pg_dump -U sage -d "$source_db" -Fc \
  | docker exec -i "$container" pg_restore -U sage -d "$clone_db"
docker exec "$container" psql -X -U sage -d postgres -v ON_ERROR_STOP=1 \
  -c "COMMENT ON DATABASE \"$clone_db\" IS
      'memory_v1_v5_2_claim_target_review_clone_v1';" >/dev/null

clone_dsn=$(SOURCE_DSN="$POSTGRES_DSN" CLONE_DB="$clone_db" python3 - <<'PY'
import os
from urllib.parse import urlsplit, urlunsplit

source = urlsplit(os.environ["SOURCE_DSN"])
if source.scheme not in {"postgres", "postgresql"}:
    raise SystemExit("unsupported PostgreSQL DSN scheme")
print(urlunsplit((
    source.scheme,
    source.netloc,
    "/" + os.environ["CLONE_DB"],
    source.query,
    source.fragment,
)))
PY
)

mapfile -t targets < <(
  docker exec "$container" psql -X -A -t -F '|' -U sage -d "$clone_db" \
    -v ON_ERROR_STOP=1 -c "
      SELECT member.observation_id,member.observation_sha256
      FROM memory.v5_local_legacy_stage_admission_observation AS member
      JOIN memory.v5_local_packet_stage_admission AS stage
        ON stage.owner_user_id=member.owner_user_id
       AND stage.admission_id=member.admission_id
      JOIN memory.observation_entailment_v5 AS entailment
        ON entailment.owner_user_id=member.owner_user_id
       AND entailment.observation_id=member.observation_id
      WHERE member.owner_user_id='$owner'::uuid
        AND member.admission_id IN (
          '$first_admission'::uuid,
          '$second_admission'::uuid
        )
        AND stage.decision='legacy_applied_stage_entailment'
        AND entailment.decision='accepted'
      ORDER BY member.observation_id;"
)
[[ "${#targets[@]}" == 20 ]]
target_sha256=$(
  printf '%s\n' "${targets[@]}" | sha256sum | cut -d' ' -f1
)
[[ "$target_sha256" == "$expected_target_sha256" ]]
target_ids_json=$(
  printf '%s\n' "${targets[@]}" \
    | awk -F'|' '{print $1}' \
    | jq -Rsc 'split("\n")|map(select(length>0))'
)

runtime=(
  env
  POSTGRES_DSN="$clone_dsn"
  PYTHONPATH="$repo_root:$repo_root/scripts"
  MEMORY_V1_DISPOSABLE_CLONE_REQUIRED=1
)

clone_state_before=$(
  docker exec "$container" psql -X -A -t -U sage -d "$clone_db" \
    -v ON_ERROR_STOP=1 -c "
      SELECT jsonb_build_object(
        'claims',(SELECT count(*) FROM memory.claim),
        'claim_revisions',(SELECT count(*) FROM memory.claim_revision),
        'claim_links',(SELECT count(*) FROM memory.claim_observation),
        'entities',(SELECT count(*) FROM memory.entity),
        'observations',(SELECT count(*) FROM memory.observation),
        'entailments',(SELECT count(*) FROM memory.observation_entailment_v5),
        'projection_plans',(SELECT count(*) FROM memory.projection_plan)
      )::text;" | sha256sum | cut -d' ' -f1
)

POSTGRES_DSN="$clone_dsn" TARGET_IDS_JSON="$target_ids_json" \
  "$python_bin" - <<'PY'
import asyncio
import json
import os
import uuid

import asyncpg

OWNER = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
TARGETS = [uuid.UUID(value) for value in json.loads(os.environ["TARGET_IDS_JSON"])]

async def main():
    conn = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    try:
        await conn.execute("SELECT set_config('app.user_id',$1,false)", str(OWNER))
        failures = 0
        for observation_id in TARGETS:
            try:
                await conn.fetchrow(
                    "SELECT * FROM memory.preflight_projection_source_v5_2($1)",
                    observation_id,
                )
            except asyncpg.NoDataFoundError:
                failures += 1
        if len(TARGETS) != 20 or failures != 20:
            raise SystemExit("pre-migration source rejection boundary drifted")
    finally:
        await conn.close()

asyncio.run(main())
PY

docker exec -i "$container" psql -X -U sage -d "$clone_db" \
  -v ON_ERROR_STOP=1 <"$repo_root/$migration" >/dev/null

helper_allowed=$(
  docker exec "$container" psql -X -A -t -U sage -d "$clone_db" \
    -v ON_ERROR_STOP=1 -c "
      SELECT set_config('app.user_id','$owner',false);
      SELECT count(*)
      FROM memory.v5_local_legacy_stage_admission_observation AS member
      WHERE member.owner_user_id='$owner'::uuid
        AND member.admission_id IN (
          '$first_admission'::uuid,
          '$second_admission'::uuid
        )
        AND memory.v5_2_legacy_stage_projection_source_allowed_v1(
          member.owner_user_id,
          member.observation_id,
          member.observation_sha256
        );" | tail -n1
)
[[ "$helper_allowed" == 20 ]]

POSTGRES_DSN="$clone_dsn" TARGET_IDS_JSON="$target_ids_json" \
  "$python_bin" - <<'PY'
import asyncio
import json
import os
import uuid

import asyncpg

OWNER = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
OTHER = uuid.UUID("557ea042-cb82-48f8-9429-472e96c957ef")
NON_MEMBER_V5 = uuid.UUID("9bf1e6b2-1840-4524-98dc-142567ebe013")
POLICY_HELD = uuid.UUID("1a498c5b-29ee-4b91-a038-7cc3987162f9")
TARGETS = [uuid.UUID(value) for value in json.loads(os.environ["TARGET_IDS_JSON"])]

async def rejected(conn, observation):
    try:
        await conn.fetchrow(
            "SELECT * FROM memory.preflight_projection_source_v5_2($1)",
            observation,
        )
    except asyncpg.NoDataFoundError:
        return True
    return False

async def main():
    conn = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    try:
        await conn.execute("SELECT set_config('app.user_id',$1,false)", str(OWNER))
        if len(TARGETS) != 20:
            raise SystemExit("post-migration target count drifted")
        rejected_targets = []
        for observation_id in TARGETS:
            try:
                source = await conn.fetchrow(
                    "SELECT * FROM memory.preflight_projection_source_v5_2($1)",
                    observation_id,
                )
            except asyncpg.NoDataFoundError:
                rejected_targets.append(observation_id)
                continue
            if (
                source is None
                or source["predicate_registry_version"]
                != "memory_predicate_registry_v5_2"
            ):
                raise SystemExit("legacy source was not admitted as V5.2")
        if rejected_targets != [POLICY_HELD]:
            raise SystemExit("V5.2 source policy holds drifted")
        if not await rejected(conn, NON_MEMBER_V5):
            raise SystemExit("non-member V5 observation was admitted")
        await conn.execute("SELECT set_config('app.user_id',$1,false)", str(OTHER))
        if not await rejected(conn, TARGETS[0]):
            raise SystemExit("cross-owner source was admitted")
    finally:
        await conn.close()

asyncio.run(main())
PY

[[ "$(
  docker exec "$container" psql -X -A -t -U sage -d "$clone_db" \
    -v ON_ERROR_STOP=1 -c "
      SELECT pg_get_userbyid(proowner)||'|'||
        coalesce(array_to_string(proacl,','),'')
      FROM pg_proc
      WHERE oid=
        'memory.preflight_projection_source_v5_2(uuid)'::regprocedure;"
)" == \
  'memory_v5_writer|memory_v5_writer=X/memory_v5_writer,brains_app=X/memory_v5_writer' ]]
[[ "$(
  docker exec "$container" psql -X -A -t -U sage -d "$clone_db" \
    -v ON_ERROR_STOP=1 -c "
      SELECT pg_get_userbyid(proowner)||'|'||
        coalesce(array_to_string(proacl,','),'')
      FROM pg_proc
      WHERE oid=
        'memory.v5_2_legacy_stage_projection_source_allowed_v1(uuid,uuid,text)'::regprocedure;"
)" == \
  'memory_v5_legacy_stage_compat_maintainer|memory_v5_legacy_stage_compat_maintainer=X/memory_v5_legacy_stage_compat_maintainer,memory_v5_writer=X/memory_v5_legacy_stage_compat_maintainer' ]]
[[ "$(
  docker exec "$container" psql -X -A -t -U sage -d "$clone_db" \
    -v ON_ERROR_STOP=1 -c "
      SELECT pg_get_userbyid(proowner)||'|'||
        coalesce(array_to_string(proacl,','),'')
      FROM pg_proc
      WHERE oid=
        'memory.render_projection_claim_text_v5_2(uuid)'::regprocedure;"
)" == 'memory_v5_writer|memory_v5_writer=X/memory_v5_writer' ]]

success_files=()
failed_rows=()
for target in "${targets[@]}"; do
  observation=${target%%|*}
  observation_sha256=${target#*|}
  item_report="$artifact_dir/review-$observation.json"
  item_log="$artifact_dir/review-$observation.log"
  if "${runtime[@]}" "$python_bin" \
    "$repo_root/scripts/memory_v1_v5_2_claim_target_review.py" \
    --owner "$owner" \
    --observation "$observation" \
    --output "$item_report" >"$item_log" 2>&1; then
    [[ "$(jq -er '.item_count' "$item_report")" == 1 ]]
    [[ "$(jq -er '.proofs.database_writes' "$item_report")" == 0 ]]
    [[ "$(jq -er '.proofs.local_model_calls' "$item_report")" == 0 ]]
    [[ "$(jq -er '.proofs.external_model_calls' "$item_report")" == 0 ]]
    [[ "$(jq -er '.proofs.claim_writes' "$item_report")" == 0 ]]
    [[ "$(jq -er '.proofs.qdrant_writes' "$item_report")" == 0 ]]
    [[ "$(jq -er '.proofs.disposable_clone_verified' "$item_report")" == true ]]
    success_files+=("$item_report")
  else
    [[ ! -e "$item_report" ]]
    failed_rows+=("$observation|$observation_sha256")
  fi
done

success_count=${#success_files[@]}
failure_count=${#failed_rows[@]}
[[ "$((success_count + failure_count))" == 20 ]]
[[ "$success_count" == 19 ]]
[[ "$failure_count" == 1 ]]
[[ "${failed_rows[0]%%|*}" == "$policy_held" ]]
grep -q \
  'complete owner-scoped V5.2 projection source not found' \
  "$artifact_dir/review-$policy_held.log"

if ((success_count)); then
  action_counts=$(
    jq -cs '
      map(.items[0].action)
      | group_by(.)
      | map({key:.[0],value:length})
      | from_entries
    ' "${success_files[@]}"
  )
else
  action_counts='{}'
fi
[[ "$action_counts" == '{"create":19}' ]]
predicate_counts=$(
  jq -cs '
    map(.items[0].predicate)
    | group_by(.)
    | map({key:.[0],value:length})
    | from_entries
  ' "${success_files[@]}"
)
[[ "$predicate_counts" == \
  '{"age.reported":1,"health.user_reported_observation":2,"identity.name":5,"pet.breed":1,"pet.coat_color":2,"pet.eye_color":2,"pet.hearing_status":1,"pet.sex":1,"pet.weight_reported":1,"relationship.sibling_of":3}' ]]

failed_hashes=$(
  printf '%s\n' "${failed_rows[@]:-}" \
    | awk -F'|' 'NF==2 {print $1}' \
    | sha256sum | cut -d' ' -f1
)
jq -nS \
  --arg contract_version \
    memory_v1_v5_legacy_stage_claim_target_review_summary_v1 \
  --arg owner_user_id_sha256 \
    "$(printf '%s' "$owner" | sha256sum | cut -d' ' -f1)" \
  --arg target_set_sha256 "$target_sha256" \
  --arg failed_observation_set_sha256 "$failed_hashes" \
  --argjson item_count 20 \
  --argjson success_count "$success_count" \
  --argjson failure_count "$failure_count" \
  --argjson action_counts "$action_counts" \
  '{
    contract_version:$contract_version,
    owner_user_id_sha256:$owner_user_id_sha256,
    target_set_sha256:$target_set_sha256,
    item_count:$item_count,
    success_count:$success_count,
    failure_count:$failure_count,
    action_counts:$action_counts,
    failure_reason_counts:{surface_policy_contract_hold:$failure_count},
    failed_observation_set_sha256:$failed_observation_set_sha256,
    proofs:{
      database_writes:0,
      local_model_calls:0,
      external_model_calls:0,
      claim_writes:0,
      qdrant_writes:0,
      production_writes:0
    }
  }' >"$summary"
chmod 0600 "$summary"

if "${runtime[@]}" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_claim_target_review.py" \
  --owner "$other_owner" \
  --observation "${targets[0]%%|*}" \
  --output "$cross_owner" >"$cross_owner_log" 2>&1; then
  echo 'cross-owner review unexpectedly succeeded' >&2
  exit 1
fi
[[ ! -e "$cross_owner" ]]

[[ "$(production_signature)" == "$production_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

docker exec -i "$container" psql -X -U sage -d "$clone_db" \
  -v ON_ERROR_STOP=1 <"$repo_root/$rollback" >/dev/null
clone_function_after=$(
  docker exec "$container" psql -X -A -t -U sage -d "$clone_db" \
    -v ON_ERROR_STOP=1 -c "
      SELECT pg_get_functiondef(
        'memory.preflight_projection_source_v5_2(uuid)'::regprocedure
      );" | sha256sum | cut -d' ' -f1
)
[[ "$clone_function_after" == "$production_function_before" ]]
[[ "$(
  docker exec "$container" psql -X -A -t -U sage -d "$clone_db" \
    -v ON_ERROR_STOP=1 -c "
      SELECT
        pg_get_functiondef(
          'memory.render_projection_claim_text_v5_2(uuid)'::regprocedure
        ) LIKE '%observation.predicate_registry_version=%memory_predicate_registry_v5_2%'
        AND pg_get_functiondef(
          'memory.render_projection_claim_text_v5_2(uuid)'::regprocedure
        ) NOT LIKE '%SELECT * INTO STRICT source%';"
)" == t ]]

clone_state_after=$(
  docker exec "$container" psql -X -A -t -U sage -d "$clone_db" \
    -v ON_ERROR_STOP=1 -c "
      SELECT jsonb_build_object(
        'claims',(SELECT count(*) FROM memory.claim),
        'claim_revisions',(SELECT count(*) FROM memory.claim_revision),
        'claim_links',(SELECT count(*) FROM memory.claim_observation),
        'entities',(SELECT count(*) FROM memory.entity),
        'observations',(SELECT count(*) FROM memory.observation),
        'entailments',(SELECT count(*) FROM memory.observation_entailment_v5),
        'projection_plans',(SELECT count(*) FROM memory.projection_plan)
      )::text;" | sha256sum | cut -d' ' -f1
)
[[ "$clone_state_after" == "$clone_state_before" ]]

[[ "$(production_signature)" == "$production_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

printf '%s\n' \
  'LEGACY_STAGE_CLAIM_TARGET_REVIEW_CLONE=COMPLETE' \
  "MIGRATION_SHA256=$migration_sha256" \
  "ROLLBACK_SHA256=$rollback_sha256" \
  "TARGET_SET_SHA256=$target_sha256" \
  "SUMMARY=$summary" \
  "ACTION_COUNTS=$action_counts" \
  "SUCCESS_COUNT=$success_count" \
  "FAILURE_COUNT=$failure_count" \
  "ITEMS=20" \
  'DATABASE_WRITES=0' \
  'MODEL_CALLS=0' \
  'CLAIM_WRITES=0' \
  'QDRANT_WRITES=0' \
  'PRODUCTION_WRITES=0' \
  'CROSS_OWNER_REJECTED=1'
