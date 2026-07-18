#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Tests sanitized project shadow trace persistence on a
# disposable production clone and destroys the clone.

compose=(docker compose -p memoryv1v5projecttrace -f docker-compose.ci.yml)
migration=ops/sql/20260718_memory_v1_v5_project_shadow_trace_persistence.sql
rollback=ops/sql/20260718_memory_v1_v5_project_shadow_trace_persistence_rollback.sql
test_sql=tests/memory_v1_v5_project_shadow_trace_persistence.sql
backup=$(mktemp /tmp/memory-v1-v5-project-trace.XXXXXX.dump)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup"
}
trap cleanup EXIT
chmod 0600 "$backup"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory
}

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc --no-owner --no-privileges >"$backup"
[[ -s "$backup" ]]
"${compose[@]}" up -d --wait postgres
printf '%s\n' \
  'CREATE ROLE brains_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_trace_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_extraction_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"

run_sql <<'SQL'
ALTER FUNCTION memory.current_actor_user_id() OWNER TO memory_v5_writer;
ALTER FUNCTION memory.require_v5_shadow_trace_writer_context()
  OWNER TO memory_v5_trace_writer;
ALTER FUNCTION memory.v5_shadow_trace_sha256_valid(text)
  OWNER TO memory_v5_trace_writer;
ALTER FUNCTION memory.v5_shadow_rejection_counts_valid(jsonb)
  OWNER TO memory_v5_trace_writer;
GRANT USAGE ON SCHEMA memory TO brains_app,memory_v5_trace_writer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_v5_trace_writer;
SQL

run_sql <"$migration"
run_sql <"$migration"
run_sql <"$test_sql"

run_sql <"$rollback"
[[ $("${compose[@]}" exec -T postgres psql -X -A -t -U sage -d memory \
  -c "SELECT (
    to_regprocedure('memory.record_v5_project_shadow_trace_v1(text,text,text,text,text,text,text,text,text,text,text,text,integer,integer,integer,jsonb,integer,integer)') IS NULL
    AND to_regclass('memory.v5_project_shadow_trace_event') IS NULL
  )::int") == 1 ]]

echo 'memory_v1_v5_project_shadow_trace_persistence_production_clone: PASS'
