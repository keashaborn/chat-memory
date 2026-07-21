#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_1_RECONCILIATION_CLONE_PORT:-55471}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v51reconciliationclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
migration=ops/sql/20260721_memory_v1_entity_resolution_reconciliation_v5_1.sql
rollback=ops/sql/20260721_memory_v1_entity_resolution_reconciliation_v5_1_rollback.sql
backup=$(mktemp /tmp/memory-v1-v5-1-reconciliation.XXXXXX.dump)
roles=$(mktemp /tmp/memory-v1-v5-1-reconciliation-roles.XXXXXX.sql)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$roles"
}
trap cleanup EXIT
chmod 0600 "$backup" "$roles"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory "$@"
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc --no-owner --no-privileges >"$backup"
[[ -s "$backup" ]]
docker exec brains-postgres-1 psql -X -A -t -U sage -d memory -c "
  SELECT format(
    'CREATE ROLE %I %s %s NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;',
    rolname,
    CASE WHEN rolcanlogin THEN 'LOGIN' ELSE 'NOLOGIN' END,
    CASE WHEN rolinherit THEN 'INHERIT' ELSE 'NOINHERIT' END
  )
  FROM pg_roles
  WHERE rolname NOT LIKE 'pg\\_%' ESCAPE '\\'
    AND rolname NOT IN ('sage','postgres')
  ORDER BY rolname
" >"$roles"
[[ -s "$roles" ]]

"${compose[@]}" up -d --wait postgres
run_sql <"$roles"
printf '%s\n' "ALTER ROLE brains_app PASSWORD 'clone_only_brains_password';" \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"

before=$(scalar "
  SELECT count(*) FROM information_schema.tables
  WHERE table_schema='memory'
    AND table_name='entity_resolution_reconciliation_v5_1'")
[[ "$before" == 0 ]]
run_sql <"$migration"
run_sql <"$migration"
[[ "$(scalar "SELECT count(*) FROM memory.entity_resolution_reconciliation_v5_1")" == 0 ]]
[[ "$(scalar "SELECT relrowsecurity::int || ':' || relforcerowsecurity::int
  FROM pg_class WHERE oid='memory.entity_resolution_reconciliation_v5_1'::regclass")" == '1:1' ]]
[[ "$(scalar "SELECT pg_get_userbyid(relowner)
  FROM pg_class WHERE oid='memory.entity_resolution_reconciliation_v5_1'::regclass")" \
  == memory_v5_writer ]]
[[ "$(scalar "SELECT has_table_privilege(
  'brains_app','memory.entity_resolution_reconciliation_v5_1','INSERT')::int")" == 0 ]]
[[ "$(scalar "SELECT has_function_privilege(
  'brains_app',
  'memory.reconcile_entity_resolution_v5_1(uuid,uuid,uuid,uuid,text,text)',
  'EXECUTE')::int")" == 1 ]]

run_sql <"$rollback"
[[ "$(scalar "SELECT to_regclass(
  'memory.entity_resolution_reconciliation_v5_1') IS NULL")" == t ]]
run_sql <"$migration"
[[ "$(scalar "SELECT count(*) FROM memory.entity_resolution_reconciliation_v5_1")" == 0 ]]

echo 'memory_v1_v5_1_entity_reconciliation_schema_clone: PASS'
