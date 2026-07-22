#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Exercises compiler-v3 persistence, bounded attempt four,
# and exact recovery on a disposable production clone.

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_2_COMPILER_V3_CLONE_PORT:-55478}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(docker compose -p memoryv1v52compilerv3clone
  -f docker-compose.ci.yml -f docker-compose.stage-batch-clone.yml)
migration=ops/sql/20260722_memory_v1_v5_2_compiler_v3_persistence.sql
rollback=ops/sql/20260722_memory_v1_v5_2_compiler_v3_persistence_rollback.sql
test_sql=tests/memory_v1_v5_2_compiler_v3_persistence.sql
expected_migration_sha=a34ab2fd57dfb5b3d3831f60811822942933f21c491a9b76a08e28747b7f9fa0
expected_rollback_sha=9057494079e44ea9e5d86461f8e6a0123593a48db8536cc1f1c8391d8f1b5b68
expected_test_sha=a8f4d4f60ed48bcc0b46aafa66240c989ed107051839fccd5667c0ace7a00a4a
backup=$(mktemp /tmp/memory-v1-v5-2-compiler-v3.XXXXXX.dump)
tables=$(mktemp /tmp/memory-v1-v5-2-compiler-v3-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-2-compiler-v3-before.XXXXXX)
after=$(mktemp /tmp/memory-v1-v5-2-compiler-v3-after.XXXXXX)
cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$tables" "$before" "$after"
}
trap cleanup EXIT
chmod 0600 "$backup" "$tables" "$before" "$after"
run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory
}
scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}
capture_state() {
  local output=$1 schema table state
  : >"$output"
  while IFS=$'\t' read -r schema table; do
    state=$(scalar "SELECT count(*)::text || E'\\t' ||
      encode(public.digest(convert_to(coalesce(string_agg(row_value,E'\\n'
        ORDER BY row_value),''),'UTF8'),'sha256'),'hex')
      FROM (SELECT to_jsonb(value)::text AS row_value
        FROM \"$schema\".\"$table\" AS value) AS rows")
    printf '%s\t%s\t%s\n' "$schema" "$table" "$state" >>"$output"
  done <"$tables"
}
for required in "$migration" "$rollback" "$test_sql"; do
  [[ -f "$repo_root/$required" ]]
done
[[ "$(sha256sum "$repo_root/$migration" | cut -d' ' -f1)" == "$expected_migration_sha" ]]
[[ "$(sha256sum "$repo_root/$rollback" | cut -d' ' -f1)" == "$expected_rollback_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | cut -d' ' -f1)" == "$expected_test_sha" ]]
docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc --no-owner >"$backup"
[[ -s "$backup" ]]
"${compose[@]}" up -d --wait postgres
printf '%s\n' \
  "CREATE ROLE brains_app LOGIN PASSWORD 'clone_only_brains_password' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;" \
  'CREATE ROLE lifeswitch_training_observation_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
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
  --clean --if-exists --no-owner <"$backup"
printf '%s\n' 'GRANT USAGE ON SCHEMA memory TO brains_app;' \
  'GRANT EXECUTE ON FUNCTION memory.current_actor_user_id() TO brains_app;' \
  | run_sql
scalar "SELECT table_schema || E'\\t' || table_name
  FROM information_schema.tables WHERE table_type='BASE TABLE'
    AND table_schema='memory' ORDER BY table_schema,table_name" >"$tables"
capture_state "$before"
[[ "$(scalar "SELECT encode(public.digest(convert_to(pg_get_functiondef(
  'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure
  ),'UTF8'),'sha256'),'hex')")" == a8a8e97bba56577a1f7bf0fbee932b1094b4e6c50200891eedb7c1970bcec3fd ]]
[[ "$(scalar "SELECT encode(public.digest(convert_to(pg_get_functiondef(
  'memory.claim_owner_v5_local_inference_job_v1(uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,integer,integer,integer)'::regprocedure
  ),'UTF8'),'sha256'),'hex')")" == 01052eb7ea5a2add2a9795bcc960408a7702fdc1baee83e17b12722889307970 ]]
run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$test_sql"
capture_state "$after"
cmp -s "$before" "$after"
run_sql <"$repo_root/$rollback"
[[ "$(scalar "SELECT (to_regprocedure(
  'memory.requeue_owner_v5_2_persistence_mismatch_v1(uuid,uuid,text,uuid,uuid,integer,text)'
) IS NULL)::integer")" == 1 ]]
printf '%s\n' 'memory_v1_v5_2_compiler_v3_persistence_production_clone: PASS'
