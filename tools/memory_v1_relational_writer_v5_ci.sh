#!/usr/bin/env bash
set -euo pipefail

compose=(docker compose -p memoryv1v5writer -f docker-compose.ci.yml)
staging_migration=ops/sql/20260715_memory_v1_relational_staging_v5.sql
staging_rollback=ops/sql/20260715_memory_v1_relational_staging_v5_rollback.sql
staging_test=tests/memory_v1_relational_staging_v5.sql
writer_migration=ops/sql/20260715_memory_v1_relational_writer_v5.sql
writer_rollback=ops/sql/20260715_memory_v1_relational_writer_v5_rollback.sql
writer_test=tests/memory_v1_relational_writer_v5.sql

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT

run_sql() {
  "${compose[@]}" exec -T postgres \
    psql -X -v ON_ERROR_STOP=1 -U sage -d memory
}

"${compose[@]}" up -d --wait postgres

printf '%s\n' \
  'CREATE EXTENSION IF NOT EXISTS pgcrypto;' \
  'CREATE EXTENSION IF NOT EXISTS unaccent;' \
  | run_sql

run_sql < ops/sql/20260712_memory_v1_foundation.sql
run_sql < ops/sql/20260712_memory_v1_claim_qualifiers.sql
run_sql < ops/sql/20260713_memory_v1_artifacts.sql
run_sql < ops/sql/20260713_memory_v1_evidence_lifecycle.sql

baseline_predicate_count=$(
  "${compose[@]}" exec -T postgres \
    psql -X -A -t -v ON_ERROR_STOP=1 -U sage -d memory \
    -c 'SELECT count(*) FROM memory.predicate'
)

run_sql < "$staging_migration"
run_sql < "$staging_migration"
run_sql < "$staging_test"
run_sql < "$writer_migration"
run_sql < "$writer_migration"
run_sql < "$writer_test"

"${compose[@]}" exec -T postgres \
  pg_dump -U sage -d memory --schema-only --schema=memory \
  --no-owner --no-privileges >/dev/null

if {
  printf '%s\n' 'SET ROLE brains_app;'
  cat "$writer_migration"
} | run_sql >/dev/null 2>&1; then
  echo 'V5 writer migration unexpectedly succeeded as brains_app' >&2
  exit 1
fi

run_sql < "$writer_rollback"
run_sql < "$staging_rollback"

remaining_predicate_count=$(
  "${compose[@]}" exec -T postgres \
    psql -X -A -t -v ON_ERROR_STOP=1 -U sage -d memory \
    -c 'SELECT count(*) FROM memory.predicate'
)
if [[ "$remaining_predicate_count" != "$baseline_predicate_count" ]]; then
  echo 'V5 writer rollback changed the pre-migration predicate count' >&2
  exit 1
fi

remaining_writer_objects=$(
  "${compose[@]}" exec -T postgres \
    psql -X -A -t -v ON_ERROR_STOP=1 -U sage -d memory \
    -c "SELECT (SELECT count(*) FROM pg_roles WHERE rolname='memory_v5_writer') + (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='memory' AND c.relname IN ('relational_stage_batch','relational_operation_request'))"
)
if [[ "$remaining_writer_objects" != '0' ]]; then
  echo 'V5 writer rollback left writer objects behind' >&2
  exit 1
fi

echo 'memory_v1_relational_writer_v5_ci: PASS'
