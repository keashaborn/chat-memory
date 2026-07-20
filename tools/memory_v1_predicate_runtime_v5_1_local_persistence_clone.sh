#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores production into an isolated Docker clone,
# installs the additive V5.1 persistence function, and runs rollback-only tests.

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_1_PERSISTENCE_CLONE_PORT:-55461}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v51persistenceclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
base_migration=ops/sql/20260718_memory_v1_v5_local_inference.sql
registry_generator=scripts/memory_v1_predicate_registry_v5_1.py
registry_integration=specs/memory_v1_predicate_registry_v5_1_integration.json
relationship_registry=specs/memory_v1_relationship_registry_v5_1.json
migration=ops/sql/20260720_memory_v1_predicate_runtime_v5_1_local_persistence.sql
test_sql=tests/memory_v1_predicate_runtime_v5_1_local_persistence.sql
base_migration_sha=2825d5e0d0d079f1ba647c047985cef886e116eab75e2719e24357748775fd65
registry_generator_sha=4b3ca8357f5b269024b531c157de84cf8bcf6fc3a13ae3cb8829ee3f83708f90
registry_integration_sha=12e132f03554adb041090fa74ed99c178a20c5625cade85dc1bfb52b43a61eaf
relationship_registry_sha=01d0045cf5607b55eb7d2b97611c5810b81c6098c6118435a2015d5cc8102a4f
migration_sha=658fce782842ddb096433c6ad07f49184eefb35000bf60608e584e86656edc23
test_sha=1382ebaa5cd67a774562e68870c92dea833cebe57b18a57800ae4fbd983b4d8b

backup=$(mktemp /tmp/memory-v1-v5-1-persistence.XXXXXX.dump)
tables=$(mktemp /tmp/memory-v1-v5-1-persistence-tables.XXXXXX.txt)
before=$(mktemp /tmp/memory-v1-v5-1-persistence-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-v5-1-persistence-after.XXXXXX.tsv)
registry_install=$(mktemp /tmp/memory-v1-v5-1-registry.XXXXXX.sql)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$tables" "$before" "$after" "$registry_install"
}
trap cleanup EXIT
chmod 0600 "$backup" "$tables" "$before" "$after" "$registry_install"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

capture_state() {
  local output=$1
  : >"$output"
  while IFS=$'\t' read -r schema table; do
    state=$(scalar "
      SELECT count(*)::text || E'\\t' || encode(
        public.digest(
          convert_to(
            coalesce(string_agg(row_value,E'\\n' ORDER BY row_value),''),
            'UTF8'
          ),
          'sha256'
        ),
        'hex'
      )
      FROM (
        SELECT to_jsonb(row_value)::text AS row_value
        FROM \"$schema\".\"$table\" AS row_value
      ) AS rows
    ")
    printf '%s\t%s\t%s\n' "$schema" "$table" "$state" >>"$output"
  done <"$tables"
}

for required in "$base_migration" "$registry_generator" \
  "$registry_integration" "$relationship_registry" "$migration" "$test_sql"; do
  [[ -f "$repo_root/$required" ]]
done
[[ "$(sha256sum "$repo_root/$base_migration" | cut -d' ' -f1)" == "$base_migration_sha" ]]
[[ "$(sha256sum "$repo_root/$registry_generator" | cut -d' ' -f1)" == "$registry_generator_sha" ]]
[[ "$(sha256sum "$repo_root/$registry_integration" | cut -d' ' -f1)" == "$registry_integration_sha" ]]
[[ "$(sha256sum "$repo_root/$relationship_registry" | cut -d' ' -f1)" == "$relationship_registry_sha" ]]
[[ "$(sha256sum "$repo_root/$migration" | cut -d' ' -f1)" == "$migration_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | cut -d' ' -f1)" == "$test_sha" ]]

/opt/chat-memory/venv/bin/python "$repo_root/$registry_generator" \
  --emit-install-sql >"$registry_install"
[[ -s "$registry_install" ]]

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc --no-owner --no-privileges >"$backup"
[[ -s "$backup" ]]

"${compose[@]}" up -d --wait postgres
printf '%s\n' \
  "CREATE ROLE brains_app LOGIN PASSWORD 'clone_only_brains_password' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;" \
  'CREATE ROLE memory_evidence_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_extraction_queue_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_extraction_retry_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_extraction_worker_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_intake_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_review_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_epistemic_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_extraction_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_extraction_scheduler_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_disposition_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_entailment_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_entity_validation_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_inference_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_projection_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_reextract_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_review_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_supersession_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_reviewed_stage_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_trace_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"
printf '%s\n' \
  'GRANT USAGE ON SCHEMA memory TO brains_app;' \
  'GRANT EXECUTE ON FUNCTION memory.current_actor_user_id() TO brains_app;' \
  | run_sql

run_sql <"$repo_root/$base_migration"
run_sql <"$registry_install"
run_sql <"$registry_install"
[[ "$(scalar "SELECT count(*) FROM memory.predicate_contract WHERE registry_version='memory_predicate_registry_v5_1'")" == 82 ]]
[[ "$(scalar "SELECT count(*) FROM memory.relationship_predicate_contract_v5_1")" == 41 ]]

scalar "
  SELECT table_schema || E'\\t' || table_name
  FROM information_schema.tables
  WHERE table_type='BASE TABLE' AND table_schema IN ('memory','public')
  ORDER BY table_schema,table_name
" >"$tables"
capture_state "$before"

old_v5_definition=$(scalar "
  SELECT encode(public.digest(convert_to(pg_get_functiondef(
    'memory.persist_owner_v5_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure
  ),'UTF8'),'sha256'),'hex')
")

run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$test_sql"

[[ "$(scalar "
  SELECT (
    to_regprocedure(
      'memory.persist_owner_v5_1_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'
    ) IS NOT NULL
    AND has_function_privilege(
      'brains_app',
      'memory.persist_owner_v5_1_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)',
      'EXECUTE'
    )
    AND NOT EXISTS (
      SELECT 1
      FROM pg_proc AS procedure
      CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
      WHERE procedure.oid=
        'memory.persist_owner_v5_1_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure
        AND acl.grantee=0 AND acl.privilege_type='EXECUTE'
    )
    AND EXISTS (
      SELECT 1 FROM pg_proc
      WHERE oid='memory.persist_owner_v5_1_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure
        AND prosecdef
        AND proowner='memory_v5_local_inference_maintainer'::regrole
        AND EXISTS (
          SELECT 1 FROM unnest(proconfig) AS setting
          WHERE setting LIKE 'search_path=%'
        )
    )
  )::integer
")" == 1 ]]

[[ "$(scalar "
  SELECT encode(public.digest(convert_to(pg_get_functiondef(
    'memory.persist_owner_v5_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure
  ),'UTF8'),'sha256'),'hex')
")" == "$old_v5_definition" ]]

capture_state "$after"
cmp -s "$before" "$after"

printf '%s\n' 'memory_v1_predicate_runtime_v5_1_local_persistence_clone: PASS'
