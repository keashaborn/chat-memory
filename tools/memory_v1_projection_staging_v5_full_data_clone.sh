#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores a temporary full production database inside an
# isolated Docker clone, reapplies the current ACL migrations, and runs only
# the owner-scoped production projection suite. No data leaves seebx.

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_PROJECTION_FULL_CLONE_PORT:-55447}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v5projectionfulldataclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)

migrations=(
  ops/sql/20260715_memory_v1_relational_staging_v5.sql
  ops/sql/20260715_memory_v1_relational_writer_v5.sql
  ops/sql/20260715_memory_v1_projection_targets_v5.sql
  ops/sql/20260715_memory_v1_projection_staging_v5.sql
  ops/sql/20260715_memory_v1_projection_apply_v5.sql
  ops/sql/20260717_memory_v1_v5_project_components.sql
)
production_test=tests/memory_v1_projection_staging_production_v5.sql
backup=$(mktemp /tmp/memory-v1-v5-projection-full.XXXXXX.dump)
tables=$(mktemp /tmp/memory-v1-v5-projection-full-tables.XXXXXX.txt)
before=$(mktemp /tmp/memory-v1-v5-projection-full-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-v5-projection-full-after.XXXXXX.tsv)

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
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
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
        FROM memory.\"$table\" AS table_row
      ) AS rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$tables"
}

for required in "${migrations[@]}" "$production_test"; do
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

for migration in "${migrations[@]}"; do
  run_sql <"$repo_root/$migration"
done

[[ "$(scalar "SELECT count(*) FROM memory.entity_mention WHERE owner_user_id IN ('11111111-1111-4111-8111-111111111111','22222222-2222-4222-8222-222222222222')")" == "0" ]]
scalar "SELECT table_name FROM information_schema.tables WHERE table_schema='memory' AND table_type='BASE TABLE' ORDER BY table_name" >"$tables"
capture_state "$before"
run_sql <"$repo_root/$production_test"
capture_state "$after"
cmp -s "$before" "$after"

[[ "$(scalar "SELECT (
  NOT has_table_privilege('brains_app','memory.projection_plan','SELECT')
  AND has_function_privilege('memory_v5_extraction_maintainer','memory.v5_project_scope_valid(jsonb)','EXECUTE')
  AND NOT has_function_privilege('brains_app','memory.v5_project_scope_valid(jsonb)','EXECUTE')
)::integer")" == "1" ]]

printf '%s\n' 'memory_v1_projection_staging_v5_full_data_clone: PASS'
