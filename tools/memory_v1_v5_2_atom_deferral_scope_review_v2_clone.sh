#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

container=brains-postgres-1
production=memory
clone="memory_v5_2_atom_scope_v2_${$}"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
profession_packet=6ae4a8e6-b207-5201-a997-53fb2363fc9d
caregiving_packet=b76915b8-0603-50e8-b263-761da39f5651
migration=ops/sql/20260725_memory_v1_v5_2_atom_deferral_scope_review_v2.sql
rollback=ops/sql/20260725_memory_v1_v5_2_atom_deferral_scope_review_v2_rollback.sql
test_sql=tests/memory_v1_v5_2_atom_deferral_scope_review_v2.sql
backup=$(mktemp /tmp/memory-v5-2-atom-scope-v2.XXXXXX.dump)
chmod 0600 "$backup"

cleanup() {
  rc=$?
  trap - EXIT
  docker exec "$container" dropdb -U sage --if-exists --force "$clone" \
    >/dev/null 2>&1 || true
  rm -f "$backup"
  exit "$rc"
}
trap cleanup EXIT

database_signature() {
  local database=$1
  docker exec "$container" psql -U sage -d "$database" -X -Atqc "
    WITH rows AS (
      SELECT 'proposal' AS lane,to_jsonb(value) AS value
      FROM memory.v5_2_atom_admission_proposal AS value
      UNION ALL
      SELECT 'review',to_jsonb(value)
      FROM memory.v5_2_atom_admission_review AS value
      UNION ALL
      SELECT 'apply',to_jsonb(value)
      FROM memory.v5_2_atom_admission_apply AS value
      UNION ALL
      SELECT 'operation',to_jsonb(value)
      FROM memory.v5_2_atom_admission_operation AS value
    )
    SELECT encode(public.digest(convert_to(coalesce(string_agg(
      lane||':'||value::text,E'\n' ORDER BY lane,value::text
    ),''),'UTF8'),'sha256'),'hex') FROM rows"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

function_hash() {
  local database=$1
  local function_name=$2
  docker exec "$container" psql -U sage -d "$database" -X -Atqc "
    SELECT encode(public.digest(convert_to(
      pg_get_functiondef('$function_name'::regprocedure),'UTF8'
    ),'sha256'),'hex')"
}

before_database=$(database_signature "$production")
before_qdrant=$(qdrant_signature)

docker exec "$container" pg_dump -U sage -d "$production" \
  -Fc >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --exit-on-error <"$backup"
v1_planner_before=$(function_hash \
  "$clone" 'memory.plan_owner_v5_2_atom_admission_v1(uuid)')
v1_record_before=$(function_hash \
  "$clone" \
  'memory.record_owner_v5_2_atom_proposal_v1(uuid,uuid,uuid,text,text)')

docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null

[[ "$(function_hash \
  "$clone" 'memory.plan_owner_v5_2_atom_admission_v1(uuid)')" \
  == "$v1_planner_before" ]]
[[ "$(function_hash \
  "$clone" \
  'memory.record_owner_v5_2_atom_proposal_v1(uuid,uuid,uuid,text,text)')" \
  == "$v1_record_before" ]]

docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 \
  -v target_owner="$owner" \
  -v other_owner="$other" \
  -v profession_packet="$profession_packet" \
  -v caregiving_packet="$caregiving_packet" \
  <"$test_sql" >/dev/null

docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$rollback" >/dev/null
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc "
  SELECT to_regprocedure(
    'memory.plan_owner_v5_2_atom_admission_v2(uuid)'
  ) IS NULL")" == t ]]
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc "
  SELECT pg_get_constraintdef(oid)
  FROM pg_constraint
  WHERE conrelid='memory.v5_2_atom_admission_proposal'::regclass
    AND conname='v5_2_atom_admission_proposal_policy_version_check'")" \
  != *memory_v1_v5_2_atom_admission_policy_v2* ]]

docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 \
  -v target_owner="$owner" \
  -v other_owner="$other" \
  -v profession_packet="$profession_packet" \
  -v caregiving_packet="$caregiving_packet" \
  <"$test_sql" >/dev/null

python3 -m unittest \
  tests.test_memory_v1_v5_2_atom_admission_apply_v2
python3 -m unittest \
  tests.test_memory_v1_v5_2_atom_admission_manifest_v2

[[ "$(database_signature "$production")" == "$before_database" ]]
[[ "$(qdrant_signature)" == "$before_qdrant" ]]
[[ "$(systemctl is-active brains.service)" == active ]]

printf '%s\n' \
  'memory_v1_v5_2_atom_deferral_scope_review_v2_clone: PASS'
