#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Production is read-only: only its schema is copied into
# an isolated PostgreSQL 16 container. All fixture rows are rolled back.

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_PATTERN_REVIEW_V5_1_CLONE_PORT:-55462}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1patternreviewv51clone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
base_migration=ops/sql/20260720_memory_v1_epistemic_pattern_salience_v5_1.sql
base_rollback=ops/sql/20260720_memory_v1_epistemic_pattern_salience_v5_1_rollback.sql
migration=ops/sql/20260720_memory_v1_pattern_review_apply_v5_1.sql
rollback=ops/sql/20260720_memory_v1_pattern_review_apply_v5_1_rollback.sql
test_sql=tests/memory_v1_pattern_review_apply_v5_1.sql
base_migration_sha=629a315135b0b0627f88944460adbaddb48efc7ec5e78681952333b2374dc20c
base_rollback_sha=5343d67af7d7e8d784f776d9c8003aa7da4df21aa501de70c0b8496e9a2f9a6d
migration_sha=4d7357e126c8a09908210d9d63a8a9a0636ab7f9ee583f7223caa0fe60cabdad
rollback_sha=8c52da53f278e63ab94c0890c72aec4b633aa2169ad09569182e58e87f1d6cf4
test_sha=46925dcd192d05927aa19fc5cceb19a6934bad73b9d925334f692e120512139c

schema_dump=$(mktemp /tmp/memory-v1-pattern-review-schema.XXXXXX.sql)
role_sql=$(mktemp /tmp/memory-v1-pattern-review-roles.XXXXXX.sql)
before_schema=$(mktemp /tmp/memory-v1-pattern-review-before.XXXXXX.sql)
after_schema=$(mktemp /tmp/memory-v1-pattern-review-after.XXXXXX.sql)

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

for required in "$base_migration" "$base_rollback" \
  "$migration" "$rollback" "$test_sql"; do
  [[ -f "$repo_root/$required" ]]
done
[[ "$(sha256sum "$repo_root/$base_migration" | cut -d' ' -f1)" == "$base_migration_sha" ]]
[[ "$(sha256sum "$repo_root/$base_rollback" | cut -d' ' -f1)" == "$base_rollback_sha" ]]
[[ "$(sha256sum "$repo_root/$migration" | cut -d' ' -f1)" == "$migration_sha" ]]
[[ "$(sha256sum "$repo_root/$rollback" | cut -d' ' -f1)" == "$rollback_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | cut -d' ' -f1)" == "$test_sha" ]]

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

schema_preinstalled=$(scalar "SELECT (
  to_regclass('memory.pattern_hypothesis_v5_1') IS NOT NULL
  AND to_regclass('memory.pattern_review_v5_1') IS NOT NULL
)::integer")
[[ "$schema_preinstalled" == 0 || "$schema_preinstalled" == 1 ]]
run_sql <"$repo_root/$base_migration"
run_sql <"$repo_root/$base_migration"
run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$test_sql"

for relation in pattern_review_v5_1 pattern_review_observation_v5_1 \
  pattern_apply_event_v5_1 pattern_operation_request_v5_1 \
  pattern_hypothesis_v5_1 pattern_hypothesis_revision_v5_1 \
  pattern_observation_link_v5_1; do
  [[ "$(scalar "SELECT count(*) FROM memory.$relation")" == 0 ]]
done

if [[ "$schema_preinstalled" == 0 ]]; then
  run_sql <"$repo_root/$rollback"
  run_sql <"$repo_root/$base_rollback"
  [[ "$(scalar "SELECT (
    to_regclass('memory.pattern_review_v5_1') IS NULL
    AND to_regprocedure(
      'memory.apply_pattern_review_v5_1(uuid,uuid,text)'
    ) IS NULL
    AND to_regclass('memory.pattern_hypothesis_v5_1') IS NULL
    AND to_regrole('memory_v5_epistemic_writer') IS NULL
  )::integer")" == 1 ]]
fi

"${compose[@]}" exec -T postgres pg_dump -U sage -d memory \
  --schema-only --no-owner --no-privileges >"$after_schema"
if ! diff -I '^\\restrict ' -I '^\\unrestrict ' -q \
  "$before_schema" "$after_schema" >/dev/null; then
  diff -I '^\\restrict ' -I '^\\unrestrict ' -u \
    "$before_schema" "$after_schema" | head -n 240 >&2
  printf '%s\n' 'pattern review clone rollback did not restore baseline' >&2
  exit 1
fi

printf '%s\n' 'memory_v1_pattern_review_apply_v5_1_production_clone: PASS'
