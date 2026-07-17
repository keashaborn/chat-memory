#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs and tests the exact-job canary lease on a
# disposable full production clone. The test transaction is rolled back.

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_BOUND_EXACT_CLAIM_CLONE_PORT:-55452}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v5boundexactclaimclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)

worker_migration=ops/sql/20260716_memory_v1_evidence_extraction_worker.sql
bounded_migration=ops/sql/20260717_memory_v1_v5_bounded_extraction.sql
component_migration=ops/sql/20260717_memory_v1_v5_project_components.sql
migration=ops/sql/20260717_memory_v1_v5_bound_exact_job_claim.sql
rollback=ops/sql/20260717_memory_v1_v5_bound_exact_job_claim_rollback.sql
test_sql=tests/memory_v1_v5_bound_exact_job_claim.sql

backup=$(mktemp /tmp/memory-v1-v5-bound-exact-claim.XXXXXX.dump)
tables=$(mktemp /tmp/memory-v1-v5-bound-exact-claim-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-bound-exact-claim-before.XXXXXX)
after=$(mktemp /tmp/memory-v1-v5-bound-exact-claim-after.XXXXXX)

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
  while IFS=$'\t' read -r schema table; do
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
        FROM \"$schema\".\"$table\" AS table_row
      ) AS rows
    ")
    printf '%s\t%s\t%s\n' "$schema" "$table" "$state" >>"$output"
  done <"$tables"
}

for required in "$worker_migration" "$bounded_migration" \
  "$component_migration" "$migration" "$rollback" "$test_sql"; do
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
  | run_sql

run_sql <"$repo_root/$worker_migration"
run_sql <"$repo_root/$bounded_migration"
run_sql <"$repo_root/$component_migration"
run_sql <"$repo_root/$migration"

scalar "SELECT table_schema || E'\\t' || table_name FROM information_schema.tables WHERE table_type='BASE TABLE' AND table_schema IN ('memory','public') ORDER BY table_schema,table_name" >"$tables"
capture_state "$before"
run_sql <"$repo_root/$test_sql"
capture_state "$after"
cmp -s "$before" "$after"

run_sql <"$repo_root/$rollback"
[[ "$(scalar "SELECT to_regprocedure('memory.claim_owner_bound_evidence_job_v5(uuid,uuid,text,uuid,text,text,integer,integer)') IS NULL")" == t ]]

printf '%s\n' 'memory_v1_v5_bound_exact_job_claim_production_clone: PASS'
