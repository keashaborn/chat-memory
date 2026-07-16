#!/usr/bin/env bash
set -euo pipefail

compose=(docker compose -p memoryv1deferredscanner -f docker-compose.ci.yml)
reconciliation_migration=ops/sql/20260716_memory_v1_deferred_entailment_reconciliation_v5.sql
migration=ops/sql/20260716_memory_v1_deferred_reconciliation_scanner_v5.sql
rollback=ops/sql/20260716_memory_v1_deferred_reconciliation_scanner_v5_rollback.sql
test_sql=tests/memory_v1_deferred_reconciliation_scanner_v5.sql
backup=$(mktemp /tmp/memory-v1-deferred-scanner.XXXXXX.dump)

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
ALTER FUNCTION memory.claim_assessment_state_v5(uuid)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_claim_assessment_review_v5(
  uuid,memory.claim_assessment_action_v5,numeric,numeric,numeric,numeric,
  jsonb,text,text,text
) OWNER TO memory_v5_writer;
GRANT USAGE ON SCHEMA memory TO brains_app,memory_v5_writer;
GRANT USAGE ON TYPE
  memory.claim_status,memory.evidence_stance,
  memory.claim_assessment_action_v5,
  memory.observation_entailment_decision_v5
TO memory_v5_writer;
GRANT SELECT ON
  memory.claim,memory.claim_revision,memory.claim_assessment,
  memory.claim_observation,memory.observation,memory.observation_temporal,
  memory.evidence,memory.projection_apply_event,
  memory.claim_assessment_review_v5,memory.claim_assessment_apply_v5,
  memory.observation_entailment_v5,memory.relational_operation_request,
  memory.claim_entailment_reconciliation_v5
TO memory_v5_writer;
GRANT INSERT ON memory.claim_entailment_reconciliation_v5
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.v5_sha256_valid(text)
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.v5_reason_codes_valid(jsonb,integer)
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.guard_v5_append_only()
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.preflight_claim_assessment_review_v5(
  uuid,memory.claim_assessment_action_v5,numeric,numeric,numeric,numeric,
  jsonb,text,text,text
) TO memory_v5_writer;
SQL

run_sql <"$reconciliation_migration"
run_sql <"$migration"
run_sql <"$migration"
run_sql <"$test_sql"

[[ $("${compose[@]}" exec -T postgres psql -X -A -t -U sage -d memory \
  -c "SELECT (
    pg_get_userbyid((
      SELECT proowner FROM pg_proc
      WHERE oid='memory.scan_deferred_entailment_reconciliation_v5(integer)'::regprocedure
    ))='memory_v5_writer'
    AND has_function_privilege(
      'brains_app',
      'memory.scan_deferred_entailment_reconciliation_v5(integer)',
      'EXECUTE'
    )
    AND NOT has_function_privilege(
      'brains_app',
      'memory.v5_deferred_support_state_eligible(jsonb)',
      'EXECUTE'
    )
  )::int") == 1 ]]

run_sql <"$rollback"
[[ $("${compose[@]}" exec -T postgres psql -X -A -t -U sage -d memory \
  -c "SELECT (
    to_regprocedure(
      'memory.scan_deferred_entailment_reconciliation_v5(integer)'
    ) IS NULL
    AND to_regprocedure(
      'memory.v5_deferred_support_state_eligible(jsonb)'
    ) IS NULL
    AND (SELECT count(*) FROM memory.claim_entailment_reconciliation_v5)=1
    AND (SELECT count(*) FROM memory.claim
      WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
        AND claim_id='50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid
        AND status='retracted' AND confidence=0)=1
  )::int") == 1 ]]

echo 'memory_v1_deferred_reconciliation_scanner_v5_production_clone: PASS'
