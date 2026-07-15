#!/usr/bin/env bash
set -euo pipefail

compose=(docker compose -p memoryv1v5stage -f docker-compose.ci.yml)
migration=ops/sql/20260715_memory_v1_relational_staging_v5.sql
rollback=ops/sql/20260715_memory_v1_relational_staging_v5_rollback.sql
security_test=tests/memory_v1_relational_staging_v5.sql

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT

run_sql() {
  "${compose[@]}" exec -T postgres \
    psql -X -v ON_ERROR_STOP=1 -U sage -d memory
}

"${compose[@]}" up -d --wait postgres

printf '%s\n' 'CREATE EXTENSION IF NOT EXISTS pgcrypto;' | run_sql

run_sql < ops/sql/20260712_memory_v1_foundation.sql
run_sql < ops/sql/20260712_memory_v1_claim_qualifiers.sql
run_sql < ops/sql/20260713_memory_v1_artifacts.sql
run_sql < ops/sql/20260713_memory_v1_evidence_lifecycle.sql

baseline_predicate_count=$(
  "${compose[@]}" exec -T postgres \
    psql -X -A -t -v ON_ERROR_STOP=1 -U sage -d memory \
    -c 'SELECT count(*) FROM memory.predicate'
)

run_sql < "$migration"
run_sql < "$migration"
run_sql < "$security_test"

"${compose[@]}" exec -T postgres \
  pg_dump -U sage -d memory --schema-only --schema=memory \
  --no-owner --no-privileges >/dev/null

if {
  printf '%s\n' 'SET ROLE brains_app;'
  cat "$migration"
} | run_sql >/dev/null 2>&1; then
  echo 'V5 staging migration unexpectedly succeeded as brains_app' >&2
  exit 1
fi

run_sql < "$rollback"

remaining_predicate_count=$(
  "${compose[@]}" exec -T postgres \
    psql -X -A -t -v ON_ERROR_STOP=1 -U sage -d memory \
    -c 'SELECT count(*) FROM memory.predicate'
)

if [[ "$remaining_predicate_count" != "$baseline_predicate_count" ]]; then
  echo 'V5 rollback changed the pre-migration predicate count' >&2
  exit 1
fi

remaining_table_count=$(
  "${compose[@]}" exec -T postgres \
    psql -X -A -t -v ON_ERROR_STOP=1 -U sage -d memory \
    -c "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='memory' AND c.relname IN ('entity_mention','entity_resolution_plan','entity_resolution_candidate','entity_resolution_review','entity_resolution_apply','entity_alias_observation','observation','observation_temporal','observation_entity_binding','claim_observation','candidate_observation','predicate_registry_version','predicate_registry_seed','predicate_contract')"
)

if [[ "$remaining_table_count" != '0' ]]; then
  echo 'V5 rollback left staging tables behind' >&2
  exit 1
fi

echo 'memory_v1_relational_staging_v5_ci: PASS'
