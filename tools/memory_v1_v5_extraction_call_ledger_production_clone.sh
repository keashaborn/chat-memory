#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_CALL_LEDGER_CLONE_PORT:-55454}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v5callledgerclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
migration=ops/sql/20260718_memory_v1_v5_extraction_call_ledger.sql
rollback=ops/sql/20260718_memory_v1_v5_extraction_call_ledger_rollback.sql
test_sql=tests/memory_v1_v5_extraction_call_ledger.sql
worker=scripts/memory_v1_v5_bounded_extraction_worker.py
worker_test=scripts/memory_v1_v5_bounded_extraction_worker_test.py

backup=$(mktemp /tmp/memory-v1-v5-call-ledger.XXXXXX.dump)
before=$(mktemp /tmp/memory-v1-v5-call-ledger-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-v5-call-ledger-after.XXXXXX.tsv)
plan_report=$(mktemp /tmp/memory-v1-v5-call-ledger-plan.XXXXXX.json)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$before" "$after" "$plan_report"
}
trap cleanup EXIT
chmod 0600 "$backup" "$before" "$after" "$plan_report"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory
}

run_as_brains() {
  "${compose[@]}" exec -T \
    -e PGPASSWORD=clone_only_brains_password postgres \
    psql -X -v ON_ERROR_STOP=1 -U brains_app -d memory
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
  done < <(scalar "
    SELECT table_schema || E'\\t' || table_name
    FROM information_schema.tables
    WHERE table_type='BASE TABLE'
      AND table_schema IN ('memory','public')
      AND table_name<>'v5_extraction_call_event'
    ORDER BY table_schema,table_name
  ")
}

for required in "$migration" "$rollback" "$test_sql" \
  "$worker" "$worker_test"; do
  [[ -f "$repo_root/$required" ]]
done

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
printf '%s\n' 'GRANT USAGE ON SCHEMA memory TO brains_app;' | run_sql
printf '%s\n' \
  'GRANT SELECT ON memory.evidence_extraction_job TO brains_app;' \
  | run_sql

capture_state "$before"
run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$migration"
run_as_brains <"$repo_root/$test_sql"

PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python \
  "$repo_root/$worker_test"

POSTGRES_DSN="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory" \
  PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python \
  "$repo_root/$worker" \
  --owner-user-id 1240822d-ac9a-4096-95aa-e2b24d36ef50 \
  --run-id 76666666-6666-4666-8666-666666666666 \
  --max-jobs 1 >"$plan_report"
PLAN_REPORT="$plan_report" python3 - <<'PY'
import json
import os
from pathlib import Path

report=json.loads(Path(os.environ['PLAN_REPORT']).read_text())
assert report['apply'] is False
assert report['route']=='relational_extraction'
assert report['external_model_calls']==0
assert report['write_counts']=={
    'queue':0,'packets':0,'candidates':0,'claims':0,
    'staging':0,'qdrant':0,'prompt_influence':0,
}
assert 'source' not in report
PY

[[ "$(scalar "SELECT count(*) FROM memory.v5_extraction_call_event")" == "0" ]]
run_sql <"$repo_root/$rollback"
[[ "$(scalar "SELECT (
  to_regclass('memory.v5_extraction_call_event') IS NULL
  AND to_regprocedure(
    'memory.claim_owner_v5_bounded_extraction_job_v1(uuid,uuid,text,text,integer,integer,text,text,text,integer,integer,integer)'
  ) IS NULL
  AND to_regprocedure(
    'memory.complete_owner_v5_extraction_call_v1(uuid,uuid,uuid,uuid,text,integer,text,text,text,text)'
  ) IS NULL
  AND to_regrole('memory_v5_extraction_scheduler_maintainer') IS NULL
)::integer")" == "1" ]]
capture_state "$after"
cmp -s "$before" "$after"

printf '%s\n' 'memory_v1_v5_extraction_call_ledger_production_clone: PASS'
