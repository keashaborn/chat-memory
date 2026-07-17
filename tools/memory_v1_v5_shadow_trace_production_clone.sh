#!/usr/bin/env bash
set -euo pipefail

compose=(docker compose -p memoryv1v5shadowtrace -f docker-compose.ci.yml)
backup=$(mktemp /tmp/memory-v1-v5-shadow-trace.XXXXXX.dump)

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
run_sql <ops/sql/20260716_memory_v1_v5_shadow_read_api.sql
run_sql <ops/sql/20260716_memory_v1_v5_reader_status_gate.sql

run_sql <<'SQL'
UPDATE memory.claim
SET status='supported', confidence=0.800
WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
  AND claim_id='50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid;

SELECT 1 / ((
  SELECT count(*)=1
  FROM memory.claim
  WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
    AND claim_id='50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid
    AND status='supported'
)::integer);
SQL

"${compose[@]}" build brains
"${compose[@]}" run --rm --no-deps \
  -e POSTGRES_DSN=postgresql://brains_app:ci_only_brains_password@postgres:5432/memory \
  -e PYTHONPATH=/app \
  brains python scripts/memory_v1_v5_shadow_trace_clone_test.py

echo 'memory_v1_v5_shadow_trace_production_clone: PASS'
