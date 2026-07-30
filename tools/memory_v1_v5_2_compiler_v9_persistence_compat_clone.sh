#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
container=brains-postgres-1
production=memory
clone="memory_v5_2_compiler_v9_persistence_compat_${$}"
migration=ops/sql/20260730_memory_v1_v5_2_compiler_v9_persistence_compat.sql
rollback=ops/sql/20260730_memory_v1_v5_2_compiler_v9_persistence_compat_rollback.sql
test_sql=tests/memory_v1_v5_2_compiler_v9_persistence_compat.sql
old_sha=4dcbd998364abc91d5f6abd0844546c74387f4c057fc876efbee7c222eb0c8ab
new_sha=2b3182f59091a7d93697ae79ee6a8e1cc7541829f957b5a859c47cf28707d190
backup=$(mktemp /tmp/memory-v5-2-compiler-v9-persistence.XXXXXX.dump)
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

function_sha() {
  local database=$1
  docker exec "$container" psql -U sage -d "$database" -X -Atqc "
    SELECT encode(public.digest(convert_to(pg_get_functiondef(
      'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure
    ),'UTF8'),'sha256'),'hex')"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

production_before=$(function_sha "$production")
[[ "$production_before" == "$old_sha" ]]
qdrant_before=$(qdrant_signature)

docker exec "$container" pg_dump -U sage -d "$production" \
  -Fc --no-owner >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --exit-on-error <"$backup"

docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
[[ "$(function_sha "$clone")" == "$new_sha" ]]

# Migration replay is zero-change.
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
[[ "$(function_sha "$clone")" == "$new_sha" ]]

docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$test_sql" >/dev/null

# Rollback is exact and the migration can be reapplied afterward.
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$rollback" >/dev/null
[[ "$(function_sha "$clone")" == "$old_sha" ]]
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
[[ "$(function_sha "$clone")" == "$new_sha" ]]

[[ "$(function_sha "$production")" == "$production_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(systemctl is-active brains.service)" == active ]]

printf '%s\n' 'memory_v1_v5_2_compiler_v9_persistence_compat_clone: PASS'
