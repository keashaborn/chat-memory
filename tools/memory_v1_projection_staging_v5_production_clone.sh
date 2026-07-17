#!/usr/bin/env bash
set -euo pipefail

compose=(docker compose -p memoryv1v5projectionprodclone -f docker-compose.ci.yml)
staging_migration=ops/sql/20260715_memory_v1_relational_staging_v5.sql
staging_rollback=ops/sql/20260715_memory_v1_relational_staging_v5_rollback.sql
staging_test=tests/memory_v1_relational_staging_v5.sql
writer_migration=ops/sql/20260715_memory_v1_relational_writer_v5.sql
writer_rollback=ops/sql/20260715_memory_v1_relational_writer_v5_rollback.sql
writer_test=tests/memory_v1_relational_writer_v5.sql
target_migration=ops/sql/20260715_memory_v1_projection_targets_v5.sql
target_rollback=ops/sql/20260715_memory_v1_projection_targets_v5_rollback.sql
target_test=tests/memory_v1_projection_targets_v5.sql
projection_migration=ops/sql/20260715_memory_v1_projection_staging_v5.sql
projection_rollback=ops/sql/20260715_memory_v1_projection_staging_v5_rollback.sql
projection_test=tests/memory_v1_projection_staging_v5.sql
projection_production_test=tests/memory_v1_projection_staging_production_v5.sql
apply_migration=ops/sql/20260715_memory_v1_projection_apply_v5.sql
apply_rollback=ops/sql/20260715_memory_v1_projection_apply_v5_rollback.sql
component_migration=ops/sql/20260717_memory_v1_v5_project_components.sql
component_rollback=ops/sql/20260717_memory_v1_v5_project_components_rollback.sql

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT

run_sql() {
  "${compose[@]}" exec -T postgres \
    psql -X -v ON_ERROR_STOP=1 -U sage -d memory
}

scalar_sql() {
  "${compose[@]}" exec -T postgres \
    psql -X -A -t -v ON_ERROR_STOP=1 -U sage -d memory -c "$1"
}

"${compose[@]}" up -d --wait postgres

printf '%s\n' \
  'CREATE ROLE brains_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;' \
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

server_major=$(scalar_sql "SELECT current_setting('server_version_num')::integer / 10000")
if [[ "$server_major" != '16' ]]; then
  echo "projection clone requires PostgreSQL 16, got major $server_major" >&2
  exit 1
fi

# Read-only production access through the local database owner. No rows, chat
# content, credentials, or Qdrant data enter this disposable clone.
docker exec brains-postgres-1 pg_dump -U sage -d memory \
  --schema-only --no-owner --no-privileges \
  | run_sql

run_sql < "$staging_migration"
run_sql < "$staging_migration"
run_sql < "$staging_test"
run_sql < "$writer_migration"
run_sql < "$writer_migration"
run_sql < "$writer_test"
run_sql < "$target_migration"
run_sql < "$target_migration"
run_sql < "$target_test"
run_sql < "$projection_migration"
run_sql < "$projection_migration"
run_sql < "$apply_migration"
run_sql < "$apply_migration"
run_sql < "$component_migration"
run_sql < "$component_migration"

baseline_predicate_count=$(scalar_sql 'SELECT count(*) FROM memory.predicate')

if [[ "${PROJECTION_SKIP_SECURITY_TEST:-0}" != '1' ]]; then
  run_sql < "$projection_test"
  run_sql < "$projection_production_test"
fi

"${compose[@]}" exec -T postgres \
  pg_dump -U sage -d memory --schema-only --schema=memory \
  --no-owner --no-privileges >/dev/null

remaining_predicate_count=$(scalar_sql 'SELECT count(*) FROM memory.predicate')
if [[ "$remaining_predicate_count" != "$baseline_predicate_count" ]]; then
  echo 'projection clone tests changed the baseline predicate count' >&2
  exit 1
fi

remaining_rows=$(scalar_sql "
  SELECT
    (SELECT count(*) FROM memory.project_component_v5)
    + (SELECT count(*) FROM memory.project_component_alias_v5)
    + (SELECT count(*) FROM memory.project_component_registration_event_v5)
    + (SELECT count(*) FROM memory.projection_plan)
    + (SELECT count(*) FROM memory.projection_apply_event)
    + (SELECT count(*) FROM memory.project_knowledge_head_v5)
")
if [[ "$remaining_rows" != '0' ]]; then
  echo 'projection clone tests left transaction-scoped rows behind' >&2
  exit 1
fi

echo 'memory_v1_projection_staging_v5_production_clone: PASS'
