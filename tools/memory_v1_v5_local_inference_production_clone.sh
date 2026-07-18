#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores a temporary production database into an
# isolated Docker clone. No model endpoint or external network is used.

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_LOCAL_INFERENCE_CLONE_PORT:-55458}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v5localinferenceclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
migration=ops/sql/20260718_memory_v1_v5_local_inference.sql
rollback=ops/sql/20260718_memory_v1_v5_local_inference_rollback.sql
test_sql=tests/memory_v1_v5_local_inference.sql
migration_sha=d74272e9c40c306e78b6166981c4c8cfbc87e156980ca87340a35b95e9892b74
rollback_sha=ed21cb3e686385728828b90cc188e943fba43c52534b229c19c1438e27ee44d9
test_sha=5c4b30056bfa87eb47f11b8f0e788a85e6fb940198020f72c319b9c8224c59ab

backup=$(mktemp /tmp/memory-v1-v5-local-inference.XXXXXX.dump)
tables=$(mktemp /tmp/memory-v1-v5-local-inference-tables.XXXXXX.txt)
before=$(mktemp /tmp/memory-v1-v5-local-inference-before.XXXXXX.tsv)
after_test=$(mktemp /tmp/memory-v1-v5-local-inference-after-test.XXXXXX.tsv)
after_rollback=$(mktemp /tmp/memory-v1-v5-local-inference-after-rollback.XXXXXX.tsv)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$tables" "$before" "$after_test" "$after_rollback"
}
trap cleanup EXIT
chmod 0600 "$backup" "$tables" "$before" "$after_test" "$after_rollback"

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

for required in "$migration" "$rollback" "$test_sql"; do
  [[ -f "$repo_root/$required" ]]
done
[[ "$(sha256sum "$repo_root/$migration" | cut -d' ' -f1)" == "$migration_sha" ]]
[[ "$(sha256sum "$repo_root/$rollback" | cut -d' ' -f1)" == "$rollback_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | cut -d' ' -f1)" == "$test_sha" ]]

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
  'CREATE ROLE memory_v5_extraction_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_trace_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"
printf '%s\n' \
  'GRANT USAGE ON SCHEMA memory TO brains_app;' \
  'GRANT EXECUTE ON FUNCTION memory.current_actor_user_id() TO brains_app;' \
  | run_sql

preexisting_local=$(scalar "SELECT (
  to_regclass('memory.v5_local_inference_event') IS NOT NULL
  AND to_regclass('memory.evidence_extraction_packet_v5_local') IS NOT NULL
)::integer")
local_events_before=0
local_packets_before=0
if [[ "$preexisting_local" == 1 ]]; then
  local_events_before=$(scalar 'SELECT count(*) FROM memory.v5_local_inference_event')
  local_packets_before=$(scalar 'SELECT count(*) FROM memory.evidence_extraction_packet_v5_local')
fi
scalar "
  SELECT table_schema || E'\\t' || table_name
  FROM information_schema.tables
  WHERE table_type='BASE TABLE'
    AND table_schema IN ('memory','public')
    AND ($preexisting_local=1 OR table_name NOT IN (
      'v5_local_inference_event','evidence_extraction_packet_v5_local'
    ))
  ORDER BY table_schema,table_name
" >"$tables"
capture_state "$before"

run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$test_sql"

[[ "$(scalar 'SELECT count(*) FROM memory.v5_local_inference_event')" == "$local_events_before" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_extraction_packet_v5_local')" == "$local_packets_before" ]]
[[ "$(scalar "SELECT (
  (SELECT count(*)=2 FROM pg_class
    WHERE oid IN (
      'memory.v5_local_inference_event'::regclass,
      'memory.evidence_extraction_packet_v5_local'::regclass
    ) AND relrowsecurity AND relforcerowsecurity)
  AND NOT (
    has_table_privilege('brains_app','memory.v5_local_inference_event','SELECT')
    OR has_table_privilege('brains_app','memory.v5_local_inference_event','INSERT')
    OR has_table_privilege('brains_app','memory.v5_local_inference_event','UPDATE')
    OR has_table_privilege('brains_app','memory.v5_local_inference_event','DELETE')
    OR has_table_privilege(
      'brains_app','memory.evidence_extraction_packet_v5_local','SELECT'
    )
    OR has_table_privilege(
      'brains_app','memory.evidence_extraction_packet_v5_local','INSERT'
    )
    OR has_table_privilege(
      'brains_app','memory.evidence_extraction_packet_v5_local','UPDATE'
    )
    OR has_table_privilege(
      'brains_app','memory.evidence_extraction_packet_v5_local','DELETE'
    )
  )
  AND (SELECT count(*)=3 FROM pg_proc
    WHERE oid IN (
      'memory.claim_owner_v5_local_inference_job_v1(uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,integer,integer,integer)'::regprocedure,
      'memory.persist_owner_v5_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure,
      'memory.complete_owner_v5_local_inference_v1(uuid,uuid,uuid,uuid,text,integer,text,text,text,text)'::regprocedure
    )
    AND prosecdef
    AND proowner='memory_v5_local_inference_maintainer'::regrole
    AND proconfig=ARRAY['search_path=pg_catalog']::text[])
)::integer")" == "1" ]]
capture_state "$after_test"
cmp -s "$before" "$after_test"

if [[ "$preexisting_local" == 1 ]]; then
  printf '%s\n' 'memory_v1_v5_local_inference_production_clone: PASS (compatibility)'
  exit 0
fi

run_sql <"$repo_root/$rollback"
[[ "$(scalar "SELECT (
  to_regclass('memory.v5_local_inference_event') IS NULL
  AND to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
  AND to_regprocedure(
    'memory.claim_owner_v5_local_inference_job_v1(uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,integer,integer,integer)'
  ) IS NULL
  AND to_regprocedure(
    'memory.persist_owner_v5_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'
  ) IS NULL
  AND to_regprocedure(
    'memory.complete_owner_v5_local_inference_v1(uuid,uuid,uuid,uuid,text,integer,text,text,text,text)'
  ) IS NULL
  AND to_regrole('memory_v5_local_inference_maintainer') IS NULL
)::integer")" == "1" ]]
capture_state "$after_rollback"
cmp -s "$before" "$after_rollback"

printf '%s\n' 'memory_v1_v5_local_inference_production_clone: PASS'
