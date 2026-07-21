#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores production into an isolated clone and verifies
# scheduler selection, sanitization, expected rejection handling, and zero writes.

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_LOCAL_SCHEDULER_CLONE_PORT:-55462}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(docker compose -p memoryv1v5localschedulerclone \
  -f docker-compose.ci.yml -f docker-compose.stage-batch-clone.yml)
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"
worker=scripts/memory_v1_v5_local_inference_scheduler.py
worker_test=scripts/memory_v1_v5_local_inference_scheduler_test.py
fake_canary=tests/fixtures/memory_v1_v5_local_scheduler_fake_canary.py
service=ops/systemd/memory-v1-v5-local-inference-scheduler.service
timer=ops/systemd/memory-v1-v5-local-inference-scheduler.timer
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef

backup=$(mktemp /tmp/memory-v1-v5-local-scheduler.XXXXXX.dump)
before=$(mktemp /tmp/memory-v1-v5-local-scheduler-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-v5-local-scheduler-after.XXXXXX.tsv)
plan=$(mktemp /tmp/memory-v1-v5-local-scheduler-plan.XXXXXX.json)
other_plan=$(mktemp /tmp/memory-v1-v5-local-scheduler-other.XXXXXX.json)
accepted=$(mktemp /tmp/memory-v1-v5-local-scheduler-accepted.XXXXXX.json)
rejected=$(mktemp /tmp/memory-v1-v5-local-scheduler-rejected.XXXXXX.json)
invalid=$(mktemp /tmp/memory-v1-v5-local-scheduler-invalid.XXXXXX.json)
credential_dir=$(mktemp -d /tmp/memory-v1-v5-local-scheduler-credential.XXXXXX)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$before" "$after" "$plan" "$other_plan" \
    "$accepted" "$rejected" "$invalid"
  find "$credential_dir" -type f -delete
  rmdir "$credential_dir"
}
trap cleanup EXIT
chmod 0600 "$backup" "$before" "$after" "$plan" "$other_plan" \
  "$accepted" "$rejected" "$invalid"
printf '%s' 'synthetic-local-scheduler-key-0000000000000000' \
  >"$credential_dir/local_api_key"
chmod 0400 "$credential_dir/local_api_key"

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
    WHERE table_type='BASE TABLE' AND table_schema IN ('memory','public')
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

for required in "$worker" "$worker_test" "$fake_canary" "$service" "$timer"; do
  [[ -f "$repo_root/$required" ]]
done
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$repo_root/$worker_test"
systemd-analyze verify "$repo_root/$service" "$repo_root/$timer"

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
printf '%s\n' \
  'GRANT USAGE ON SCHEMA memory TO brains_app;' \
  'GRANT EXECUTE ON FUNCTION memory.current_actor_user_id() TO brains_app;' \
  'GRANT SELECT ON TABLE memory.evidence_extraction_job TO brains_app;' \
  | run_sql

capture_state "$before"
qdrant_before=$(qdrant_signature)

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python -c \
  'import asyncio; from scripts.memory_v1_v5_local_inference_scheduler import run; raise SystemExit(asyncio.run(run()))' \
  --owner-user-id "$owner" >"$plan"
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python -c \
  'import asyncio; from scripts.memory_v1_v5_local_inference_scheduler import run; raise SystemExit(asyncio.run(run()))' \
  --owner-user-id "$other" >"$other_plan"

for outcome in accepted rejected; do
  output=$accepted
  [[ "$outcome" == rejected ]] && output=$rejected
  POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  CREDENTIALS_DIRECTORY="$credential_dir" \
  MEMORY_V1_V5_LOCAL_SCHEDULER_APPLY=memory_v1_v5_local_inference_scheduler_apply_v1 \
  FAKE_LOCAL_CANARY_OUTCOME="$outcome" \
    /opt/chat-memory/venv/bin/python "$repo_root/$worker" \
    --owner-user-id "$owner" --canary "$repo_root/$fake_canary" --apply \
    >"$output"
done

set +e
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
CREDENTIALS_DIRECTORY="$credential_dir" \
MEMORY_V1_V5_LOCAL_SCHEDULER_APPLY=memory_v1_v5_local_inference_scheduler_apply_v1 \
FAKE_LOCAL_CANARY_OUTCOME=invalid \
  /opt/chat-memory/venv/bin/python "$repo_root/$worker" \
  --owner-user-id "$owner" --canary "$repo_root/$fake_canary" --apply \
  >/dev/null 2>"$invalid"
invalid_status=$?
set -e
[[ "$invalid_status" -eq 1 ]]

PLAN="$plan" OTHER_PLAN="$other_plan" ACCEPTED="$accepted" \
REJECTED="$rejected" INVALID="$invalid" python3 - <<'PY'
import json
import os
from pathlib import Path

plan=json.loads(Path(os.environ['PLAN']).read_text())
other=json.loads(Path(os.environ['OTHER_PLAN']).read_text())
accepted=json.loads(Path(os.environ['ACCEPTED']).read_text())
rejected=json.loads(Path(os.environ['REJECTED']).read_text())
invalid=json.loads(Path(os.environ['INVALID']).read_text())
assert plan['apply'] is False and plan['external_model_calls']==0
assert plan['plans'][0]['status_counts']['pending']>0
assert other['plans'][0]['status_counts']['pending']>0
assert plan['plans'][0]['owner_user_id_sha256']!=other['plans'][0]['owner_user_id_sha256']
assert accepted['outcome']=='max_jobs_reached'
assert accepted['processed']==1
assert accepted['outcome_counts']=={'accepted':1}
assert rejected['outcome']=='max_jobs_reached'
assert rejected['processed']==1
assert rejected['outcome_counts']=={'rejected':1}
assert rejected['rejection_code_counts']=={'local_validation_rejected':1}
assert 'source_text' not in json.dumps(accepted)
assert invalid['outcome']=='scheduler_error'
assert 'error_sha256' in invalid and 'traceback' not in json.dumps(invalid).lower()
PY

capture_state "$after"
cmp -s "$before" "$after"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
printf '%s\n' 'memory_v1_v5_local_inference_scheduler_production_clone: PASS'
