#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Runs the exact thread-binding transaction against a
# disposable full production clone. No external provider is called.

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_COMPONENT_BINDING_CLONE_PORT:-55451}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v5componentbindingclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)

manifest=ops/manifests/memory_v1_v5_component_thread_binding_apply_20260717.json
manifest_sha=ce19dbd1c41a9831ceaae18509a94d2d255ef06d18890aa84f453294f49c8d1f
apply_sql=ops/sql/20260717_memory_v1_v5_component_thread_binding_apply.sql
apply_sql_sha=72baa74bade65bbbdffebc59784fc56c1e6b538afdeac6050bc0b7531865e28d
bounded_migration=ops/sql/20260717_memory_v1_v5_bounded_extraction.sql
component_migration=ops/sql/20260717_memory_v1_v5_project_components.sql

backup=$(mktemp /tmp/memory-v1-v5-component-binding.XXXXXX.dump)
tables=$(mktemp /tmp/memory-v1-v5-component-binding-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-component-binding-before.XXXXXX)
after=$(mktemp /tmp/memory-v1-v5-component-binding-after.XXXXXX)

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

for required in "$manifest" "$apply_sql" "$bounded_migration" "$component_migration"; do
  [[ -f "$repo_root/$required" ]]
done
[[ "$(sha256sum "$repo_root/$manifest" | cut -d' ' -f1)" == "$manifest_sha" ]]
[[ "$(sha256sum "$repo_root/$apply_sql" | cut -d' ' -f1)" == "$apply_sql_sha" ]]

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
run_sql <"$repo_root/$bounded_migration"
run_sql <"$repo_root/$component_migration"

[[ "$(scalar 'SELECT count(*) FROM memory.project_thread_binding_event')" == 0 ]]
scalar "SELECT table_name FROM information_schema.tables WHERE table_schema='memory' AND table_type='BASE TABLE' AND table_name<>'project_thread_binding_event' ORDER BY table_name" >"$tables"
capture_state "$before"
run_sql <"$repo_root/$apply_sql"
capture_state "$after"
cmp -s "$before" "$after"

[[ "$(scalar 'SELECT count(*) FROM memory.project_thread_binding_event')" == 1 ]]
[[ "$(scalar "SELECT count(*) FROM memory.current_project_thread_binding_v5 WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50' AND thread_id='d776c8ef-7f3d-45b2-8820-4be87b7ca19d' AND project_id='08cd6a8a-5599-43d5-8d5c-b59401df8ccc'")" == 1 ]]
[[ "$(scalar "SELECT count(*) FROM memory.project_thread_binding_event WHERE owner_user_id<>'1240822d-ac9a-4096-95aa-e2b24d36ef50'")" == 0 ]]

printf '%s\n' 'memory_v1_v5_component_thread_binding_production_clone: PASS'
