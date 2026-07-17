#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores a temporary full production database inside an
# isolated Docker clone, reapplies the installed component ACL contract, and
# runs the exact Memory V1 registration inside a rolled-back transaction.

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_MEMORY_COMPONENT_CLONE_PORT:-55449}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v5memorycomponentclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)

manifest=ops/manifests/memory_v1_v5_memory_component_registration_dry_run_20260717.json
manifest_sha=1af8e98955860b82957ff462155d2e40865523a6bfc66fc7ff156587444693ea
test_sql=tests/memory_v1_v5_memory_component_registration_dry_run.sql
test_sha=dd3716c845b73c6204ac9d7fa8902134a0e53e0b0576a6c899e9026547da273a
migration=ops/sql/20260717_memory_v1_v5_project_components.sql
acl_compat=ops/sql/20260717_memory_v1_v5_project_scope_acl_compat.sql

backup=$(mktemp /tmp/memory-v1-v5-memory-component.XXXXXX.dump)
tables=$(mktemp /tmp/memory-v1-v5-memory-component-tables.XXXXXX.txt)
before=$(mktemp /tmp/memory-v1-v5-memory-component-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-v5-memory-component-after.XXXXXX.tsv)

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

for required in "$manifest" "$test_sql" "$migration" "$acl_compat"; do
  [[ -f "$repo_root/$required" ]]
done
[[ "$(sha256sum "$repo_root/$manifest" | cut -d' ' -f1)" == "$manifest_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | cut -d' ' -f1)" == "$test_sha" ]]

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

run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$acl_compat"

[[ "$(scalar "SELECT count(*) FROM memory.project_component_v5")" == "0" ]]
[[ "$(scalar "SELECT count(*) FROM memory.project_component_alias_v5")" == "0" ]]
[[ "$(scalar "SELECT count(*) FROM memory.project_component_registration_event_v5")" == "0" ]]

scalar "SELECT table_name FROM information_schema.tables WHERE table_schema='memory' AND table_type='BASE TABLE' ORDER BY table_name" >"$tables"
capture_state "$before"
run_sql <"$repo_root/$test_sql"
capture_state "$after"
cmp -s "$before" "$after"

[[ "$(scalar "SELECT count(*) FROM memory.project_component_v5")" == "0" ]]
[[ "$(scalar "SELECT count(*) FROM memory.project_component_alias_v5")" == "0" ]]
[[ "$(scalar "SELECT count(*) FROM memory.project_component_registration_event_v5")" == "0" ]]

printf '%s\n' 'memory_v1_v5_memory_component_registration_production_clone: PASS'
