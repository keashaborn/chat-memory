#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Verifies the governed write-boundary ACL migration on a
# disposable production clone. Production and Qdrant remain read-only.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
container=brains-postgres-1
production=memory
clone="memory_v1_write_acl_${$}"
migration=ops/sql/20260719_memory_v1_governed_write_boundary.sql
rollback=ops/sql/20260719_memory_v1_governed_write_boundary_rollback.sql
test_sql=tests/memory_v1_governed_write_boundary.sql
backup=$(mktemp /tmp/memory-v1-write-acl-clone.XXXXXX.dump)

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists "$clone" >/dev/null 2>&1 || true
  rm -f "$backup"
}
trap cleanup EXIT
chmod 0600 "$backup"

run_sql() {
  docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
    -U sage -d "$clone" "$@"
}

scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$clone" -c "$1" | tr -d '[:space:]'
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

[[ -f "$migration" && -f "$rollback" && -f "$test_sql" ]]
[[ -z "$(git status --porcelain)" ]]
qdrant_before=$(qdrant_signature)
production_claims_before=$(docker exec "$container" psql -X -A -t \
  -U sage -d "$production" -c 'SELECT count(*) FROM memory.claim' \
  | tr -d '[:space:]')

docker exec "$container" pg_dump -U sage -d "$production" \
  -Fc --no-owner >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --no-owner --exit-on-error <"$backup"

claims_before=$(scalar 'SELECT count(*) FROM memory.claim')
evidence_before=$(scalar 'SELECT count(*) FROM memory.evidence')

run_sql <"$migration" >/dev/null
run_sql <"$migration" >/dev/null
run_sql <"$test_sql" >/dev/null
[[ "$(scalar 'SELECT count(*) FROM memory.claim')" == "$claims_before" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.evidence')" == "$evidence_before" ]]

run_sql <"$rollback" >/dev/null
[[ "$(scalar "SELECT has_table_privilege('brains_app','memory.claim','INSERT,UPDATE,DELETE')")" == t ]]
run_sql <"$migration" >/dev/null
[[ "$(scalar "SELECT NOT has_table_privilege('brains_app','memory.claim','INSERT,UPDATE,DELETE')")" == t ]]

[[ "$(docker exec "$container" psql -X -A -t -U sage -d "$production" \
  -c 'SELECT count(*) FROM memory.claim' | tr -d '[:space:]')" == "$production_claims_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

printf '%s\n' 'memory_v1_governed_write_boundary_production_clone: PASS'
