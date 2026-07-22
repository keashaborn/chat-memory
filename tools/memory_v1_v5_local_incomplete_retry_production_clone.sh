#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs and exercises the bounded incomplete-response
# retry on a disposable production clone. Every fixture write is rolled back.

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_LOCAL_INCOMPLETE_RETRY_CLONE_PORT:-55475}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v5localincompleteretryclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
migration=ops/sql/20260722_memory_v1_v5_local_incomplete_retry.sql
rollback=ops/sql/20260722_memory_v1_v5_local_incomplete_retry_rollback.sql
test_sql=tests/memory_v1_v5_local_incomplete_retry.sql
expected_migration_sha=92644619ff8337dfe9d8b9607878f5d4a38c20dfa51ccc2bf0ac5d1f048ed104
expected_rollback_sha=78a99749106f534eeea8936d84a2346ac6a942eba1a89e4874ff8e8ca43757a2
expected_test_sha=b0cb9b454af318bfb93f4d067ac82c3d51d840b95cd38655c30f18d74290b4b1

backup=$(mktemp /tmp/memory-v1-v5-local-incomplete-retry.XXXXXX.dump)
tables=$(mktemp /tmp/memory-v1-v5-local-incomplete-retry-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-local-incomplete-retry-before.XXXXXX)
after=$(mktemp /tmp/memory-v1-v5-local-incomplete-retry-after.XXXXXX)

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
    state=$(scalar "
      SELECT count(*)::text || E'\\t' || encode(public.digest(convert_to(
        coalesce(string_agg(row_value,E'\\n' ORDER BY row_value),''),
        'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_value
        FROM \"$schema\".\"$table\" AS value
      ) AS rows
    ")
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

scalar "
  SELECT table_schema || E'\\t' || table_name
  FROM information_schema.tables
  WHERE table_type='BASE TABLE' AND table_schema='memory'
  ORDER BY table_schema,table_name
" >"$tables"
capture_state "$before"

run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$test_sql"
capture_state "$after"
cmp -s "$before" "$after"

run_sql <"$repo_root/$rollback"
[[ "$(scalar "SELECT (to_regprocedure(
  'memory.requeue_owner_local_incomplete_failure_v1(uuid,uuid,text,uuid,uuid,integer,text)'
) IS NULL)::integer")" == 1 ]]

printf '%s\n' 'memory_v1_v5_local_incomplete_retry_production_clone: PASS'
