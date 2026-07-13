#!/usr/bin/env bash
set -euo pipefail

compose=(docker compose -p memoryv1test -f docker-compose.ci.yml)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT

"${compose[@]}" up -d --wait postgres

"${compose[@]}" exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U sage -d memory \
  < ops/sql/20260712_memory_v1_foundation.sql

"${compose[@]}" exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U sage -d memory \
  < ops/sql/20260712_memory_v1_claim_qualifiers.sql

"${compose[@]}" exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U sage -d memory \
  < ops/sql/20260712_memory_v1_projection_outbox_owner_index.sql

"${compose[@]}" exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U sage -d memory \
  < ops/sql/20260713_memory_v1_artifacts.sql

"${compose[@]}" exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U sage -d memory \
  < ops/sql/20260713_memory_v1_artifacts.sql

"${compose[@]}" exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U sage -d memory \
  < ops/sql/20260712_memory_v1_projection_outbox_owner_index.sql

"${compose[@]}" exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U sage -d memory \
  < ops/sql/20260712_memory_v1_claim_qualifiers.sql

# Re-applying must be safe and must not duplicate seed rows or policies.
"${compose[@]}" exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U sage -d memory \
  < ops/sql/20260712_memory_v1_foundation.sql

"${compose[@]}" exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U sage -d memory \
  < tests/memory_v1_rls.sql

"${compose[@]}" build brains
"${compose[@]}" run --rm --no-deps \
  -e POSTGRES_DSN=postgresql://sage:ci_only_postgres_password@postgres:5432/memory \
  -e PYTHONPATH=/app \
  brains python scripts/memory_v1_store_integration.py

"${compose[@]}" run --rm --no-deps \
  -e POSTGRES_DSN=postgresql://sage:ci_only_postgres_password@postgres:5432/memory \
  -e PYTHONPATH=/app \
  brains python scripts/memory_v1_artifact_ingestion_test.py

"${compose[@]}" run --rm --no-deps \
  -e PYTHONPATH=/app \
  brains python scripts/memory_v1_seed_mapping_test.py

"${compose[@]}" run --rm --no-deps \
  -e PYTHONPATH=/app \
  brains python scripts/memory_v1_evidence_triage_test.py

"${compose[@]}" run --rm --no-deps \
  -e PYTHONPATH=/app \
  brains python scripts/memory_v1_atomic_span_test.py

"${compose[@]}" run --rm --no-deps \
  -e PYTHONPATH=/app \
  brains python scripts/memory_v1_compound_span_test.py

"${compose[@]}" run --rm --no-deps \
  -e PYTHONPATH=/app \
  brains python scripts/memory_v1_evidence_persistence_test.py

"${compose[@]}" run --rm --no-deps \
  -e PYTHONPATH=/app \
  brains python scripts/memory_v1_actor_auth_test.py

"${compose[@]}" run --rm --no-deps \
  -e PYTHONPATH=/app \
  brains python scripts/memory_v1_shadow_policy_test.py

"${compose[@]}" run --rm --no-deps \
  -e PYTHONPATH=/app \
  brains python scripts/query_embedding_cache_test.py

"${compose[@]}" run --rm --no-deps \
  -e PYTHONPATH=/app \
  brains python scripts/memory_v1_pet_review_test.py

"${compose[@]}" run --rm --no-deps \
  -e POSTGRES_DSN=postgresql://sage:ci_only_postgres_password@postgres:5432/memory \
  -e PYTHONPATH=/app \
  brains python scripts/memory_v1_projection_integration.py

"${compose[@]}" exec -T postgres \
  pg_dump -U sage -d memory --schema-only --schema=memory --no-owner --no-privileges \
  >/dev/null

# The application role must never be able to execute the migration.
if {
  printf '%s\n' 'SET ROLE brains_app;'
  cat ops/sql/20260712_memory_v1_foundation.sql
} | "${compose[@]}" exec -T postgres \
      psql -X -v ON_ERROR_STOP=1 -U sage -d memory >/dev/null 2>&1; then
  echo "memory V1 migration unexpectedly succeeded as brains_app" >&2
  exit 1
fi

# RLS test data was rolled back, so the guarded rollback must now succeed.
"${compose[@]}" exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U sage -d memory \
  < ops/sql/20260713_memory_v1_artifacts_rollback.sql

"${compose[@]}" exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U sage -d memory \
  < ops/sql/20260712_memory_v1_foundation_rollback.sql

schema_count=$(
  "${compose[@]}" exec -T postgres \
    psql -X -A -t -v ON_ERROR_STOP=1 -U sage -d memory \
    -c "SELECT count(*) FROM pg_namespace WHERE nspname='memory'"
)
if [[ "$schema_count" != "0" ]]; then
  echo "memory V1 rollback left schema behind" >&2
  exit 1
fi

echo "memory_v1_schema_ci: PASS"
