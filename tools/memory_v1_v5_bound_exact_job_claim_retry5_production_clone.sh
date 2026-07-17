#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Tests fifth-attempt compatibility against a disposable
# full production clone. All target claims and test changes are rolled back.

repo_root=$(git rev-parse --show-toplevel)
retry=${1:-5}
case "$retry" in
  5)
    port=${MEMORY_V1_V5_RETRY5_CLAIM_CLONE_PORT:-55459}
    migration=ops/sql/20260717_memory_v1_v5_bound_exact_job_claim_retry5.sql
    rollback=ops/sql/20260717_memory_v1_v5_bound_exact_job_claim_retry5_rollback.sql
    test_sql=tests/memory_v1_v5_bound_exact_job_claim_retry5.sql
    before_function_sha=a694fc38a54422dea43f184f98a3576253b6a0083e47043d85d63c964813c312
    after_function_sha=e67b5bbd08e9eb40283b29ee436f8aec6e3633439d65fb5897d2bc9ef9938b09
    ;;
  6)
    port=${MEMORY_V1_V5_RETRY6_CLAIM_CLONE_PORT:-55460}
    migration=ops/sql/20260717_memory_v1_v5_bound_exact_job_claim_retry6.sql
    rollback=ops/sql/20260717_memory_v1_v5_bound_exact_job_claim_retry6_rollback.sql
    test_sql=tests/memory_v1_v5_bound_exact_job_claim_retry6.sql
    before_function_sha=e67b5bbd08e9eb40283b29ee436f8aec6e3633439d65fb5897d2bc9ef9938b09
    after_function_sha=9768f3cf0d96dcc926a94a3f4d4260e6ada33eb45714851757e0604ce961ef6a
    ;;
  *)
    printf 'unsupported retry ceiling: %s\n' "$retry" >&2
    exit 1
    ;;
esac
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p "memoryv1v5retry${retry}claimclone"
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
backup=$(mktemp "/tmp/memory-v1-v5-retry${retry}-claim.XXXXXX.dump")
tables=$(mktemp "/tmp/memory-v1-v5-retry${retry}-claim-tables.XXXXXX")
before=$(mktemp "/tmp/memory-v1-v5-retry${retry}-claim-before.XXXXXX")
after=$(mktemp "/tmp/memory-v1-v5-retry${retry}-claim-after.XXXXXX")

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
      SELECT count(*)::text || E'\\t' || encode(public.digest(
        convert_to(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'UTF8'),
        'sha256'),'hex')
      FROM (
        SELECT to_jsonb(table_row)::text AS row_json
        FROM \"$schema\".\"$table\" AS table_row
      ) AS rows
    ")
    printf '%s\t%s\t%s\n' "$schema" "$table" "$state" >>"$output"
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
  'CREATE ROLE memory_extraction_retry_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_extraction_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"
printf '%s\n' \
  'GRANT USAGE ON SCHEMA memory TO brains_app;' \
  'GRANT EXECUTE ON FUNCTION memory.current_actor_user_id() TO brains_app;' \
  'GRANT USAGE ON SCHEMA memory TO memory_extraction_worker_maintainer;' \
  'GRANT EXECUTE ON FUNCTION memory.current_actor_user_id() TO memory_extraction_worker_maintainer;' \
  'GRANT SELECT,UPDATE ON memory.evidence_extraction_job TO memory_extraction_worker_maintainer;' \
  'GRANT SELECT,INSERT ON memory.evidence_extraction_event TO memory_extraction_worker_maintainer;' \
  'GRANT SELECT ON memory.evidence,memory.project_thread_binding_event,memory.current_project_thread_binding_v5 TO memory_extraction_worker_maintainer;' \
  'ALTER FUNCTION memory.claim_owner_bound_evidence_job_v5(uuid,uuid,text,uuid,text,text,integer,integer) OWNER TO memory_extraction_worker_maintainer;' \
  'REVOKE ALL ON FUNCTION memory.claim_owner_bound_evidence_job_v5(uuid,uuid,text,uuid,text,text,integer,integer) FROM PUBLIC;' \
  'GRANT EXECUTE ON FUNCTION memory.claim_owner_bound_evidence_job_v5(uuid,uuid,text,uuid,text,text,integer,integer) TO brains_app;' \
  | run_sql

function_sha() {
  scalar "SELECT encode(public.digest(convert_to(pg_get_functiondef(
    'memory.claim_owner_bound_evidence_job_v5(uuid,uuid,text,uuid,text,text,integer,integer)'::regprocedure
  ),'UTF8'),'sha256'),'hex')"
}

[[ "$(function_sha)" == "$before_function_sha" ]]
scalar "SELECT table_schema || E'\\t' || table_name
  FROM information_schema.tables
  WHERE table_type='BASE TABLE' AND table_schema IN ('memory','public')
  ORDER BY table_schema,table_name" >"$tables"
capture_state "$before"

run_sql <"$repo_root/$migration"
PGPASSWORD=clone_only_brains_password psql -X -v ON_ERROR_STOP=1 \
  -h 127.0.0.1 -p "$port" -U brains_app -d memory \
  -f "$repo_root/$test_sql" >/dev/null
run_sql <"$repo_root/$rollback"
[[ "$(function_sha)" == "$before_function_sha" ]]
run_sql <"$repo_root/$migration"
[[ "$(function_sha)" == "$after_function_sha" ]]

capture_state "$after"
cmp -s "$before" "$after"
printf 'memory_v1_v5_bound_exact_job_claim_retry%s_production_clone: PASS\n' "$retry"
