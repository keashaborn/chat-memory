#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_PROJECT_COMPONENT_ENTITY_CLONE_PORT:-55449}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(docker compose -p memoryv1v5projectcomponententityclone \
  -f docker-compose.ci.yml -f docker-compose.stage-batch-clone.yml)
migration=ops/sql/20260717_memory_v1_v5_project_component_entity.sql
rollback=ops/sql/20260717_memory_v1_v5_project_component_entity_rollback.sql
test_sql=tests/memory_v1_v5_project_component_entity.sql
backup=$(mktemp /tmp/memory-v1-v5-project-component-entity.XXXXXX.dump)
before=$(mktemp /tmp/memory-v1-v5-project-component-entity-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-v5-project-component-entity-after.XXXXXX.tsv)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$before" "$after"
}
trap cleanup EXIT
chmod 0600 "$backup" "$before" "$after"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 -U sage -d memory
}
scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}
capture_state() {
  local output=$1
  : >"$output"
  while IFS=$'\t' read -r schema table; do
    state=$(scalar "SELECT count(*)::text||E'\\t'||encode(public.digest(
      convert_to(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'UTF8'),
      'sha256'),'hex') FROM (
        SELECT to_jsonb(row_value)::text AS row_json
        FROM \"$schema\".\"$table\" AS row_value
      ) AS rows")
    printf '%s\t%s\t%s\n' "$schema" "$table" "$state" >>"$output"
  done < <(scalar "SELECT table_schema||E'\\t'||table_name
    FROM information_schema.tables
    WHERE table_type='BASE TABLE' AND table_schema IN ('memory','public')
      AND table_name<>'project_component_entity_binding_v5'
    ORDER BY table_schema,table_name")
}

for required in "$migration" "$rollback" "$test_sql"; do
  [[ -f "$repo_root/$required" ]]
done
docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc --no-owner --no-privileges >"$backup"
[[ -s "$backup" ]]
"${compose[@]}" up -d --wait postgres
printf '%s\n' \
  "CREATE ROLE brains_app LOGIN PASSWORD 'clone_only_brains_password' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;" \
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
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"
printf '%s\n' \
  'GRANT USAGE ON SCHEMA memory TO brains_app,memory_v5_writer;' \
  'GRANT EXECUTE ON FUNCTION memory.current_actor_user_id() TO memory_v5_writer;' \
  'GRANT EXECUTE ON FUNCTION memory.require_v5_writer_context() TO memory_v5_writer;' \
  'GRANT EXECUTE ON FUNCTION memory.v5_sha256_valid(text) TO memory_v5_writer;' \
  'GRANT EXECUTE ON FUNCTION memory.v5_digest_text(text) TO memory_v5_writer;' \
  'GRANT EXECUTE ON FUNCTION memory.normalize_entity_name_v5(text) TO memory_v5_writer;' \
  'GRANT SELECT,INSERT ON memory.entity,memory.entity_alias TO memory_v5_writer;' \
  'GRANT SELECT ON memory.project_space,memory.project_component_v5,memory.project_component_alias_v5 TO memory_v5_writer;' \
  | run_sql
capture_state "$before"
run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$test_sql"
[[ "$(scalar 'SELECT count(*) FROM memory.project_component_entity_binding_v5')" == 0 ]]
run_sql <"$repo_root/$rollback"
[[ "$(scalar "SELECT (
  to_regclass('memory.project_component_entity_binding_v5') IS NULL
  AND to_regprocedure('memory.preflight_owner_project_component_entity_v5(uuid,uuid)') IS NULL
  AND to_regprocedure('memory.bootstrap_owner_project_component_entity_v5(uuid,uuid,uuid,text)') IS NULL
  AND to_regprocedure('memory.resolve_owner_project_component_entity_candidate_v5(text,text,text)') IS NULL
)::integer")" == 1 ]]
capture_state "$after"
cmp -s "$before" "$after"
printf '%s\n' 'memory_v1_v5_project_component_entity_production_clone: PASS'
