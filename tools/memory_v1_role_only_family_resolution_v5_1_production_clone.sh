#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores a current production backup into a disposable
# Postgres clone and exercises the role-only family reconciliation path inside
# a transaction that always rolls back. Production is read-only.

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_ROLE_ONLY_FAMILY_CLONE_PORT:-55473}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1rolefamilyclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
migration=ops/sql/20260721_memory_v1_role_only_family_resolution_v5_1.sql
rollback=ops/sql/20260721_memory_v1_role_only_family_resolution_v5_1_rollback.sql
security_test=tests/memory_v1_role_only_family_resolution_v5_1.sql
backup=$(mktemp /tmp/memory-v1-role-family.XXXXXX.dump)
roles=$(mktemp /tmp/memory-v1-role-family-roles.XXXXXX.sql)
baseline=$(mktemp /tmp/memory-v1-role-family-before.XXXXXX.tsv)
post=$(mktemp /tmp/memory-v1-role-family-after.XXXXXX.tsv)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$roles" "$baseline" "$post"
}
trap cleanup EXIT
chmod 0600 "$backup" "$roles" "$baseline" "$post"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory "$@"
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

capture_state() {
  local output=$1 table state
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    state=$(scalar "
      SELECT count(*)::text || E'\\t' ||
             encode(public.digest(convert_to(coalesce(string_agg(
               row_json,E'\\n' ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(table_row)::text AS row_json
        FROM memory.\"$table\" AS table_row
      ) rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done < <(scalar "
    SELECT table_name FROM information_schema.tables
    WHERE table_schema='memory' AND table_type='BASE TABLE'
      AND table_name <> 'entity_role_resolution_v5_1'
    ORDER BY table_name
  ")
}

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc >"$backup"
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
  --clean --if-exists <"$backup"

[[ "$(scalar "SELECT to_regclass(
  'memory.entity_role_resolution_v5_1') IS NULL")" == t ]]
capture_state "$baseline"

run_sql <"$migration"
run_sql <"$migration"
[[ "$(scalar "SELECT count(*) FROM memory.entity_role_resolution_v5_1")" == 0 ]]
[[ "$(scalar "SELECT relrowsecurity::int || ':' || relforcerowsecurity::int
  FROM pg_class WHERE oid='memory.entity_role_resolution_v5_1'::regclass")" == '1:1' ]]
[[ "$(scalar "SELECT pg_get_userbyid(relowner)
  FROM pg_class WHERE oid='memory.entity_role_resolution_v5_1'::regclass")" \
  == memory_v5_writer ]]
[[ "$(scalar "SELECT has_table_privilege(
  'brains_app','memory.entity_role_resolution_v5_1','INSERT')::int")" == 0 ]]
[[ "$(scalar "SELECT has_function_privilege(
  'brains_app',
  'memory.apply_role_only_family_resolution_v5_1(uuid,uuid,uuid,text)',
  'EXECUTE')::int")" == 1 ]]

run_sql <<'SQL'
CREATE OR REPLACE FUNCTION memory.clone_test_role_family_state(p_owner uuid)
RETURNS jsonb
LANGUAGE sql
SECURITY DEFINER
SET search_path = ''
AS $function$
  SELECT jsonb_build_object(
    'entity_count', (
      SELECT count(*) FROM memory.entity
      WHERE owner_user_id = p_owner
    ),
    'plan_count', (
      SELECT count(*) FROM memory.entity_resolution_plan
      WHERE owner_user_id = p_owner
    ),
    'review_count', (
      SELECT count(*) FROM memory.entity_resolution_review
      WHERE owner_user_id = p_owner
    ),
    'apply_count', (
      SELECT count(*) FROM memory.entity_resolution_apply
      WHERE owner_user_id = p_owner
    ),
    'binding_count', (
      SELECT count(*) FROM memory.observation_entity_binding
      WHERE owner_user_id = p_owner
    ),
    'request_count', (
      SELECT count(*) FROM memory.relational_operation_request
      WHERE owner_user_id = p_owner
    ),
    'reconciliation_count', (
      SELECT count(*) FROM memory.entity_role_resolution_v5_1
      WHERE owner_user_id = p_owner
    ),
    'mother_count', (
      SELECT count(*) FROM memory.entity
      WHERE owner_user_id = p_owner
        AND entity_type = 'person'
        AND canonical_name = 'mother'
        AND metadata->>'identity_state' = 'role_only'
        AND metadata->>'relationship_role' = 'family:mother'
        AND metadata->>'resolution_id' =
            'eaf253ec-b34f-46a5-8f8c-a543d2679d4b'
    ),
    'death_binding_count', (
      SELECT count(*)
      FROM memory.observation_entity_binding AS binding
      JOIN memory.observation AS observation
        ON observation.owner_user_id = binding.owner_user_id
       AND observation.observation_id = binding.observation_id
      JOIN memory.entity AS entity
        ON entity.owner_user_id = binding.owner_user_id
       AND entity.entity_id = binding.subject_entity_id
      WHERE binding.owner_user_id = p_owner
        AND entity.metadata->>'resolution_id' =
            'eaf253ec-b34f-46a5-8f8c-a543d2679d4b'
        AND observation.predicate = 'life_event.died'
        AND observation.object_literal->>'value' = 'true'
    )
  );
$function$;
REVOKE ALL ON FUNCTION memory.clone_test_role_family_state(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.clone_test_role_family_state(uuid)
  TO brains_app;
SQL

PGPASSWORD=clone_only_brains_password psql \
  "postgresql://brains_app@127.0.0.1:${port}/memory" \
  -X -v ON_ERROR_STOP=1 <"$security_test"

capture_state "$post"
cmp -s "$baseline" "$post"

run_sql <"$rollback"
[[ "$(scalar "SELECT to_regclass(
  'memory.entity_role_resolution_v5_1') IS NULL")" == t ]]
run_sql <"$migration"
[[ "$(scalar "SELECT count(*) FROM memory.entity_role_resolution_v5_1")" == 0 ]]

echo 'memory_v1_role_only_family_resolution_v5_1_production_clone: PASS'
