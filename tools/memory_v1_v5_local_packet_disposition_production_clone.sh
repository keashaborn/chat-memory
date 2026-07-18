#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores production into an isolated PostgreSQL clone,
# installs the append-only local-packet disposition path, and proves owner
# isolation, deterministic replay, unchanged authority rows, and unchanged
# Qdrant. No production database writes are made.

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_LOCAL_DISPOSITION_CLONE_PORT:-55463}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(docker compose -p memoryv1v5localdispositionclone \
  -f docker-compose.ci.yml -f docker-compose.stage-batch-clone.yml)
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"
migration=ops/sql/20260718_memory_v1_v5_local_packet_disposition.sql
rollback=ops/sql/20260718_memory_v1_v5_local_packet_disposition_rollback.sql
sql_test=tests/memory_v1_v5_local_packet_disposition.sql
worker=scripts/memory_v1_v5_local_packet_disposition.py
worker_test=scripts/memory_v1_v5_local_packet_disposition_test.py
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
packet=50c8fb51-f847-5423-b9b6-285c79703270

declare -A expected_sha256=(
  ["$migration"]="f09e7c2eb6aaa75ea085c352f480bee9daf1258b43bbe3444fd8829c020841c3"
  ["$rollback"]="a973a725264dd3b05e53259d3849207eef45bccf3c36eb7f400be9e9ba8c0ebf"
  ["$sql_test"]="78c3d4b88cc5e1dbfa07080112ce60228a14e2afa3dd5fdf6ff142571615b995"
  ["$worker"]="b63a63a5e352484b3ad010e1a849de2ba77043e249adf3cad5c5e4ba4dd09cb6"
  ["$worker_test"]="da89081ec37891a60d28bb653b4e81ed08c675dad9f36f6ebc97f622061d724d"
)

backup=$(mktemp /tmp/memory-v1-v5-local-disposition.XXXXXX.dump)
before=$(mktemp /tmp/memory-v1-v5-local-disposition-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-v5-local-disposition-after.XXXXXX.tsv)
after_rollback=$(mktemp /tmp/memory-v1-v5-local-disposition-rollback.XXXXXX.tsv)
plan=$(mktemp /tmp/memory-v1-v5-local-disposition-plan.XXXXXX.json)
other_plan=$(mktemp /tmp/memory-v1-v5-local-disposition-other.XXXXXX.json)
applied=$(mktemp /tmp/memory-v1-v5-local-disposition-applied.XXXXXX.json)
replayed=$(mktemp /tmp/memory-v1-v5-local-disposition-replayed.XXXXXX.json)
invalid=$(mktemp /tmp/memory-v1-v5-local-disposition-invalid.XXXXXX.json)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$before" "$after" "$after_rollback" "$plan" \
    "$other_plan" "$applied" "$replayed" "$invalid"
}
trap cleanup EXIT
chmod 0600 "$backup" "$before" "$after" "$after_rollback" "$plan" \
  "$other_plan" "$applied" "$replayed" "$invalid"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory "$@"
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1" | tr -d '[:space:]'
}

