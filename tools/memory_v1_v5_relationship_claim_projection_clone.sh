#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Verifies neutral kinship projection on an owner-preserving
# production clone, including idempotency, rollback, account isolation, and no
# database-row or Qdrant change.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
port=${MEMORY_V1_V5_RELATIONSHIP_CLAIM_CLONE_PORT:-55472}
project=memoryv1v5relationshipclaimclone
compose=(docker compose -p "$project" -f docker-compose.ci.yml \
  -f docker-compose.stage-batch-clone.yml)
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
migration=ops/sql/20260719_memory_v1_v5_relationship_claim_projection.sql
rollback=ops/sql/20260719_memory_v1_v5_relationship_claim_projection_rollback.sql
sql_test=tests/memory_v1_v5_relationship_claim_projection.sql
local_projection_test=tests/memory_v1_v5_local_claim_projection.sql
python_test=scripts/memory_v1_v5_claim_projection_preflight_test.py

backup=$(mktemp /tmp/memory-v1-v5-relationship-claim.XXXXXX.dump)
work=$(mktemp -d /tmp/memory-v1-v5-relationship-claim.XXXXXX)
table_list="$work/tables.tsv"
before="$work/before.tsv"
after="$work/after.tsv"

cleanup() {
  MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" down -v \
    >/dev/null 2>&1 || true
  rm -f "$backup"
  rm -rf "$work"
}
trap cleanup EXIT
chmod 0600 "$backup"

run_sql() {
  MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" exec -T postgres \
    psql -X -v ON_ERROR_STOP=1 -U sage -d memory "$@"
}

scalar() {
  MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" exec -T postgres \
    psql -X -A -t -v ON_ERROR_STOP=1 -U sage -d memory -c "$1" \
    | tr -d '[:space:]'
}

capture_rows() {
  local output=$1 schema table state
  : >"$output"
  while IFS=$'\t' read -r schema table; do
    state=$(scalar "SELECT count(*)::text || ':' ||
      encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
        ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
      FROM (SELECT to_jsonb(value)::text AS row_json
        FROM \"$schema\".\"$table\" AS value) rows")
    printf '%s\t%s\t%s\n' "$schema" "$table" "$state" >>"$output"
  done <"$table_list"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

actor_preflight() {
  local actor=$1 observation=$2
  printf "BEGIN; SET LOCAL SESSION AUTHORIZATION brains_app; SELECT set_config('app.user_id','%s',true); SELECT canonical_text FROM memory.preflight_claim_projection_source_v5_1('%s'::uuid); ROLLBACK;\n" \
    "$actor" "$observation" \
    | MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" exec -T postgres \
      psql -X -q -A -t -v ON_ERROR_STOP=1 -U sage -d memory
}

for required in "$migration" "$rollback" "$sql_test" \
  "$local_projection_test" "$python_test"; do
  [[ -f "$required" ]]
done
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$python_test"
python3 -m py_compile scripts/memory_v1_v5_claim_projection_preflight.py

docker exec brains-postgres-1 pg_dump -U sage -d memory -Fc >"$backup"
[[ -s "$backup" ]]
MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" up -d --wait postgres
{
  printf '%s\n' "CREATE ROLE brains_app LOGIN PASSWORD 'clone_only_brains_password' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;"
  docker exec brains-postgres-1 psql -X -A -t -U sage -d memory -c \
    "SELECT format('CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;',rolname) FROM pg_roles WHERE rolname LIKE 'memory%' ORDER BY rolname;"
} | run_sql >/dev/null
MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" exec -T postgres \
  pg_restore -U sage -d memory --clean --if-exists <"$backup"

run_sql -At -c "SELECT table_schema || E'\\t' || table_name
  FROM information_schema.tables WHERE table_type='BASE TABLE'
    AND table_schema IN ('memory','public')
  ORDER BY table_schema,table_name" >"$table_list"
capture_rows "$before"
qdrant_before=$(qdrant_signature)

run_sql <"$migration" >/dev/null
run_sql <"$migration" >/dev/null
run_sql <"$sql_test" >/dev/null
run_sql -v owner_user_id="$owner" <"$local_projection_test" >/dev/null
observation=$(scalar "SELECT observation_id FROM memory.observation
  WHERE owner_user_id='$owner'::uuid AND predicate='relationship.parent_of'
  ORDER BY created_at DESC LIMIT 1")
[[ "$observation" =~ ^[0-9a-f-]{36}$ ]]
owner_output=$(actor_preflight "$owner" "$observation")
[[ "$(printf '%s\n' "$owner_output" | tail -n 1)" == "dad is the user's parent." ]]
if actor_preflight "$other" "$observation" >/dev/null 2>&1; then
  echo 'cross-owner relationship source was visible' >&2
  exit 1
fi

run_sql <"$rollback" >/dev/null
[[ "$(scalar "SELECT position('''relationship.parent_of'',''relationship.sibling_of''' IN pg_get_functiondef('memory.preflight_claim_projection_source_v5_1(uuid)'::regprocedure))=0")" == t ]]
run_sql <"$migration" >/dev/null
run_sql <"$sql_test" >/dev/null

capture_rows "$after"
cmp -s "$before" "$after"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
printf '%s\n' 'memory_v1_v5_relationship_claim_projection_clone: PASS'
