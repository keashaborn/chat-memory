#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Reads the production schema, restores it into an
# isolated PostgreSQL 16 container, and performs rollback-only tests.

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_EPISTEMIC_V5_1_CLONE_PORT:-55461}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1epistemicv51clone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
migration=ops/sql/20260720_memory_v1_epistemic_pattern_salience_v5_1.sql
rollback=ops/sql/20260720_memory_v1_epistemic_pattern_salience_v5_1_rollback.sql
test_sql=tests/memory_v1_epistemic_pattern_salience_v5_1.sql
migration_sha=629a315135b0b0627f88944460adbaddb48efc7ec5e78681952333b2374dc20c
rollback_sha=5343d67af7d7e8d784f776d9c8003aa7da4df21aa501de70c0b8496e9a2f9a6d
test_sha=01a567399eddde4af41561fdb73c2dea50eb27fa6c08d5798a577d003b2ecfaa

schema_dump=$(mktemp /tmp/memory-v1-epistemic-v5-1-schema.XXXXXX.sql)
role_sql=$(mktemp /tmp/memory-v1-epistemic-v5-1-roles.XXXXXX.sql)
before_schema=$(mktemp /tmp/memory-v1-epistemic-v5-1-before.XXXXXX.sql)
after_schema=$(mktemp /tmp/memory-v1-epistemic-v5-1-after.XXXXXX.sql)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$schema_dump" "$role_sql" "$before_schema" "$after_schema"
}
trap cleanup EXIT
chmod 0600 "$schema_dump" "$role_sql" "$before_schema" "$after_schema"

run_sql() {
  "${compose[@]}" exec -T postgres \
    psql -X -v ON_ERROR_STOP=1 -U sage -d memory
}

scalar() {
  "${compose[@]}" exec -T postgres \
    psql -X -A -t -v ON_ERROR_STOP=1 -U sage -d memory -c "$1"
}

for required in "$migration" "$rollback" "$test_sql"; do
  [[ -f "$repo_root/$required" ]]
done
[[ "$(sha256sum "$repo_root/$migration" | cut -d' ' -f1)" == "$migration_sha" ]]
[[ "$(sha256sum "$repo_root/$rollback" | cut -d' ' -f1)" == "$rollback_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | cut -d' ' -f1)" == "$test_sha" ]]

# Production is read only. No rows or chat content enter the clone.
docker exec brains-postgres-1 pg_dump -U sage -d memory \
  --schema-only --no-owner --no-privileges >"$schema_dump"
[[ -s "$schema_dump" ]]

docker exec brains-postgres-1 psql -X -A -t -U sage -d memory -c "
  SELECT format(
    'CREATE ROLE %I %s NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;',
    rolname,
    CASE WHEN rolname='brains_app' THEN 'LOGIN INHERIT'
         ELSE 'NOLOGIN NOINHERIT' END
  )
  FROM pg_roles
  WHERE rolname NOT LIKE 'pg\\_%' ESCAPE '\\'
    AND rolname NOT IN ('sage','postgres')
  ORDER BY rolname
" >"$role_sql"

"${compose[@]}" up -d --wait postgres
run_sql <"$role_sql"
run_sql <"$schema_dump"
printf '%s\n' \
  'GRANT USAGE ON SCHEMA memory TO brains_app;' \
  'GRANT EXECUTE ON FUNCTION memory.current_actor_user_id() TO brains_app;' \
  | run_sql

"${compose[@]}" exec -T postgres pg_dump -U sage -d memory \
  --schema-only --no-owner --no-privileges >"$before_schema"

run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$test_sql"

[[ "$(scalar "SELECT count(*) FROM memory.epistemic_target_binding_v5_1")" == 0 ]]
[[ "$(scalar "SELECT count(*) FROM memory.epistemic_assessment_snapshot_v5_1")" == 0 ]]
[[ "$(scalar "SELECT count(*) FROM memory.salience_feature_snapshot_v5_1")" == 0 ]]
[[ "$(scalar "SELECT count(*) FROM memory.retrieval_outcome_signal_v5_1")" == 0 ]]
[[ "$(scalar "SELECT count(*) FROM memory.epistemic_operation_request_v5_1")" == 0 ]]

"${compose[@]}" exec -T postgres pg_dump -U sage -d memory \
  --schema-only --no-owner --no-privileges >/dev/null

run_sql <"$repo_root/$rollback"

[[ "$(scalar "SELECT (
  to_regclass('memory.pattern_hypothesis_v5_1') IS NULL
  AND to_regclass('memory.epistemic_target_binding_v5_1') IS NULL
  AND to_regclass('memory.epistemic_assessment_snapshot_v5_1') IS NULL
  AND to_regclass('memory.salience_feature_snapshot_v5_1') IS NULL
  AND to_regclass('memory.retrieval_outcome_signal_v5_1') IS NULL
  AND to_regprocedure(
    'memory.persist_epistemic_snapshot_packet_v5_1(uuid,jsonb)'
  ) IS NULL
  AND to_regrole('memory_v5_epistemic_writer') IS NULL
)::integer")" == 1 ]]

"${compose[@]}" exec -T postgres pg_dump -U sage -d memory \
  --schema-only --no-owner --no-privileges >"$after_schema"
if ! diff -I '^\\restrict ' -I '^\\unrestrict ' -q \
  "$before_schema" "$after_schema" >/dev/null; then
  diff -I '^\\restrict ' -I '^\\unrestrict ' -u \
    "$before_schema" "$after_schema" | head -n 240 >&2
  printf '%s\n' 'production-schema clone rollback did not restore baseline' >&2
  exit 1
fi

printf '%s\n' 'memory_v1_epistemic_pattern_salience_v5_1_production_clone: PASS'
