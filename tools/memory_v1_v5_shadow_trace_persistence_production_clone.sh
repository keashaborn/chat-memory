#!/usr/bin/env bash
set -euo pipefail

compose=(docker compose -p memoryv1v5tracepersist -f docker-compose.ci.yml)
migration=ops/sql/20260717_memory_v1_v5_shadow_trace_persistence.sql
rollback=ops/sql/20260717_memory_v1_v5_shadow_trace_persistence_rollback.sql
test_sql=tests/memory_v1_v5_shadow_trace_persistence.sql
backup=$(mktemp /tmp/memory-v1-v5-trace-persistence.XXXXXX.dump)

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
  "CREATE ROLE brains_app LOGIN PASSWORD 'ci_only_brains_password' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;" \
  'CREATE ROLE memory_v5_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"

run_sql <<'SQL'
ALTER FUNCTION memory.current_actor_user_id() OWNER TO memory_v5_writer;
GRANT USAGE ON SCHEMA memory TO brains_app,memory_v5_writer;
SQL

run_sql <"$migration"
run_sql <tests/memory_v1_v5_shadow_trace_persistence_production_rollback.sql
run_sql <"$migration"
run_sql <"$test_sql"

before_rerun=$("${compose[@]}" exec -T postgres psql -X -A -t \
  -U sage -d memory -c 'SELECT count(*) FROM memory.v5_shadow_trace_event')
[[ "$before_rerun" == 2 ]]
run_sql <"$migration"
after_rerun=$("${compose[@]}" exec -T postgres psql -X -A -t \
  -U sage -d memory -c 'SELECT count(*) FROM memory.v5_shadow_trace_event')
[[ "$after_rerun" == "$before_rerun" ]]

run_sql <"$rollback"
[[ $("${compose[@]}" exec -T postgres psql -X -A -t \
  -U sage -d memory -c "SELECT (
    to_regclass('memory.v5_shadow_trace_event') IS NULL
    AND to_regprocedure('memory.record_v5_shadow_trace_v1(text,text,text,text,text,text,text,text,text,text,text,integer,integer,integer,integer,jsonb,integer,integer,integer,text,integer)') IS NULL
    AND to_regrole('memory_v5_trace_writer') IS NULL
  )::int") == 1 ]]

run_sql <"$migration"
"${compose[@]}" build brains
"${compose[@]}" run --rm --no-deps \
  -e POSTGRES_DSN=postgresql://brains_app:ci_only_brains_password@postgres:5432/memory \
  -e PYTHONPATH=/app \
  brains python scripts/memory_v1_v5_shadow_trace_test.py
"${compose[@]}" run --rm --no-deps \
  -e PYTHONPATH=/app \
  brains python scripts/memory_v1_v5_shadow_trace_store_test.py
"${compose[@]}" run --rm --no-deps \
  -e POSTGRES_DSN=postgresql://brains_app:ci_only_brains_password@postgres:5432/memory \
  -e PYTHONPATH=/app \
  brains python scripts/memory_v1_v5_shadow_trace_store_clone_test.py

[[ $("${compose[@]}" exec -T postgres psql -X -A -t \
  -U sage -d memory -c 'SELECT count(*) FROM memory.v5_shadow_trace_event') == 1 ]]
[[ $("${compose[@]}" exec -T postgres psql -X -A -t \
  -U sage -d memory -c "SELECT count(*) FROM information_schema.columns
    WHERE table_schema='memory' AND table_name='v5_shadow_trace_event'
      AND column_name IN ('answer','answer_text','canonical_text','claim','claims',
        'evidence','message','prompt','query','query_preview','system_prompt','text')") == 0 ]]

echo 'memory_v1_v5_shadow_trace_persistence_production_clone: PASS'
