#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores production into an isolated PostgreSQL clone and
# verifies the private entailment schema, RLS, no-work planner, and rollback.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
port=${MEMORY_V1_V5_LOCAL_ENTAILMENT_CLONE_PORT:-55469}
project=memoryv1v5localentailmentclone
compose=(docker compose -p "$project" -f docker-compose.ci.yml \
  -f docker-compose.stage-batch-clone.yml)
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"
migration=ops/sql/20260719_memory_v1_v5_local_entailment.sql
rollback=ops/sql/20260719_memory_v1_v5_local_entailment_rollback.sql
sql_test=tests/memory_v1_v5_local_entailment.sql
provider=scripts/memory_v1_v5_local_entailment_provider.py
worker=scripts/memory_v1_v5_local_entailment_scheduler.py
worker_test=scripts/memory_v1_v5_local_entailment_test.py
smoke=scripts/memory_v1_v5_local_entailment_smoke.py
service=ops/systemd/memory-v1-v5-local-entailment.service
timer=ops/systemd/memory-v1-v5-local-entailment.timer

backup=$(mktemp /tmp/memory-v1-v5-local-entailment.XXXXXX.dump)
work=$(mktemp -d /tmp/memory-v1-v5-local-entailment.XXXXXX)
table_list="$work/tables.tsv"
before="$work/before.tsv"
after="$work/after.tsv"
dry="$work/dry.json"
other_dry="$work/other.json"

cleanup() {
  MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" down -v \
    >/dev/null 2>&1 || true
  rm -f "$backup"
  rm -rf "$work"
}
trap cleanup EXIT
chmod 0600 "$backup"

run_sql() {
  MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" exec -T postgres \
    psql -X -v ON_ERROR_STOP=1 -U sage -d memory "$@"
}

scalar() {
  MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" exec -T postgres \
    psql -X -A -t -v ON_ERROR_STOP=1 -U sage -d memory -c "$1" \
    | tr -d '[:space:]'
}

capture_rows() {
  local output=$1 schema table state
  : >"$output"
  while IFS=$'\t' read -r schema table; do
    state=$(scalar "SELECT count(*)::text || ':' ||
      encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
        ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
      FROM (SELECT to_jsonb(value)::text AS row_json
        FROM \"$schema\".\"$table\" AS value) rows")
    printf '%s\t%s\t%s\n' "$schema" "$table" "$state" >>"$output"
  done <"$table_list"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

for required in "$migration" "$rollback" "$sql_test" "$provider" \
  "$worker" "$worker_test" "$smoke" "$service" "$timer"; do
  [[ -f "$required" ]]
done
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$worker_test"
python3 -m py_compile "$provider" "$worker" "$smoke"
systemd-analyze verify "$service" "$timer"

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc --no-owner --no-privileges >"$backup"
[[ -s "$backup" ]]
MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" up -d --wait postgres
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
  'CREATE ROLE memory_extraction_retry_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_extraction_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_extraction_scheduler_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_inference_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_review_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_disposition_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  | run_sql >/dev/null
MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" exec -T postgres \
  pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"
printf '%s\n' 'GRANT USAGE ON SCHEMA memory TO brains_app;' | run_sql >/dev/null

run_sql -At -c "SELECT table_schema || E'\\t' || table_name
  FROM information_schema.tables WHERE table_type='BASE TABLE'
    AND table_schema IN ('memory','public')
  ORDER BY table_schema,table_name" >"$table_list"
capture_rows "$before"
qdrant_before=$(qdrant_signature)

run_sql <"$migration" >/dev/null
run_sql <"$migration" >/dev/null
run_sql -v owner_user_id="$owner" <"$sql_test" >/dev/null
[[ "$(scalar 'SELECT count(*) FROM memory.v5_local_entailment_assessment')" == 0 ]]

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$owner" >"$dry"
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$other" >"$other_dry"
jq -e '.apply==false and .plans[0].route=="no_work" and
  .database_writes==0 and .local_model_calls==0 and
  .external_model_calls==0 and .qdrant_writes==0 and
  .claim_writes==0 and .projection_writes==0 and
  .prompt_influence==0' "$dry" >/dev/null
jq -e '.apply==false and .plans[0].route=="no_work" and
  .database_writes==0' "$other_dry" >/dev/null

capture_rows "$after"
cmp -s "$before" "$after"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
run_sql <"$rollback" >/dev/null
[[ "$(scalar "SELECT to_regclass('memory.v5_local_entailment_assessment') IS NULL")" == t ]]

printf '%s\n' 'memory_v1_v5_local_entailment_clone: PASS'
