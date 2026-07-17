#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs and tests reviewed skipped-job retry on a
# disposable full production clone. Every test row change is rolled back.

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_REVIEWED_RETRY_CLONE_PORT:-55453}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v5reviewedretryclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
migration=ops/sql/20260717_memory_v1_v5_reviewed_retry.sql
rollback=ops/sql/20260717_memory_v1_v5_reviewed_retry_rollback.sql
test_sql=tests/memory_v1_v5_reviewed_retry.sql

backup=$(mktemp /tmp/memory-v1-v5-reviewed-retry.XXXXXX.dump)
tables=$(mktemp /tmp/memory-v1-v5-reviewed-retry-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-reviewed-retry-before.XXXXXX)
after=$(mktemp /tmp/memory-v1-v5-reviewed-retry-after.XXXXXX)

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
  local output=$1
  : >"$output"
  while IFS=$'\t' read -r schema_name table; do
    state=$(scalar "
      SELECT count(*)::text || E'\\t' || encode(
        public.digest(
          convert_to(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'UTF8'),
          'sha256'
        ),
        'hex'
      )
      FROM (
        SELECT to_jsonb(table_row)::text AS row_json
        FROM \"$schema_name\".\"$table\" AS table_row
      ) AS rows
    ")
    printf '%s\t%s\t%s\n' "$schema_name" "$table" "$state" >>"$output"
  done <"$tables"
}

for required in "$migration" "$rollback" "$test_sql"; do
  [[ -f "$repo_root/$required" ]]
done

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc --no-owner --no-privileges >"$backup"
[[ -s "$backup" ]]

"${compose[@]}" up -d --wait postgres
printf '%s\n' \
  "CREATE ROLE brains_app LOGIN PASSWORD 'clone_only_brains_password' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;" \
  'CREATE ROLE memory_evidence_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_review_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_trace_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_intake_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_extraction_queue_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_extraction_worker_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_extraction_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"
printf '%s\n' \
  'GRANT USAGE ON SCHEMA memory TO brains_app;' \
  'GRANT EXECUTE ON FUNCTION memory.current_actor_user_id() TO brains_app;' \
  'GRANT SELECT ON memory.evidence_extraction_job,memory.evidence_extraction_event,memory.evidence_extraction_packet_v5,memory.evidence,memory.project_thread_binding_event,memory.current_project_thread_binding_v5 TO brains_app;' \
  | run_sql

run_sql <"$repo_root/$migration"

scalar "
  SELECT table_schema || E'\\t' || table_name
  FROM information_schema.tables
  WHERE table_type='BASE TABLE'
    AND table_schema IN ('memory','public')
  ORDER BY table_schema,table_name
" >"$tables"
capture_state "$before"
run_sql <"$repo_root/$test_sql"
capture_state "$after"
cmp -s "$before" "$after"

run_sql <"$repo_root/$rollback"
[[ "$(scalar "SELECT to_regprocedure('memory.requeue_owner_skipped_evidence_job_v5(uuid,uuid,text,uuid,uuid,text,integer,text)') IS NULL")" == t ]]
[[ "$(scalar "SELECT to_regrole('memory_extraction_retry_maintainer') IS NULL")" == t ]]
[[ "$(scalar "SELECT position('OR (OLD.status=''skipped'' AND NEW.status=''pending'')' in pg_get_functiondef('memory.guard_evidence_extraction_job_update()'::regprocedure))=0")" == t ]]

printf '%s\n' 'memory_v1_v5_reviewed_retry_production_clone: PASS'
