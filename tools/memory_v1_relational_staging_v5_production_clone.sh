#!/usr/bin/env bash
set -euo pipefail

: "${POSTGRES_DSN:?POSTGRES_DSN must point to seebx production for schema-only cloning}"

compose=(docker compose -p memoryv1v5prodclone -f docker-compose.ci.yml)
migration=ops/sql/20260715_memory_v1_relational_staging_v5.sql
rollback=ops/sql/20260715_memory_v1_relational_staging_v5_rollback.sql
security_test=tests/memory_v1_relational_staging_v5.sql

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT

run_sql() {
  "${compose[@]}" exec -T postgres \
    psql -X -v ON_ERROR_STOP=1 -U sage -d memory
}

"${compose[@]}" up -d --wait postgres

# Schema only: no production rows or chat content enter the clone.
pg_dump "$POSTGRES_DSN" \
  --schema-only --no-owner --no-privileges \
  | run_sql

DOES_ROLE_EXIST=$(
  "${compose[@]}" exec -T postgres \
    psql -X -A -t -v ON_ERROR_STOP=1 -U sage -d memory \
    -c "SELECT count(*) FROM pg_roles WHERE rolname='brains_app'"
)
if [[ "$DOES_ROLE_EXIST" = '0' ]]; then
  printf '%s\n' \
    'CREATE ROLE brains_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
    | run_sql
fi

baseline_predicate_count=$(
  "${compose[@]}" exec -T postgres \
    psql -X -A -t -v ON_ERROR_STOP=1 -U sage -d memory \
    -c 'SELECT count(*) FROM memory.predicate'
)

run_sql < "$migration"
run_sql < "$migration"
run_sql < "$security_test"
run_sql < "$rollback"

remaining_predicate_count=$(
  "${compose[@]}" exec -T postgres \
    psql -X -A -t -v ON_ERROR_STOP=1 -U sage -d memory \
    -c 'SELECT count(*) FROM memory.predicate'
)
if [[ "$remaining_predicate_count" != "$baseline_predicate_count" ]]; then
  echo 'production-schema clone rollback changed predicate count' >&2
  exit 1
fi

echo 'memory_v1_relational_staging_v5_production_clone: PASS'
