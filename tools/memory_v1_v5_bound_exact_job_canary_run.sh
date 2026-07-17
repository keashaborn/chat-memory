#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Executes one hash-locked, owner-scoped, store=false
# extraction canary and stops before packet review or downstream persistence.

repo_root=$(git rev-parse --show-toplevel)
manifest=ops/manifests/memory_v1_v5_bound_exact_job_canary_authorized_20260717.json
manifest_sha=6f0162b8e9506dabe3cd633383b140b5e35c23bb2ff34639826bdd4e5ed0fc57
runner=scripts/memory_v1_v5_bound_exact_job_canary.py
runner_sha=2c5f3c5fb8bc77d18c3b5f15c2dc0827025523672f23cf347973efd62935d909
registry=specs/memory_v1_predicate_registry_v5.json
registry_sha=4837cc66f8ef41d5b091528c02e06add267586cb170dc0eb4b57fc207bd0f3d8
schema=specs/memory_v1_relational_extraction_v5.schema.json
schema_sha=744ce1d466dfe502fb78fd0ba0a996dd723d1593f34bc456c849c20811f99e70
required_commit=996a42f01468cfddf818a9f30afed116c47a29cf
container=brains-postgres-1
snapshot_dir=/home/ubuntu/brains/snapshots
python=/opt/chat-memory/venv/bin/python
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
job_id=788d0258-7227-46e2-8382-d13e6a122722
content_sha=ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778
binding_event=e8738381-c3be-4395-bfb2-75bfd42949e9
project_key=verbal-sage
component_key=memory-v1
run_id=a25e8a71-f834-4f9a-b9b2-a1a2f86c5fb8
claim_operation=06a60509-abda-54a9-a761-f0072de23e94
packet_id=863f41d1-752d-5d04-b4e8-5b7e3919f17e
persist_operation=ceba62d9-575f-597d-939b-926db940b4da
failure_operation=a95e5e34-edb8-5296-be1b-ebe9e348aee0
worker_id=memory-v1-v5-bound-exact-canary-20260717
model=gpt-5.6-terra
units=(
  memory-v1-projection.timer
  memory-v1-governance.timer
  memory-v1-evidence-intake-dispatcher.timer
  memory-v1-v5-chat-capture.timer
  memory-v1-deferred-reconciliation-scan.timer
)

stamp=$(date -u +%Y%m%dT%H%M%SZ)
execution_id=$(cat /proc/sys/kernel/random/uuid)
backup="$snapshot_dir/memory_pre_v5_bound_exact_canary_${stamp}_${execution_id}.dump"
report="$snapshot_dir/memory_v1_v5_bound_exact_canary_${stamp}_${execution_id}.json"
lock=/run/lock/memory-v1-v5-bound-exact-canary.lock
tables=$(mktemp /tmp/memory-v1-v5-canary-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-canary-before.XXXXXX)
after=$(mktemp /tmp/memory-v1-v5-canary-after.XXXXXX)
scope_before=$(mktemp /tmp/memory-v1-v5-canary-scope-before.XXXXXX)
scope_after=$(mktemp /tmp/memory-v1-v5-canary-scope-after.XXXXXX)
timer_state=$(mktemp /tmp/memory-v1-v5-canary-timers.XXXXXX)
qdrant_before=$(mktemp /tmp/memory-v1-v5-canary-qdrant-before.XXXXXX)
qdrant_after=$(mktemp /tmp/memory-v1-v5-canary-qdrant-after.XXXXXX)
runner_output_file=$(mktemp /tmp/memory-v1-v5-canary-output.XXXXXX)
quiesced=false

scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

