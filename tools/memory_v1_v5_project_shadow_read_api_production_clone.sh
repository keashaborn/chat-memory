#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores a production snapshot into an isolated
# disposable Postgres instance, then tests the project/component binding and
# project shadow reader entirely outside production.

compose=(docker compose -p memoryv1v5projectshadow -f docker-compose.ci.yml)
migration=ops/sql/20260718_memory_v1_v5_project_shadow_read_api.sql
rollback=ops/sql/20260718_memory_v1_v5_project_shadow_read_api_rollback.sql
test_sql=tests/memory_v1_v5_project_shadow_read_api.sql
backup=$(mktemp /tmp/memory-v1-v5-project-shadow.XXXXXX.dump)

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
ALTER FUNCTION memory.require_v5_reader_context() OWNER TO memory_v5_reader;
ALTER FUNCTION memory.apply_owner_project_thread_binding_v5(
  uuid,uuid,uuid,text,text
) OWNER TO memory_v5_extraction_maintainer;
GRANT USAGE ON SCHEMA memory
  TO brains_app,memory_v5_writer,memory_v5_reader,
     memory_v5_extraction_maintainer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_v5_reader,memory_v5_extraction_maintainer;
GRANT SELECT ON memory.project_thread_binding_event
  TO memory_v5_reader,memory_v5_extraction_maintainer;
SQL

run_sql <"$migration"
run_sql <"$migration"
run_sql <"$test_sql"

owner_state=$("${compose[@]}" exec -T postgres psql -X -A -t -F '|' \
  -U sage -d memory -c "
    SELECT
      pg_get_userbyid((SELECT proowner FROM pg_proc WHERE oid=
        'memory.read_v5_shadow_project_knowledge(uuid,integer)'::regprocedure)),
      pg_get_userbyid((SELECT proowner FROM pg_proc WHERE oid=
        'memory.apply_owner_project_thread_component_binding_v5(uuid,uuid,uuid,uuid,uuid,uuid,text,text,text)'::regprocedure));
  ")
[[ "$owner_state" == 'memory_v5_reader|memory_v5_extraction_maintainer' ]]

run_sql <"$rollback"
[[ $("${compose[@]}" exec -T postgres psql -X -A -t -U sage -d memory \
  -c "SELECT (
    to_regprocedure('memory.read_v5_shadow_project_knowledge(uuid,integer)') IS NULL
    AND to_regprocedure('memory.apply_owner_project_thread_component_binding_v5(uuid,uuid,uuid,uuid,uuid,uuid,text,text,text)') IS NULL
    AND to_regclass('memory.project_thread_component_binding_event_v5') IS NULL
  )::int") == 1 ]]

echo 'memory_v1_v5_project_shadow_read_api_production_clone: PASS'
