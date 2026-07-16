#!/usr/bin/env bash
set -euo pipefail

compose=(docker compose -p memoryv1v5claimassessment -f docker-compose.ci.yml)
migration=ops/sql/20260716_memory_v1_v5_claim_assessment.sql
rollback=ops/sql/20260716_memory_v1_v5_claim_assessment_rollback.sql
test_sql=tests/memory_v1_v5_claim_assessment.sql
backup=$(mktemp /tmp/memory-v1-v5-claim-assessment.XXXXXX.dump)

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
ALTER FUNCTION memory.require_v5_writer_context() OWNER TO memory_v5_writer;
ALTER FUNCTION memory.v5_digest_text(text) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.v5_canonical_json_text(jsonb) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.guard_v5_append_only() OWNER TO memory_v5_writer;
GRANT USAGE ON SCHEMA memory TO brains_app,memory_v5_writer;
GRANT USAGE ON TYPE
  memory.claim_status,
  memory.evidence_stance
TO memory_v5_writer;
GRANT SELECT ON
  memory.claim,
  memory.claim_revision,
  memory.claim_assessment,
  memory.claim_observation,
  memory.observation,
  memory.observation_temporal,
  memory.evidence,
  memory.projection_apply_event,
  memory.relational_operation_request
TO memory_v5_writer;
GRANT INSERT ON
  memory.claim_revision,
  memory.claim_assessment,
  memory.relational_operation_request
TO memory_v5_writer;
GRANT UPDATE ON memory.claim TO memory_v5_writer;
GRANT SELECT,INSERT,UPDATE,DELETE ON
  memory.claim,
  memory.claim_revision,
  memory.claim_assessment
TO brains_app;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.v5_sha256_valid(text)
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.v5_reason_codes_valid(jsonb,integer)
  TO memory_v5_writer;
SQL

run_sql <"$migration"
run_sql <"$migration"
run_sql <"$test_sql"

owner_state=$("${compose[@]}" exec -T postgres psql -X -A -t -F '|' \
  -U sage -d memory -c "
    SELECT
      pg_get_userbyid((SELECT relowner FROM pg_class
        WHERE oid='memory.claim_assessment_review_v5'::regclass)),
      pg_get_userbyid((SELECT relowner FROM pg_class
        WHERE oid='memory.claim_assessment_apply_v5'::regclass)),
      pg_get_userbyid((SELECT proowner FROM pg_proc
        WHERE oid='memory.apply_claim_assessment_v5(uuid,uuid,uuid,text)'::regprocedure));
  ")
[[ "$owner_state" == 'memory_v5_writer|memory_v5_writer|memory_v5_writer' ]]

run_sql <"$rollback"
[[ $("${compose[@]}" exec -T postgres psql -X -A -t -U sage -d memory \
  -c "SELECT (
    to_regclass('memory.claim_assessment_review_v5') IS NULL
    AND to_regclass('memory.claim_assessment_apply_v5') IS NULL
    AND to_regprocedure('memory.claim_assessment_state_v5(uuid)') IS NULL
    AND to_regprocedure(
      'memory.apply_claim_assessment_v5(uuid,uuid,uuid,text)'
    ) IS NULL
  )::int") == 1 ]]

echo 'memory_v1_v5_claim_assessment_production_clone: PASS'
