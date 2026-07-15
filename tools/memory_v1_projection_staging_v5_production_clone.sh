#!/usr/bin/env bash
set -euo pipefail

: "${POSTGRES_DSN:?POSTGRES_DSN must point to seebx production for schema-only cloning}"

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
apply_migration=ops/sql/20260715_memory_v1_projection_apply_v5.sql
apply_rollback=ops/sql/20260715_memory_v1_projection_apply_v5_rollback.sql

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

server_major=$(scalar_sql "SELECT current_setting('server_version_num')::integer / 10000")
if [[ "$server_major" != '16' ]]; then
  echo "projection clone requires PostgreSQL 16, got major $server_major" >&2
  exit 1
fi

# Read-only production access. No rows, chat content, credentials, or Qdrant data
# enter this disposable clone.
pg_dump "$POSTGRES_DSN" \
  --schema-only --no-owner --no-privileges \
  | run_sql

if [[ "$(scalar_sql "SELECT count(*) FROM pg_roles WHERE rolname='brains_app'")" = '0' ]]; then
  printf '%s\n' \
    'CREATE ROLE brains_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
    | run_sql
fi

baseline_predicate_count=$(scalar_sql 'SELECT count(*) FROM memory.predicate')

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

if [[ "${PROJECTION_SKIP_SECURITY_TEST:-0}" != '1' ]]; then
  run_sql < "$projection_test"
fi

"${compose[@]}" exec -T postgres \
  pg_dump -U sage -d memory --schema-only --schema=memory \
  --no-owner --no-privileges >/dev/null

run_sql < "$apply_rollback"
run_sql < "$projection_rollback"
run_sql < "$target_rollback"
run_sql < "$writer_rollback"
run_sql < "$staging_rollback"

remaining_predicate_count=$(scalar_sql 'SELECT count(*) FROM memory.predicate')
if [[ "$remaining_predicate_count" != "$baseline_predicate_count" ]]; then
  echo 'projection clone rollback changed the baseline predicate count' >&2
  exit 1
fi

remaining_objects=$(scalar_sql "
  SELECT
    (SELECT count(*) FROM pg_roles WHERE rolname='memory_v5_writer')
    +
    (SELECT count(*)
     FROM pg_class AS relation
     JOIN pg_namespace AS namespace ON namespace.oid=relation.relnamespace
     WHERE namespace.nspname='memory'
       AND relation.relname IN (
         'projection_plan', 'projection_plan_item',
         'projection_claim_payload', 'projection_preference_payload',
         'projection_project_payload', 'projection_plan_observation',
         'projection_plan_relation', 'projection_review',
         'projection_apply_event', 'preference_revision_observation',
         'project_knowledge_revision_observation',
         'claim_relation_v5', 'preference_relation_v5',
         'project_knowledge_relation_v5', 'projection_dispatch_v5',
         'preference_head_v5', 'preference_revision_v5',
         'project_knowledge_head_v5', 'project_knowledge_revision_v5',
         'relational_stage_batch', 'relational_operation_request'
       ))
")
if [[ "$remaining_objects" != '0' ]]; then
  echo 'projection clone rollback left V5 objects behind' >&2
  exit 1
fi

echo 'memory_v1_projection_staging_v5_production_clone: PASS'
