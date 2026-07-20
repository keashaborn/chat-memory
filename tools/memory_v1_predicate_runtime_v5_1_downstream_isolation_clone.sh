#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores production into disposable Docker, installs the
# V5/V5.1 downstream-isolation gate, and proves zero persistent row changes.

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_1_ISOLATION_CLONE_PORT:-55462}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v51isolationclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
migration=ops/sql/20260720_memory_v1_predicate_runtime_v5_1_downstream_isolation.sql
test_sql=tests/memory_v1_predicate_runtime_v5_1_downstream_isolation.sql
compat_migration=ops/sql/20260720_memory_v1_predicate_runtime_v5_1_canonical_compiler_compat.sql
compat_test=tests/memory_v1_predicate_runtime_v5_1_canonical_compiler_compat.sql
backup=$(mktemp /tmp/memory-v1-v5-1-isolation.XXXXXX.dump)
tables=$(mktemp /tmp/memory-v1-v5-1-isolation-tables.XXXXXX.txt)
before=$(mktemp /tmp/memory-v1-v5-1-isolation-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-v5-1-isolation-after.XXXXXX.tsv)

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
  local output=$1 state
  : >"$output"
  while IFS=$'\t' read -r schema table; do
    [[ "$schema" =~ ^[a-z][a-z0-9_]*$ ]]
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    state=$(scalar "
      SELECT count(*)::text || E'\\t' || encode(
        public.digest(convert_to(coalesce(string_agg(row_value,E'\\n'
          ORDER BY row_value),''),'UTF8'),'sha256'),'hex')
      FROM (SELECT to_jsonb(value)::text AS row_value
        FROM \"$schema\".\"$table\" AS value) AS rows")
    printf '%s\t%s\t%s\n' "$schema" "$table" "$state" >>"$output"
  done <"$tables"
}

[[ -f "$repo_root/$migration" && -f "$repo_root/$test_sql" ]]
[[ -f "$repo_root/$compat_migration" && -f "$repo_root/$compat_test" ]]
docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc --no-owner --no-privileges >"$backup"
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
  --clean --if-exists --no-owner --no-privileges <"$backup"

scalar "
  SELECT table_schema || E'\\t' || table_name
  FROM information_schema.tables
  WHERE table_type='BASE TABLE' AND table_schema IN ('memory','public')
  ORDER BY table_schema,table_name
" >"$tables"
capture_state "$before"

run_sql <"$repo_root/$compat_migration"
run_sql <"$repo_root/$compat_migration"
run_sql <"$repo_root/$compat_test"
run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$test_sql"

capture_state "$after"
cmp -s "$before" "$after"
printf '%s\n' 'memory_v1_predicate_runtime_v5_1_downstream_isolation_clone: PASS'