capture_authority_state() {
  local output=$1
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
  done < <(scalar "
    SELECT table_schema || E'\\t' || table_name
    FROM information_schema.tables
    WHERE table_type='BASE TABLE'
      AND table_schema IN ('memory','public')
      AND NOT (table_schema='memory'
        AND table_name='v5_local_packet_disposition')
    ORDER BY table_schema,table_name
  ")
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
  actual=$(sha256sum "$repo_root/$required" | awk '{print $1}')
  [[ "$actual" == "${expected_sha256[$required]}" ]]
done
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python \
  "$repo_root/$worker_test"
set +e
POSTGRES_DSN='postgresql://brains_app@10.0.0.1:5432/memory' \
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$repo_root/$worker" \
  --owner-user-id "$owner" >/dev/null 2>"$invalid"
invalid_status=$?
set -e
[[ "$invalid_status" -eq 1 ]]
INVALID="$invalid" python3 - <<'PY'
import json
import os
from pathlib import Path

invalid=json.loads(Path(os.environ['INVALID']).read_text())
assert invalid['outcome']=='disposition_error'
assert invalid['error_class']=='RuntimeError'
assert len(invalid['error_sha256'])==64
assert 'traceback' not in json.dumps(invalid).lower()
PY

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
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"
printf '%s\n' \
  'GRANT USAGE ON SCHEMA memory TO brains_app;' \
  'GRANT EXECUTE ON FUNCTION memory.current_actor_user_id() TO brains_app;' \
  | run_sql

capture_authority_state "$before"
qdrant_before=$(qdrant_signature)
run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$migration"
packet_storage_sha256=$(scalar "
  SELECT packet_storage_sha256
  FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
")
[[ "$packet_storage_sha256" =~ ^[0-9a-f]{64}$ ]]
run_sql \
  -v owner_user_id="$owner" \
  -v other_owner_user_id="$other" \
  -v packet_id="$packet" \
  -v packet_storage_sha256="$packet_storage_sha256" \
  <"$repo_root/$sql_test"
[[ "$(scalar 'SELECT count(*) FROM memory.v5_local_packet_disposition')" == 0 ]]

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$worker" \
  --owner-user-id "$owner" >"$plan"
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$worker" \
  --owner-user-id "$other" >"$other_plan"
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
MEMORY_V1_V5_LOCAL_PACKET_DISPOSITION_APPLY=memory_v1_v5_local_packet_disposition_apply_v1 \
  /opt/chat-memory/venv/bin/python "$repo_root/$worker" \
  --owner-user-id "$owner" --apply >"$applied"
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
MEMORY_V1_V5_LOCAL_PACKET_DISPOSITION_APPLY=memory_v1_v5_local_packet_disposition_apply_v1 \
  /opt/chat-memory/venv/bin/python "$repo_root/$worker" \
  --owner-user-id "$owner" --apply >"$replayed"

PLAN="$plan" OTHER_PLAN="$other_plan" APPLIED="$applied" \
REPLAYED="$replayed" PACKET="$packet" python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path

plan=json.loads(Path(os.environ['PLAN']).read_text())
other=json.loads(Path(os.environ['OTHER_PLAN']).read_text())
applied=json.loads(Path(os.environ['APPLIED']).read_text())
replayed=json.loads(Path(os.environ['REPLAYED']).read_text())
packet_hash=hashlib.sha256(os.environ['PACKET'].encode()).hexdigest()
assert plan['apply'] is False and plan['database_writes']==0
assert plan['plans'][0]['route']=='terminal_deferral'
assert plan['plans'][0]['packet_id_sha256']==packet_hash
assert all(row.get('packet_id_sha256')!=packet_hash for row in other['plans'])
assert applied['outcome']=='terminal_no_stage'
assert applied['write_counts']['dispositions']==1
assert applied['zero_write_replay_proved'] is True
assert replayed['outcome'] in {'no_work','manual_review_pending'}
assert replayed['write_counts']['dispositions']==0
assert 'source_text' not in json.dumps([plan,other,applied,replayed])
PY

[[ "$(scalar "
  SELECT count(*) FROM memory.v5_local_packet_disposition
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
")" == 1 ]]
[[ "$(scalar "
  SELECT count(*) FROM memory.v5_local_packet_disposition
  WHERE owner_user_id='$other'::uuid
")" == 0 ]]
capture_authority_state "$after"
cmp -s "$before" "$after"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

run_sql <"$repo_root/$rollback"
capture_authority_state "$after_rollback"
cmp -s "$before" "$after_rollback"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(scalar "SELECT to_regclass('memory.v5_local_packet_disposition') IS NULL")" == t ]]
printf '%s\n' 'memory_v1_v5_local_packet_disposition_production_clone: PASS'
