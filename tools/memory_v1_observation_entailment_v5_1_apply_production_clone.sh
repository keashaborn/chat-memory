#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores production into a disposable database, repairs
# omitted ownership/ACL metadata, and proves the exact two-decision apply.

compose=(docker compose -p memoryv1observationentailmentapply -f docker-compose.ci.yml)
migration=ops/sql/20260716_memory_v1_observation_entailment_v5_1.sql
apply_script=tools/memory_v1_observation_entailment_v5_1_initial_apply.sh
backup=$(mktemp /tmp/memory-v1-observation-entailment-apply.XXXXXX.dump)
snapshot_dir=$(mktemp -d /tmp/memory-v1-observation-entailment-apply.XXXXXX)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup"
  rm -rf "$snapshot_dir"
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
SQL

# Reapplication restores the exact owners and least-privilege grants for the
# V5.1 objects omitted by --no-owner/--no-privileges.
run_sql <"$migration"

MEMORY_V1_OBSERVATION_ENTAILMENT_APPLY=authorized \
MEMORY_V1_DB_CONTAINER=memoryv1observationentailmentapply-postgres-1 \
MEMORY_V1_SNAPSHOT_DIR="$snapshot_dir" \
MEMORY_V1_APPLY_LOCK_FILE="$snapshot_dir/apply.lock" \
bash "$apply_script"

[[ $("${compose[@]}" exec -T postgres psql -X -A -t -U sage -d memory \
  -c "SELECT (
    (SELECT count(*) FROM memory.observation_entailment_v5
      WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
        AND observation_id IN (
          '9bf1e6b2-1840-4524-98dc-142567ebe013'::uuid,
          '93024235-89a8-49d5-88fa-7e4a143b68f3'::uuid
        ))=2
    AND
    (SELECT count(*) FROM memory.relational_operation_request
      WHERE request_id IN (
        '42000000-0000-4000-8000-000000000001'::uuid,
        '42000000-0000-4000-8000-000000000002'::uuid
      )
      AND operation='record_observation_entailment_v5')=2
  )::int") == 1 ]]

echo 'memory_v1_observation_entailment_v5_1_apply_production_clone: PASS'
