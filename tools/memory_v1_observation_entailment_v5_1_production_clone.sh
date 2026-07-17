#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores production into a disposable Postgres container,
# installs the V5.1 observation-entailment gate, and rolls every test write back.

compose=(docker compose -p memoryv1observationentailment -f docker-compose.ci.yml)
migration=ops/sql/20260716_memory_v1_observation_entailment_v5_1.sql
rollback=ops/sql/20260716_memory_v1_observation_entailment_v5_1_rollback.sql
test_sql=tests/memory_v1_observation_entailment_v5_1.sql
backup=$(mktemp /tmp/memory-v1-observation-entailment.XXXXXX.dump)

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
ALTER FUNCTION memory.guard_projection_actor_v5() OWNER TO memory_v5_writer;
ALTER FUNCTION memory.guard_projection_plan_complete_v5()
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.guard_projection_item_complete_v5()
  OWNER TO memory_v5_writer;

GRANT USAGE ON SCHEMA memory TO brains_app,memory_v5_writer;
GRANT SELECT ON
  memory.observation,
  memory.evidence,
  memory.relational_operation_request,
  memory.projection_plan_observation
TO memory_v5_writer;
GRANT INSERT ON
  memory.relational_operation_request,
  memory.projection_plan_observation
TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.v5_sha256_valid(text)
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.v5_reason_codes_valid(jsonb,integer)
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.v5_source_spans_valid(jsonb)
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.guard_v5_append_only()
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.guard_projection_actor_v5()
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.guard_projection_plan_complete_v5()
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.guard_projection_item_complete_v5()
  TO memory_v5_writer;
SQL

run_sql <"$migration"
run_sql <"$migration"
run_sql <"$test_sql"

security_state=$("${compose[@]}" exec -T postgres psql -X -A -t -F '|' \
  -U sage -d memory -c "
    SELECT
      pg_get_userbyid(class.relowner),
      class.relrowsecurity,
      class.relforcerowsecurity,
      has_table_privilege(
        'brains_app','memory.observation_entailment_v5','SELECT'
      ),
      has_table_privilege(
        'brains_app','memory.observation_entailment_v5','INSERT'
      ),
      pg_get_userbyid(preflight.proowner),
      pg_get_userbyid(recorder.proowner),
      pg_get_userbyid(gate.proowner),
      EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgrelid='memory.projection_plan_observation'::regclass
          AND tgname='projection_plan_observation_entailment_guard'
          AND tgenabled='O' AND NOT tgisinternal
      )
    FROM pg_class AS class
    CROSS JOIN pg_proc AS preflight
    CROSS JOIN pg_proc AS recorder
    CROSS JOIN pg_proc AS gate
    WHERE class.oid='memory.observation_entailment_v5'::regclass
      AND preflight.oid=
        'memory.preflight_observation_entailment_v5(
          uuid,memory.observation_entailment_decision_v5,
          text,jsonb,text,text
        )'::regprocedure
      AND recorder.oid=
        'memory.record_observation_entailment_v5(
          uuid,uuid,memory.observation_entailment_decision_v5,
          text,jsonb,text,text,text
        )'::regprocedure
      AND gate.oid=
        'memory.guard_projection_observation_entailment_v5()'::regprocedure;
  ")
[[ "$security_state" == \
'memory_v5_writer|t|t|f|f|memory_v5_writer|memory_v5_writer|memory_v5_writer|t' ]]

run_sql <"$rollback"
[[ $("${compose[@]}" exec -T postgres psql -X -A -t -U sage -d memory \
  -c "SELECT (
    to_regclass('memory.observation_entailment_v5') IS NULL
    AND to_regtype('memory.observation_entailment_decision_v5') IS NULL
    AND NOT EXISTS (
      SELECT 1 FROM pg_proc AS procedure
      JOIN pg_namespace AS namespace ON namespace.oid=procedure.pronamespace
      WHERE namespace.nspname='memory'
        AND procedure.proname IN (
          'preflight_observation_entailment_v5',
          'record_observation_entailment_v5',
          'observation_entailment_allows_projection_v5',
          'guard_projection_observation_entailment_v5',
          'v5_source_spans_match_text',
          'v5_source_spans_cover'
        )
    )
    AND NOT EXISTS (
      SELECT 1 FROM pg_trigger
      WHERE tgrelid='memory.projection_plan_observation'::regclass
        AND tgname='projection_plan_observation_entailment_guard'
        AND NOT tgisinternal
    )
    AND pg_get_constraintdef((
      SELECT oid FROM pg_constraint
      WHERE conrelid='memory.relational_operation_request'::regclass
        AND conname='relational_operation_request_operation_check'
    ))=
      'CHECK ((operation = ANY (ARRAY[''stage_packet''::text, ''review_resolution''::text, ''apply_resolution''::text, ''review_claim_assessment_v5''::text, ''apply_claim_assessment_v5''::text])))'
  )::int") == 1 ]]

echo 'memory_v1_observation_entailment_v5_1_production_clone: PASS'
