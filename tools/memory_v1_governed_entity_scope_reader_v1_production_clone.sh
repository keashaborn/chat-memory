#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
compose=(docker compose -p memoryv1entityscopereader -f docker-compose.ci.yml)
migration=ops/sql/20260724_memory_v1_governed_entity_scope_reader_v1.sql
rollback=ops/sql/20260724_memory_v1_governed_entity_scope_reader_v1_rollback.sql
test_sql=tests/memory_v1_governed_entity_scope_reader_v1.sql
probe=scripts/memory_v1_entity_scope_v2_clone_probe.py
backup=$(mktemp /tmp/memory-v1-entity-scope-reader.XXXXXX.dump)
before=$(mktemp /tmp/memory-v1-entity-scope-reader-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-entity-scope-reader-after.XXXXXX.tsv)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$before" "$after"
}
trap cleanup EXIT
chmod 0600 "$backup" "$before" "$after"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory
}

run_as_brains() {
  "${compose[@]}" exec -T \
    -e PGPASSWORD=clone_only_brains_password postgres \
    psql -X -v ON_ERROR_STOP=1 -U brains_app -d memory "$@"
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
  done < <(scalar "
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name
  ")
}

for required in "$migration" "$rollback" "$test_sql" "$probe"; do
  [[ -f "$repo_root/$required" ]]
done

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc --no-owner --no-privileges >"$backup"
[[ -s "$backup" ]]

"${compose[@]}" up -d --wait postgres
memory_role_sql=$(docker exec brains-postgres-1 psql -X -A -t \
  -U sage -d memory -v ON_ERROR_STOP=1 -c "
    SELECT format(
      'CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;',
      rolname
    )
    FROM pg_roles
    WHERE rolname LIKE 'memory\\_%' ESCAPE '\\'
    ORDER BY rolname
  ")
[[ -n "$memory_role_sql" ]]
printf '%s\n' \
  "CREATE ROLE brains_app LOGIN PASSWORD 'clone_only_brains_password' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;" \
  "$memory_role_sql" \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"

run_sql <<'SQL'
ALTER FUNCTION memory.current_actor_user_id() OWNER TO memory_v5_writer;
ALTER FUNCTION memory.require_v5_reader_context() OWNER TO memory_v5_reader;
GRANT USAGE ON SCHEMA memory TO brains_app,memory_v5_reader;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id() TO memory_v5_reader;
GRANT EXECUTE ON FUNCTION memory.require_v5_reader_context() TO memory_v5_reader;
SQL

capture_state "$before"
run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$migration"

[[ "$(scalar "
  SELECT count(*)
  FROM pg_proc AS p
  JOIN pg_namespace AS n ON n.oid=p.pronamespace
  WHERE n.nspname='memory'
    AND p.proname IN (
      'read_governed_entity_scope_entities_v1',
      'read_governed_entity_scope_edges_v1'
    )
    AND p.prosecdef
    AND p.provolatile='s'
    AND pg_get_userbyid(p.proowner)='memory_v5_reader'
    AND NOT has_function_privilege('public',p.oid,'EXECUTE')
    AND has_function_privilege('brains_app',p.oid,'EXECUTE')
")" == "2" ]]

run_as_brains \
  -v target_owner_user_id="1240822d-ac9a-4096-95aa-e2b24d36ef50" \
  -v other_owner_user_id="557ea042-cb82-48f8-9429-472e96c957ef" \
  <"$repo_root/$test_sql"

"${compose[@]}" build brains >/dev/null
"${compose[@]}" run --rm --no-deps -T \
  -e POSTGRES_DSN=postgresql://brains_app:clone_only_brains_password@postgres:5432/memory \
  brains python "$probe" \
    --dsn postgresql://brains_app:clone_only_brains_password@postgres:5432/memory \
    --owner-user-id 1240822d-ac9a-4096-95aa-e2b24d36ef50

capture_state "$after"
diff -u "$before" "$after"

if run_sql <<'SQL'
SET SESSION AUTHORIZATION sage;
SELECT count(*) FROM memory.read_governed_entity_scope_entities_v1();
SQL
then
  echo 'non-brains session unexpectedly passed entity-scope reader' >&2
  exit 1
fi

run_sql <"$repo_root/$rollback"
[[ "$(scalar "
  SELECT (
    to_regprocedure('memory.read_governed_entity_scope_entities_v1()') IS NULL
    AND to_regprocedure('memory.read_governed_entity_scope_edges_v1()') IS NULL
  )::integer
")" == "1" ]]

echo 'memory_v1_governed_entity_scope_reader_v1_production_clone: PASS'
