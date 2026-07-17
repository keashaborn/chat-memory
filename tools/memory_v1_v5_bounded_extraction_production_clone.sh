#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_BOUNDED_EXTRACTION_CLONE_PORT:-55445}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v5boundedextractionclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"

prerequisites=(
  ops/sql/20260713_memory_v1_evidence_lifecycle.sql
  ops/sql/20260716_memory_v1_record_evidence_api.sql
  ops/sql/20260716_memory_v1_revoke_direct_evidence_insert.sql
  ops/sql/20260716_memory_v1_evidence_intake_selector.sql
  ops/sql/20260716_memory_v1_evidence_extraction_queue.sql
  ops/sql/20260716_memory_v1_evidence_extraction_worker.sql
)
migration=ops/sql/20260717_memory_v1_v5_bounded_extraction.sql
rollback=ops/sql/20260717_memory_v1_v5_bounded_extraction_rollback.sql
test_sql=tests/memory_v1_v5_bounded_extraction.sql
provider_test=scripts/memory_v1_relational_extraction_v5_provider_test.py
openai_provider_test=scripts/memory_v1_relational_extraction_v5_openai_provider_test.py
worker=scripts/memory_v1_v5_bounded_extraction_worker.py
worker_test=scripts/memory_v1_v5_bounded_extraction_worker_test.py

backup=$(mktemp /tmp/memory-v1-v5-bounded.XXXXXX.dump)
before=$(mktemp /tmp/memory-v1-v5-bounded-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-v5-bounded-after.XXXXXX.tsv)
plan_report=$(mktemp /tmp/memory-v1-v5-bounded-plan.XXXXXX.json)

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
      AND table_name NOT IN (
        'project_thread_binding_event','evidence_extraction_packet_v5'
      )
    ORDER BY table_schema,table_name
  ")
}

for required in \
  "${prerequisites[@]}" "$migration" "$rollback" "$test_sql" \
  "$provider_test" "$openai_provider_test" "$worker" "$worker_test"; do
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
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"
printf '%s\n' 'GRANT USAGE ON SCHEMA memory TO brains_app;' | run_sql

for prerequisite in "${prerequisites[@]}"; do
  run_sql <"$repo_root/$prerequisite"
done

capture_state "$before"
run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$test_sql"

[[ "$(scalar "SELECT count(*) FROM memory.project_thread_binding_event")" == "0" ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_packet_v5")" == "0" ]]

PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python \
  "$repo_root/$provider_test"
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python \
  "$repo_root/$openai_provider_test"
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python \
  "$repo_root/$worker_test"

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$worker" \
  --owner-user-id 1240822d-ac9a-4096-95aa-e2b24d36ef50 \
  --run-id e10c63a7-f30d-43f2-932d-e85206a907d7 \
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

run_sql <"$repo_root/$rollback"
[[ "$(scalar "SELECT (to_regclass('memory.project_thread_binding_event') IS NULL AND to_regclass('memory.evidence_extraction_packet_v5') IS NULL AND to_regrole('memory_v5_extraction_maintainer') IS NULL)::integer")" == "1" ]]
capture_state "$after"
cmp -s "$before" "$after"

printf '%s\n' 'memory_v1_v5_bounded_extraction_production_clone: PASS'
