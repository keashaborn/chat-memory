#!/usr/bin/env bash
set -euo pipefail

compose=(docker compose -p memoryv1readerstatusgate -f docker-compose.ci.yml)
migration=ops/sql/20260716_memory_v1_v5_reader_status_gate.sql
rollback=ops/sql/20260716_memory_v1_v5_reader_status_gate_rollback.sql
test_sql=tests/memory_v1_v5_reader_status_gate.sql
backup=$(mktemp /tmp/memory-v1-reader-status-gate.XXXXXX.dump)

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
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"

run_sql <<'SQL'
ALTER FUNCTION memory.current_actor_user_id() OWNER TO memory_v5_writer;
ALTER FUNCTION memory.require_v5_reader_context()
  OWNER TO memory_v5_reader;
ALTER FUNCTION memory.read_v5_shadow_claims(uuid[])
  OWNER TO memory_v5_reader;
GRANT USAGE ON SCHEMA memory TO brains_app,memory_v5_reader;
GRANT SELECT ON
  memory.claim,memory.claim_observation,memory.evidence,
  memory.observation,memory.observation_temporal,
  memory.projection_apply_event
TO memory_v5_reader;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_v5_reader;
GRANT EXECUTE ON FUNCTION memory.read_v5_shadow_claims(uuid[])
  TO brains_app;
SQL

run_sql <"$migration"
run_sql <"$migration"
run_sql <"$test_sql"
run_sql <"$rollback"

[[ $("${compose[@]}" exec -T postgres psql -X -A -t -U sage -d memory \
  -c "SELECT (
    NOT EXISTS (
      SELECT 1 FROM pg_policies
      WHERE schemaname='memory' AND tablename='claim'
        AND policyname='surfaceable_status_v5_reader'
    )
    AND (SELECT count(*) FROM memory.claim
      WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
        AND claim_id='50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid
        AND status='retracted' AND confidence=0)=1
  )::int") == 1 ]]

echo 'memory_v1_v5_reader_status_gate_production_clone: PASS'
