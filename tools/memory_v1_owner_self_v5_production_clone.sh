#!/usr/bin/env bash
set -euo pipefail

compose=(docker compose -p memoryv1v5selfprodclone -f docker-compose.ci.yml)
migration=ops/sql/20260716_memory_v1_owner_self_v5.sql
rollback=ops/sql/20260716_memory_v1_owner_self_v5_rollback.sql
test_sql=tests/memory_v1_owner_self_v5.sql

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 -U sage -d memory
}

"${compose[@]}" up -d --wait postgres
role_count=$("${compose[@]}" exec -T postgres psql -X -A -t -U sage -d memory \
  -c "SELECT count(*) FROM pg_roles WHERE rolname='brains_app'")
if [[ "$role_count" = '0' ]]; then
  printf '%s\n' \
    'CREATE ROLE brains_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;' \
    | run_sql
fi

writer_role_count=$("${compose[@]}" exec -T postgres psql -X -A -t -U sage -d memory \
  -c "SELECT count(*) FROM pg_roles WHERE rolname='memory_v5_writer'")
if [[ "$writer_role_count" = '0' ]]; then
  printf '%s\n' \
    'CREATE ROLE memory_v5_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
    | run_sql
fi

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  --schema-only --no-owner --no-privileges | run_sql

# The clone intentionally omits production ACLs. Recreate only the privileges
# that the already-installed relational writer prerequisite supplies in
# production, plus schema usage for the application caller.
printf '%s\n' \
  'GRANT USAGE ON SCHEMA memory TO brains_app, memory_v5_writer;' \
  'GRANT SELECT ON memory.entity TO brains_app;' \
  'GRANT SELECT, INSERT ON memory.entity TO memory_v5_writer;' \
  | run_sql

run_sql < "$migration"
run_sql < "$migration"
run_sql < "$test_sql"
"${compose[@]}" exec -T postgres pg_dump -U sage -d memory --schema-only \
  --schema=memory --no-owner --no-privileges >/dev/null
run_sql < "$rollback"

remaining=$("${compose[@]}" exec -T postgres psql -X -A -t -U sage -d memory \
  -c "SELECT (to_regclass('memory.owner_self_bootstrap_v5') IS NOT NULL)::int + (to_regprocedure('memory.preflight_owner_self_v5()') IS NOT NULL)::int + (to_regprocedure('memory.bootstrap_owner_self_v5(uuid,text)') IS NOT NULL)::int")
if [[ "$remaining" != '0' ]]; then
  echo 'owner self V5 rollback left objects behind' >&2
  exit 1
fi

echo 'memory_v1_owner_self_v5_production_clone: PASS'
