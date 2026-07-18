#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores production into an isolated clone and proves
# that the restricted disposition role can see owner-scoped stage rows only,
# without changing any row data or Qdrant.

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_LOCAL_DISPOSITION_VISIBILITY_CLONE_PORT:-55464}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(docker compose -p memoryv1v5localvisibilityclone \
  -f docker-compose.ci.yml -f docker-compose.stage-batch-clone.yml)
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"
foundation=ops/sql/20260718_memory_v1_v5_local_packet_disposition.sql
migration=ops/sql/20260718_memory_v1_v5_local_packet_disposition_stage_visibility.sql
rollback=ops/sql/20260718_memory_v1_v5_local_packet_disposition_stage_visibility_rollback.sql
sql_test=tests/memory_v1_v5_local_packet_disposition_stage_visibility.sql
worker=scripts/memory_v1_v5_local_packet_disposition.py
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
staged_packet=2fa0db2a-0636-5353-9f3f-3f952bd4632b

declare -A expected_sha256=(
  ["$foundation"]="f09e7c2eb6aaa75ea085c352f480bee9daf1258b43bbe3444fd8829c020841c3"
  ["$migration"]="40969eb59726a5fe7cb8f002972ee8f17fb9021eab2a4b6bde735ec192dc68da"
  ["$rollback"]="795b8e2e19e8ac8ce89586eff5293ac9d863e889cae3ce7154ce199e25bb1b76"
  ["$sql_test"]="1782df44cc614989ba9fad65c7fb3ee39550a0d1bd3b3f2be7e61a7dce023655"
  ["$worker"]="b63a63a5e352484b3ad010e1a849de2ba77043e249adf3cad5c5e4ba4dd09cb6"
)

backup=$(mktemp /tmp/memory-v1-v5-local-visibility.XXXXXX.dump)
table_list=$(mktemp /tmp/memory-v1-v5-local-visibility-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-local-visibility-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-v5-local-visibility-after.XXXXXX.tsv)
plan=$(mktemp /tmp/memory-v1-v5-local-visibility-plan.XXXXXX.json)
other_plan=$(mktemp /tmp/memory-v1-v5-local-visibility-other.XXXXXX.json)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$table_list" "$before" "$after" "$plan" "$other_plan"
}
trap cleanup EXIT
chmod 0600 "$backup" "$table_list" "$before" "$after" "$plan" "$other_plan"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory "$@"
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1" | tr -d '[:space:]'
}

query_rows() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

capture_state() {
  local output=$1 schema table state
  : >"$output"
  while IFS=$'\t' read -r schema table; do
    state=$(scalar "
      SELECT count(*)::text || E'\\t' || encode(public.digest(convert_to(
        coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
        'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM \"$schema\".\"$table\" AS value
      ) AS rows
    ")
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

for required in "${!expected_sha256[@]}"; do
  [[ -f "$repo_root/$required" ]]
  [[ "$(sha256sum "$repo_root/$required" | awk '{print $1}')" \
      == "${expected_sha256[$required]}" ]]
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
  'CREATE ROLE memory_extraction_retry_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_extraction_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_extraction_scheduler_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_inference_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_review_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_disposition_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"
printf '%s\n' \
  'GRANT USAGE ON SCHEMA memory TO brains_app;' \
  'GRANT EXECUTE ON FUNCTION memory.current_actor_user_id() TO brains_app;' \
  | run_sql
run_sql <"$repo_root/$foundation" >/dev/null

query_rows "
  SELECT table_schema || E'\\t' || table_name
  FROM information_schema.tables
  WHERE table_type='BASE TABLE' AND table_schema IN ('memory','public')
  ORDER BY table_schema,table_name
" >"$table_list"
capture_state "$before"
qdrant_before=$(qdrant_signature)

run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$migration"
run_sql \
  -v owner_user_id="$owner" \
  -v other_owner_user_id="$other" \
  -v staged_packet_id="$staged_packet" \
  <"$repo_root/$sql_test"

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$worker" \
  --owner-user-id "$owner" >"$plan"
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$worker" \
  --owner-user-id "$other" >"$other_plan"
PLAN="$plan" OTHER_PLAN="$other_plan" python3 - <<'PY'
import json
import os
from pathlib import Path

plan=json.loads(Path(os.environ['PLAN']).read_text())
other=json.loads(Path(os.environ['OTHER_PLAN']).read_text())
assert plan['apply'] is False and plan['database_writes']==0
assert plan['plans'][0]['route']=='no_work'
assert all(row['route']=='no_work' for row in other['plans'])
assert 'source_text' not in json.dumps([plan,other])
PY

capture_state "$after"
cmp -s "$before" "$after"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
run_sql <"$repo_root/$rollback"
[[ "$(scalar "
  SELECT cardinality(polroles)::text || '|' ||
    (SELECT rolname FROM pg_roles WHERE oid=polroles[1])
  FROM pg_policy WHERE polrelid='memory.relational_stage_batch'::regclass
    AND polname='owner_isolation'
")" == '1|memory_v5_writer' ]]
capture_state "$after"
cmp -s "$before" "$after"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
printf '%s\n' 'memory_v1_v5_local_packet_disposition_stage_visibility_clone: PASS'