restore_timers() {
  [[ "$quiesced" == true ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$timer_state"
  quiesced=false
}

cleanup() {
  status=$?
  restore_timers || status=1
  rm -f "$tables" "$before" "$after" "$scope_before" "$scope_after" \
    "$timer_state" "$qdrant_before" "$qdrant_after" "$runner_output_file"
  exit "$status"
}
trap cleanup EXIT

capture_unchanged_tables() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    state=$(scalar "
      SELECT count(*)::text || E'\\t' || encode(
        public.digest(
          convert_to(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'UTF8'),
          'sha256'
        ),
        'hex'
      )
      FROM (
        SELECT to_jsonb(table_row)::text AS row_json
        FROM memory.\"$table\" AS table_row
      ) AS rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$tables"
}

capture_isolation_scope() {
  local output=$1
  scalar "
    WITH scoped AS (
      SELECT 'jobs_except_target' AS scope,to_jsonb(job)::text AS row_json
      FROM memory.evidence_extraction_job AS job
      WHERE NOT (job.owner_user_id='$owner' AND job.job_id='$job_id')
      UNION ALL
      SELECT 'events_except_target',to_jsonb(event)::text
      FROM memory.evidence_extraction_event AS event
      WHERE NOT (event.owner_user_id='$owner' AND event.job_id='$job_id')
      UNION ALL
      SELECT 'packets_except_target',to_jsonb(packet)::text
      FROM memory.evidence_extraction_packet_v5 AS packet
      WHERE NOT (packet.owner_user_id='$owner' AND packet.job_id='$job_id')
    )
    SELECT scope || E'\\t' || count(*)::text || E'\\t' || encode(
      public.digest(
        convert_to(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'UTF8'),
        'sha256'
      ),
      'hex'
    )
    FROM scoped
    GROUP BY scope
    ORDER BY scope
  " >"$output"
}

capture_qdrant() {
  local output=$1
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -X POST http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    -d '{"limit":10000,"with_payload":true,"with_vector":false}' \
    | jq -e -S -c \
      '{points:(.result.points|sort_by(.id|tostring)),next_page_offset:.result.next_page_offset}' \
    >"$output"
}

exec 9>"$lock"
flock -n 9

for required in "$manifest" "$runner" "$registry" "$schema"; do
  [[ -f "$repo_root/$required" ]]
done
[[ "$(sha256sum "$repo_root/$manifest" | cut -d' ' -f1)" == "$manifest_sha" ]]
[[ "$(sha256sum "$repo_root/$runner" | cut -d' ' -f1)" == "$runner_sha" ]]
[[ "$(sha256sum "$repo_root/$registry" | cut -d' ' -f1)" == "$registry_sha" ]]
[[ "$(sha256sum "$repo_root/$schema" | cut -d' ' -f1)" == "$schema_sha" ]]
git merge-base --is-ancestor "$required_commit" HEAD
[[ -z "$(git status --short)" ]]
[[ "$(jq -r '.authorization.external_call_authorized' "$repo_root/$manifest")" == true ]]
[[ "$(jq -r '.authorization.store' "$repo_root/$manifest")" == false ]]
[[ "$(jq -r '.authorization.maximum_external_calls' "$repo_root/$manifest")" == 1 ]]
[[ "$(jq -r '.authorization.maximum_http_retries' "$repo_root/$manifest")" == 0 ]]
[[ "$(jq -r '.execution.model' "$repo_root/$manifest")" == "$model" ]]
[[ "$(jq -r '.target.owner_user_id' "$repo_root/$manifest")" == "$owner" ]]
[[ "$(jq -r '.target.job_id' "$repo_root/$manifest")" == "$job_id" ]]

preflight=$(scalar "
  SELECT jsonb_build_object(
    'target_pending',(SELECT count(*) FROM memory.evidence_extraction_job
      WHERE owner_user_id='$owner' AND job_id='$job_id'
        AND route='relational_extraction'
        AND evidence_content_sha256='$content_sha'
        AND status='pending' AND attempts=0),
    'binding',(SELECT count(*) FROM memory.current_project_thread_binding_v5
      WHERE owner_user_id='$owner' AND binding_event_id='$binding_event'
        AND project_key='$project_key'),
    'reserved_operations',(SELECT count(*) FROM memory.evidence_extraction_event
      WHERE owner_user_id='$owner'
        AND operation_id IN ('$claim_operation','$persist_operation','$failure_operation')),
    'reserved_packet',(SELECT count(*) FROM memory.evidence_extraction_packet_v5
      WHERE owner_user_id='$owner' AND packet_id='$packet_id'),
    'all_packets',(SELECT count(*) FROM memory.evidence_extraction_packet_v5)
  )::text
")
[[ "$(jq -r '.target_pending' <<<"$preflight")" == 1 ]]
[[ "$(jq -r '.binding' <<<"$preflight")" == 1 ]]
[[ "$(jq -r '.reserved_operations + .reserved_packet + .all_packets' <<<"$preflight")" == 0 ]]

set -a
source /opt/chat-memory/.env
set +a
[[ -n "${POSTGRES_DSN:-}" ]]
[[ -n "${OPENAI_API_KEY:-}" ]]
[[ -x "$python" ]]

plan=$(
  cd "$repo_root"
  PYTHONPATH="$repo_root" "$python" "$runner" \
    --owner-user-id "$owner" \
    --job-id "$job_id" \
    --expected-content-sha256 "$content_sha" \
    --binding-event-id "$binding_event" \
    --expected-project-key "$project_key" \
    --expected-component-key "$component_key" \
    --run-id "$run_id" \
    --worker-id "$worker_id"
)
[[ "$(jq -r '.apply' <<<"$plan")" == false ]]
[[ "$(jq -r '.external_model_calls' <<<"$plan")" == 0 ]]
[[ "$(jq -r '.exact_target.status' <<<"$plan")" == pending ]]
[[ "$(jq -r '.exact_target.attempts' <<<"$plan")" == 0 ]]

: >"$timer_state"
for unit in "${units[@]}"; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done
quiesced=true
for unit in "${units[@]}"; do
  sudo -n systemctl stop "$unit"
  [[ "$(systemctl is-active "$unit")" == inactive ]]
done

umask 077
docker exec "$container" pg_dump -U sage -d memory \
  -Fc --no-owner --no-privileges >"$backup"
chmod 0600 "$backup"
[[ -s "$backup" ]]
docker exec -i "$container" pg_restore -l <"$backup" >/dev/null
backup_sha=$(sha256sum "$backup" | cut -d' ' -f1)

scalar "
  SELECT table_name
  FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
    AND table_name NOT IN (
      'evidence_extraction_job',
      'evidence_extraction_event',
      'evidence_extraction_packet_v5'
    )
  ORDER BY table_name
" >"$tables"
capture_unchanged_tables "$before"
capture_isolation_scope "$scope_before"
capture_qdrant "$qdrant_before"
qdrant_before_sha=$(sha256sum "$qdrant_before" | cut -d' ' -f1)

(
  cd "$repo_root"
  MEMORY_V1_V5_EXACT_JOB_CANARY_APPLY=memory_v1_v5_bound_exact_job_canary_apply_v1 \
  MEMORY_V1_V5_EXTERNAL_CALLS=memory_v1_openai_v5_external_calls_v1 \
  PYTHONPATH="$repo_root" "$python" "$runner" \
    --owner-user-id "$owner" \
    --job-id "$job_id" \
    --expected-content-sha256 "$content_sha" \
    --binding-event-id "$binding_event" \
    --expected-project-key "$project_key" \
    --expected-component-key "$component_key" \
    --run-id "$run_id" \
    --worker-id "$worker_id" \
    --model "$model" \
    --lease-seconds 300 \
    --timeout-seconds 120 \
    --max-output-tokens 16000 \
    --apply --enable-external-call
) >"$runner_output_file"
runner_result=$(jq -e -c . "$runner_output_file")
[[ "$(jq -r '.apply' <<<"$runner_result")" == true ]]
[[ "$(jq -r '.processed' <<<"$runner_result")" == 1 ]]
calls=$(jq -r '.external_model_calls' <<<"$runner_result")
[[ "$calls" =~ ^[01]$ ]]

postflight=$(scalar "
  SELECT jsonb_build_object(
    'status',(SELECT status::text FROM memory.evidence_extraction_job
      WHERE owner_user_id='$owner' AND job_id='$job_id'),
    'attempts',(SELECT attempts FROM memory.evidence_extraction_job
      WHERE owner_user_id='$owner' AND job_id='$job_id'),
    'claim_events',(SELECT count(*) FROM memory.evidence_extraction_event
      WHERE owner_user_id='$owner' AND operation_id='$claim_operation'
        AND event_type='claimed'),
    'review_events',(SELECT count(*) FROM memory.evidence_extraction_event
      WHERE owner_user_id='$owner' AND operation_id='$persist_operation'
        AND event_type='review_required'),
    'skipped_events',(SELECT count(*) FROM memory.evidence_extraction_event
      WHERE owner_user_id='$owner' AND operation_id='$failure_operation'
        AND event_type='skipped'),
    'packets',(SELECT count(*) FROM memory.evidence_extraction_packet_v5
      WHERE owner_user_id='$owner' AND packet_id='$packet_id'
        AND job_id='$job_id' AND evidence_content_sha256='$content_sha'
        AND project_binding_event_id='$binding_event'),
    'packet_external_calls',coalesce((SELECT external_model_calls
      FROM memory.evidence_extraction_packet_v5
      WHERE owner_user_id='$owner' AND packet_id='$packet_id'),0),
    'all_packets',(SELECT count(*) FROM memory.evidence_extraction_packet_v5)
  )::text
")
[[ "$(jq -r '.attempts' <<<"$postflight")" == 1 ]]
[[ "$(jq -r '.claim_events' <<<"$postflight")" == 1 ]]

status=$(jq -r '.status' <<<"$postflight")
if [[ "$status" == review_required ]]; then
  [[ "$calls" == 1 ]]
  [[ "$(jq -r '.review_events' <<<"$postflight")" == 1 ]]
  [[ "$(jq -r '.skipped_events' <<<"$postflight")" == 0 ]]
  [[ "$(jq -r '.packets' <<<"$postflight")" == 1 ]]
  [[ "$(jq -r '.packet_external_calls' <<<"$postflight")" == 1 ]]
  [[ "$(jq -r '.all_packets' <<<"$postflight")" == 1 ]]
elif [[ "$status" == skipped ]]; then
  [[ "$(jq -r '.review_events' <<<"$postflight")" == 0 ]]
  [[ "$(jq -r '.skipped_events' <<<"$postflight")" == 1 ]]
  [[ "$(jq -r '.packets + .all_packets' <<<"$postflight")" == 0 ]]
  [[ -n "$(jq -r '.result.rejection_code // empty' <<<"$runner_result")" ]]
else
  printf 'unexpected canary terminal status: %s\n' "$status" >&2
  exit 1
fi

replay=$(
  psql "$POSTGRES_DSN" -X -A -t -q -v ON_ERROR_STOP=1 <<SQL
BEGIN;
SELECT set_config('app.user_id','$owner',true);
SELECT status || E'\t' || apply_outcome
FROM memory.claim_owner_bound_evidence_job_v5(
  '$claim_operation','$job_id','$content_sha','$binding_event',
  'relational_extraction','$worker_id',300,1
);
ROLLBACK;
SQL
)
[[ "$(tail -n 1 <<<"$replay")" == "$status"$'\t'"replayed" ]]

capture_unchanged_tables "$after"
capture_isolation_scope "$scope_after"
capture_qdrant "$qdrant_after"
qdrant_after_sha=$(sha256sum "$qdrant_after" | cut -d' ' -f1)
cmp -s "$before" "$after"
cmp -s "$scope_before" "$scope_after"
[[ "$qdrant_before_sha" == "$qdrant_after_sha" ]]

restore_timers

jq -n \
  --arg report_version memory_v1_v5_bound_exact_job_canary_v1 \
  --arg execution_id "$execution_id" \
  --arg created_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg commit "$(git rev-parse HEAD)" \
  --arg manifest_sha256 "$manifest_sha" \
  --arg runner_sha256 "$runner_sha" \
  --arg backup_path "$backup" \
  --arg backup_sha256 "$backup_sha" \
  --arg qdrant_sha256 "$qdrant_after_sha" \
  --arg terminal_status "$status" \
  --argjson runner_result "$runner_result" \
  --argjson postflight "$postflight" \
  '{
    report_version:$report_version,status:"passed",
    execution_id:$execution_id,created_at:$created_at,commit:$commit,
    manifest_sha256:$manifest_sha256,runner_sha256:$runner_sha256,
    backup:{path:$backup_path,sha256:$backup_sha256},
    terminal_status:$terminal_status,runner_result:$runner_result,
    postflight:$postflight,
    proofs:{store:false,maximum_external_calls:1,http_retries:0,
      exact_job_claim:true,reviewed_binding_pinned:true,
      replay_zero_write:true,other_memory_tables_unchanged:true,
      non_target_queue_rows_unchanged:true,account_isolation:true,
      qdrant_unchanged:true,qdrant_sha256:$qdrant_sha256,
      timers_restored:true,candidates_written:0,claims_written:0,
      staging_written:0,projection_written:0,prompt_influence:false}
  }' >"$report"
chmod 0600 "$report"

printf 'memory_v1_v5_bound_exact_job_canary: PASS\n'
printf 'terminal_status=%s\n' "$status"
printf 'external_model_calls=%s\n' "$calls"
printf 'backup=%s\n' "$backup"
printf 'report=%s\n' "$report"
