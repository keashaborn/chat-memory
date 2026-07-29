#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores a production snapshot into a disposable clone
# and proves that a full intake page may refill without being misclassified as
# a failed dispatch. Production and Qdrant remain read-only.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
port=${MEMORY_V1_INTAKE_DISPATCHER_CLONE_PORT:-55481}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1intakedispatcherclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
runner=scripts/memory_v1_evidence_intake_dispatcher.py
backup=$(mktemp /tmp/memory-v1-intake-dispatcher.XXXXXX.dump)
role_sql=$(mktemp /tmp/memory-v1-intake-dispatcher-roles.XXXXXX.sql)
work=$(mktemp -d /tmp/memory-v1-intake-dispatcher.XXXXXX)
chmod 0600 "$backup" "$role_sql"

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$role_sql"
  rm -rf "$work"
}
trap cleanup EXIT

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory "$@"
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1" | tr -d '[:space:]'
}

production_scalar() {
  docker exec brains-postgres-1 psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1" | tr -d '[:space:]'
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

other_owner_signature_sql="
  WITH rows(label,row_json) AS (
    SELECT 'job',to_jsonb(value)::text
    FROM memory.evidence_extraction_job AS value
    WHERE owner_user_id<>'$owner'::uuid
    UNION ALL
    SELECT 'terminal',to_jsonb(value)::text
    FROM memory.evidence_intake_terminal AS value
    WHERE owner_user_id<>'$owner'::uuid
    UNION ALL
    SELECT 'event',to_jsonb(value)::text
    FROM memory.evidence_extraction_event AS value
    WHERE owner_user_id<>'$owner'::uuid
  )
  SELECT encode(public.digest(convert_to(coalesce(string_agg(
    label||E'\\t'||row_json,E'\\n' ORDER BY label,row_json),''),
    'UTF8'),'sha256'),'hex')
  FROM rows
"

qdrant_before=$(qdrant_signature)
production_head_before=$(git -C /opt/chat-memory rev-parse HEAD)
production_jobs_before=$(production_scalar \
  'SELECT count(*) FROM memory.evidence_extraction_job')
production_terminals_before=$(production_scalar \
  'SELECT count(*) FROM memory.evidence_intake_terminal')

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc --no-owner >"$backup"
[[ -s "$backup" ]]
docker exec brains-postgres-1 psql -X -A -t -U sage -d memory -c "
  SELECT format(
    'CREATE ROLE %I %s %s NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;',
    rolname,
    CASE WHEN rolcanlogin THEN 'LOGIN' ELSE 'NOLOGIN' END,
    CASE WHEN rolinherit THEN 'INHERIT' ELSE 'NOINHERIT' END
  )
  FROM pg_roles
  WHERE rolname NOT LIKE 'pg\\_%' ESCAPE '\\'
    AND rolname NOT IN ('sage','postgres')
  ORDER BY rolname
" >"$role_sql"
[[ -s "$role_sql" ]]

"${compose[@]}" up -d --wait postgres
run_sql <"$role_sql"
printf '%s\n' \
  "ALTER ROLE brains_app PASSWORD 'clone_only_brains_password';" \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner <"$backup"

claims_before=$(scalar 'SELECT count(*) FROM memory.claim')
projections_before=$(scalar \
  'SELECT count(*) FROM memory.projection_apply_event')
other_before=$(scalar "$other_owner_signature_sql")

dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$runner" \
  --owner-user-id "$owner" \
  --selector-version 20260717_v2 \
  --limit 100 \
  --apply \
  --report-path "$work/report.json" \
  >"$work/output.json"

jq -e '
  .apply==true
  and .owners==1
  and .queued>=0
  and .terminal_recorded>=0
' "$work/output.json" >/dev/null
jq -e '
  .contract_version=="memory_v1_evidence_intake_dispatch_report_v1"
  and .apply==true
  and .model_calls==0
  and .candidate_writes==0
  and .claim_writes==0
  and (.owners|length)==1
  and .owners[0].before.rows==100
  and .owners[0].after.rows==100
  and (
    .owners[0].apply.queue_applied
    + .owners[0].apply.terminal_applied
  )>0
' "$work/report.json" >/dev/null

[[ "$(scalar 'SELECT count(*) FROM memory.claim')" == "$claims_before" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.projection_apply_event')" \
  == "$projections_before" ]]
[[ "$(scalar "$other_owner_signature_sql")" == "$other_before" ]]

qdrant_after=$(qdrant_signature)
production_head_after=$(git -C /opt/chat-memory rev-parse HEAD)
production_jobs_after=$(production_scalar \
  'SELECT count(*) FROM memory.evidence_extraction_job')
production_terminals_after=$(production_scalar \
  'SELECT count(*) FROM memory.evidence_intake_terminal')
[[ "$qdrant_after" == "$qdrant_before" ]]
[[ "$production_head_after" == "$production_head_before" ]]
[[ "$production_jobs_after" == "$production_jobs_before" ]]
[[ "$production_terminals_after" == "$production_terminals_before" ]]

printf '%s\n' \
  'EVIDENCE_INTAKE_DISPATCHER_CLONE=PASS' \
  "PRODUCTION_HEAD=$production_head_after" \
  "BEFORE_ROWS=$(jq -r '.owners[0].before.rows' "$work/report.json")" \
  "AFTER_ROWS=$(jq -r '.owners[0].after.rows' "$work/report.json")" \
  "QUEUE_APPLIED=$(jq -r '.owners[0].apply.queue_applied' "$work/report.json")" \
  "TERMINAL_APPLIED=$(jq -r '.owners[0].apply.terminal_applied' "$work/report.json")" \
  'MODEL_CALLS=0' \
  'CLAIM_WRITES=0' \
  'QDRANT_WRITES=0' \
  'CROSS_OWNER_ISOLATION=PASS'
