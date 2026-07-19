#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores a disposable production clone and proves the
# compiler-retry v2 function with rollback-only fixtures. No model is called.

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_LOCAL_COMPILER_RETRY_V2_CLONE_PORT:-55465}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v5localcompilerretryv2clone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
migration=ops/sql/20260719_memory_v1_v5_local_compiler_retry_v2.sql
rollback=ops/sql/20260719_memory_v1_v5_local_compiler_retry_v2_rollback.sql
test_sql=tests/memory_v1_v5_local_compiler_retry_v2.sql
expected_migration_sha=8d767af4bd2c947b20d87ed8589b8154ae77f6d58856dd2aee00db9a56d2ac15
expected_rollback_sha=f9aea422ab28603ac25f2df94a17c3110fb4ce7847f2bd79f94ec89b29d9ece3
expected_test_sha=8aed8ef9ce462baaa3f1b553f82cbff6ff2e65f7f856bc02cd1c415eec0ff5da

backup=$(mktemp /tmp/memory-v1-v5-local-compiler-retry-v2.XXXXXX.dump)
tables=$(mktemp /tmp/memory-v1-v5-local-compiler-retry-v2-tables.XXXXXX.txt)
before=$(mktemp /tmp/memory-v1-v5-local-compiler-retry-v2-before.XXXXXX.tsv)
after_test=$(mktemp /tmp/memory-v1-v5-local-compiler-retry-v2-after-test.XXXXXX.tsv)
after_rollback=$(mktemp /tmp/memory-v1-v5-local-compiler-retry-v2-after-rollback.XXXXXX.tsv)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$tables" "$before" "$after_test" "$after_rollback"
}
trap cleanup EXIT
chmod 0600 "$backup" "$tables" "$before" "$after_test" "$after_rollback"

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
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
[[ "$(sha256sum "$repo_root/$migration" | cut -d' ' -f1)" == "$expected_migration_sha" ]]
[[ "$(sha256sum "$repo_root/$rollback" | cut -d' ' -f1)" == "$expected_rollback_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | cut -d' ' -f1)" == "$expected_test_sha" ]]

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc --no-owner >"$backup"
[[ -s "$backup" ]]

"${compose[@]}" up -d --wait postgres
printf '%s\n' \
  "CREATE ROLE brains_app LOGIN PASSWORD 'clone_only_brains_password' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;" \
  'CREATE ROLE memory_evidence_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_extraction_queue_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_extraction_retry_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_extraction_worker_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_intake_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_review_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_extraction_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_extraction_scheduler_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_disposition_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_entailment_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_entity_validation_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_inference_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_projection_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_review_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
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

[[ "$(scalar "SELECT (
  to_regprocedure(
    'memory.requeue_owner_local_compiler_failure_v2(uuid,uuid,text,uuid,uuid,integer,text,text,text)'
  ) IS NOT NULL
)::integer")" == 1 ]]
capture_state "$after_test"
cmp -s "$before" "$after_test"

run_sql <"$repo_root/$rollback"
[[ "$(scalar "SELECT (
  to_regprocedure(
    'memory.requeue_owner_local_compiler_failure_v2(uuid,uuid,text,uuid,uuid,integer,text,text,text)'
  ) IS NULL
  AND to_regprocedure(
    'memory.requeue_owner_local_validation_failure_v1(uuid,uuid,text,uuid,uuid,integer,text,text)'
  ) IS NOT NULL
)::integer")" == 1 ]]
capture_state "$after_rollback"
cmp -s "$before" "$after_rollback"

printf '%s\n' 'memory_v1_v5_local_compiler_retry_v2_production_clone: PASS'
