#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Exercises compiler-fingerprinted circuit breaking on a
# disposable production clone. All fixture rows are transactionally rolled back.

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_LOCAL_CIRCUIT_CLONE_PORT:-55477}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose -p memoryv1v5localcircuitclone
  -f docker-compose.ci.yml -f docker-compose.stage-batch-clone.yml
)
migration=ops/sql/20260722_memory_v1_v5_local_circuit_fingerprint.sql
rollback=ops/sql/20260722_memory_v1_v5_local_circuit_fingerprint_rollback.sql
test_sql=tests/memory_v1_v5_local_circuit_fingerprint.sql
expected_migration_sha=aedf946bfef685e02680dfd128a20cd662b0ec6b694e6ccfad0adecded2fcfd6
expected_rollback_sha=2b2f12d7092cbe6b66b7a82c95029cc7e0340dd9fb92a71f72cdf660add985c7
expected_test_sha=650af54da557562d9373d9287cf006225b43447c60a466f3150c4999d423debf

backup=$(mktemp /tmp/memory-v1-v5-local-circuit.XXXXXX.dump)
tables=$(mktemp /tmp/memory-v1-v5-local-circuit-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-local-circuit-before.XXXXXX)
after=$(mktemp /tmp/memory-v1-v5-local-circuit-after.XXXXXX)
cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$tables" "$before" "$after"
}
trap cleanup EXIT
chmod 0600 "$backup" "$tables" "$before" "$after"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory
}
scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}
capture_state() {
  local output=$1 schema table state
  : >"$output"
  while IFS=$'\t' read -r schema table; do
    state=$(scalar "SELECT count(*)::text || E'\\t' ||
      encode(public.digest(convert_to(coalesce(string_agg(row_value,E'\\n'
        ORDER BY row_value),''),'UTF8'),'sha256'),'hex')
      FROM (SELECT to_jsonb(value)::text AS row_value
        FROM \"$schema\".\"$table\" AS value) AS rows")
    printf '%s\t%s\t%s\n' "$schema" "$table" "$state" >>"$output"
  done <"$tables"
}

for required in "$migration" "$rollback" "$test_sql"; do
  [[ -f "$repo_root/$required" ]]
done
[[ "$(sha256sum "$repo_root/$migration" | cut -d' ' -f1)" == "$expected_migration_sha" ]]
[[ "$(sha256sum "$repo_root/$rollback" | cut -d' ' -f1)" == "$expected_rollback_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | cut -d' ' -f1)" == "$expected_test_sha" ]]

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc --no-owner >"$backup"
[[ -s "$backup" ]]
"${compose[@]}" up -d --wait postgres
printf '%s\n' \
  "CREATE ROLE brains_app LOGIN PASSWORD 'clone_only_brains_password' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;" \
  'CREATE ROLE lifeswitch_training_observation_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_evidence_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_extraction_queue_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_extraction_retry_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_extraction_worker_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_intake_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_review_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_epistemic_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_extraction_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_extraction_scheduler_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_disposition_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_entailment_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_entity_validation_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_inference_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_projection_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_reextract_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_review_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_supersession_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_reviewed_stage_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_trace_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner <"$backup"
printf '%s\n' \
  'GRANT USAGE ON SCHEMA memory TO brains_app;' \
  'GRANT EXECUTE ON FUNCTION memory.current_actor_user_id() TO brains_app;' \
  | run_sql

scalar "SELECT table_schema || E'\\t' || table_name
  FROM information_schema.tables WHERE table_type='BASE TABLE'
    AND table_schema='memory' ORDER BY table_schema,table_name" >"$tables"
capture_state "$before"
function_before=$(scalar "SELECT encode(public.digest(convert_to(
  pg_get_functiondef(
    'memory.claim_owner_v5_local_inference_job_v1(uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,integer,integer,integer)'::regprocedure
  ),'UTF8'),'sha256'),'hex')")
[[ "$function_before" == 9d10f1507b2a619ad011c7033076af8c52b811fc3cdaae5c2f9b79a6f3fb56fb ]]

run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$test_sql"
capture_state "$after"
cmp -s "$before" "$after"
run_sql <"$repo_root/$rollback"
function_after=$(scalar "SELECT encode(public.digest(convert_to(
  pg_get_functiondef(
    'memory.claim_owner_v5_local_inference_job_v1(uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,integer,integer,integer)'::regprocedure
  ),'UTF8'),'sha256'),'hex')")
[[ "$function_after" == "$function_before" ]]

printf '%s\n' 'memory_v1_v5_local_circuit_fingerprint_production_clone: PASS'
