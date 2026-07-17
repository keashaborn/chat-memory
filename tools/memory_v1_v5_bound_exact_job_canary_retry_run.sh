#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Executes one approved hash-locked retry canary and stops
# before packet review or downstream persistence.

repo_root=$(git rev-parse --show-toplevel)
manifest=${1:-ops/manifests/memory_v1_v5_bound_exact_job_canary_retry_authorized_20260717.json}
case "$manifest" in
  ops/manifests/memory_v1_v5_bound_exact_job_canary_retry_authorized_20260717.json)
    manifest_sha=70e938ca5d4191a90360567bd0245b5bab8f24fa66bacaa15f16f5e7f77b25c1
    ;;
  ops/manifests/memory_v1_v5_bound_exact_job_canary_third_authorized_20260717.json)
    manifest_sha=b6e4ea22be511049f7b734c2659a718f721e14a4ca5a56f6f5d1e2186f0a957d
    ;;
  *)
    printf 'unapproved exact-canary manifest: %s\n' "$manifest" >&2
    exit 1
    ;;
esac
container=brains-postgres-1
snapshot_dir=/home/ubuntu/brains/snapshots
python=/opt/chat-memory/venv/bin/python
runner=scripts/memory_v1_v5_bound_exact_job_canary.py
units=(
  memory-v1-projection.timer
  memory-v1-governance.timer
  memory-v1-evidence-intake-dispatcher.timer
  memory-v1-v5-chat-capture.timer
  memory-v1-deferred-reconciliation-scan.timer
)

jq_manifest() { jq -r "$1" "$repo_root/$manifest"; }
owner=$(jq_manifest '.target.owner_user_id')
job_id=$(jq_manifest '.target.job_id')
content_sha=$(jq_manifest '.target.evidence_content_sha256')
binding_event=$(jq_manifest '.target.binding_event_id')
retry_operation=$(jq_manifest '.target.reviewed_retry_operation_id // "f45ff77a-81b8-5d5a-9a40-b88399b3bbf6"')
project_key=$(jq_manifest '.target.project_key')
component_key=$(jq_manifest '.target.component_key')
expected_attempts=$(jq_manifest '.target.expected_attempts')
max_attempts=$(jq_manifest '.target.max_attempts')
run_id=$(jq_manifest '.execution.run_id')
claim_operation=$(jq_manifest '.execution.claim_operation_id')
packet_id=$(jq_manifest '.execution.packet_id')
persist_operation=$(jq_manifest '.execution.persist_operation_id')
failure_operation=$(jq_manifest '.execution.failure_operation_id')
worker_id=$(jq_manifest '.execution.worker_id')
model=$(jq_manifest '.execution.model')
required_commit=$(jq_manifest '.code.required_commit')

stamp=$(date -u +%Y%m%dT%H%M%SZ)
execution_id=$(cat /proc/sys/kernel/random/uuid)
backup="$snapshot_dir/memory_pre_v5_bound_exact_retry_canary_${stamp}_${execution_id}.dump"
report="$snapshot_dir/memory_v1_v5_bound_exact_retry_canary_${stamp}_${execution_id}.json"
lock=/run/lock/memory-v1-v5-bound-exact-retry-canary.lock
tables=$(mktemp /tmp/memory-v1-v5-retry-canary-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-retry-canary-before.XXXXXX)
after=$(mktemp /tmp/memory-v1-v5-retry-canary-after.XXXXXX)
scope_before=$(mktemp /tmp/memory-v1-v5-retry-canary-scope-before.XXXXXX)
scope_after=$(mktemp /tmp/memory-v1-v5-retry-canary-scope-after.XXXXXX)
timer_state=$(mktemp /tmp/memory-v1-v5-retry-canary-timers.XXXXXX)
qdrant_before=$(mktemp /tmp/memory-v1-v5-retry-canary-qdrant-before.XXXXXX)
qdrant_after=$(mktemp /tmp/memory-v1-v5-retry-canary-qdrant-after.XXXXXX)
runner_output_file=$(mktemp /tmp/memory-v1-v5-retry-canary-output.XXXXXX)
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

[[ -f "$repo_root/$manifest" ]]
[[ "$(sha256sum "$repo_root/$manifest" | cut -d' ' -f1)" == "$manifest_sha" ]]
while IFS=$'\t' read -r path expected_sha; do
  [[ -f "$repo_root/$path" ]]
  [[ "$(sha256sum "$repo_root/$path" | cut -d' ' -f1)" == "$expected_sha" ]]
done < <(jq -r '.code | to_entries[] | select(.value|type=="object") | [.value.path,.value.sha256] | @tsv' "$repo_root/$manifest")
git merge-base --is-ancestor "$required_commit" HEAD
[[ -z "$(git status --short)" ]]
[[ "$(jq_manifest '.authorization.external_call_authorized')" == true ]]
[[ "$(jq_manifest '.authorization.store')" == false ]]
[[ "$(jq_manifest '.authorization.maximum_external_calls')" == 1 ]]
[[ "$(jq_manifest '.authorization.maximum_http_retries')" == 0 ]]
[[ "$expected_attempts:$max_attempts" == 1:2 \
  || "$expected_attempts:$max_attempts" == 2:3 \
  || "$expected_attempts:$max_attempts" == 3:4 ]]

preflight=$(scalar "
  SELECT jsonb_build_object(
    'target_pending',(SELECT count(*) FROM memory.evidence_extraction_job
      WHERE owner_user_id='$owner' AND job_id='$job_id'
        AND route='relational_extraction'
        AND evidence_content_sha256='$content_sha'
        AND status='pending' AND attempts=$expected_attempts),
    'binding',(SELECT count(*) FROM memory.current_project_thread_binding_v5
      WHERE owner_user_id='$owner' AND binding_event_id='$binding_event'
        AND project_key='$project_key'),
    'reviewed_retry_event',(SELECT count(*) FROM memory.evidence_extraction_event
      WHERE owner_user_id='$owner' AND job_id='$job_id'
        AND operation_id='$retry_operation'
        AND event_type='queued' AND actor_type='admin'
        AND actor_ref='reviewed_retry'),
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
[[ "$(jq -r '.reviewed_retry_event' <<<"$preflight")" == 1 ]]
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
    --owner-user-id "$owner" --job-id "$job_id" \
    --expected-content-sha256 "$content_sha" \
    --binding-event-id "$binding_event" \
    --expected-project-key "$project_key" \
    --expected-component-key "$component_key" \
    --expected-attempts "$expected_attempts" --max-attempts "$max_attempts" \
    --run-id "$run_id" --worker-id "$worker_id"
)
[[ "$(jq -r '.apply' <<<"$plan")" == false ]]
[[ "$(jq -r '.external_model_calls' <<<"$plan")" == 0 ]]
[[ "$(jq -r '.exact_target.status' <<<"$plan")" == pending ]]
[[ "$(jq -r '.exact_target.attempts' <<<"$plan")" == "$expected_attempts" ]]

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
      'evidence_extraction_job','evidence_extraction_event',
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
    --owner-user-id "$owner" --job-id "$job_id" \
    --expected-content-sha256 "$content_sha" \
    --binding-event-id "$binding_event" \
    --expected-project-key "$project_key" \
    --expected-component-key "$component_key" \
    --expected-attempts "$expected_attempts" --max-attempts "$max_attempts" \
    --run-id "$run_id" --worker-id "$worker_id" --model "$model" \
    --lease-seconds 300 --timeout-seconds 120 --max-output-tokens 16000 \
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
[[ "$(jq -r '.attempts' <<<"$postflight")" == "$max_attempts" ]]
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
  jq -e '.result.rejection_code | type=="string" and length>0' <<<"$runner_result" >/dev/null
  jq -e '.result.sanitized_diagnostic.rejection.code | type=="string" and length>0' \
    <<<"$runner_result" >/dev/null
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
  'relational_extraction','$worker_id',300,$max_attempts
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
  --arg report_version memory_v1_v5_bound_exact_retry_canary_v1 \
  --arg execution_id "$execution_id" \
  --arg created_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg commit "$(git rev-parse HEAD)" \
  --arg manifest_sha256 "$manifest_sha" \
  --arg runner_sha256 "$(jq_manifest '.code.runner.sha256')" \
  --arg backup_path "$backup" --arg backup_sha256 "$backup_sha" \
  --arg qdrant_sha256 "$qdrant_after_sha" --arg terminal_status "$status" \
  --argjson runner_result "$runner_result" --argjson postflight "$postflight" \
  '{
    report_version:$report_version,status:"passed",
    execution_id:$execution_id,created_at:$created_at,commit:$commit,
    manifest_sha256:$manifest_sha256,runner_sha256:$runner_sha256,
    backup:{path:$backup_path,sha256:$backup_sha256},
    terminal_status:$terminal_status,runner_result:$runner_result,
    postflight:$postflight,
    proofs:{store:false,maximum_external_calls:1,http_retries:0,
      exact_job_claim:true,reviewed_retry:true,reviewed_binding_pinned:true,
      replay_zero_write:true,other_memory_tables_unchanged:true,
      non_target_queue_rows_unchanged:true,account_isolation:true,
      qdrant_unchanged:true,qdrant_sha256:$qdrant_sha256,
      timers_restored:true,candidates_written:0,claims_written:0,
      staging_written:0,projection_written:0,prompt_influence:false}
  }' >"$report"
chmod 0600 "$report"

printf 'memory_v1_v5_bound_exact_retry_canary: PASS\n'
printf 'terminal_status=%s\n' "$status"
printf 'external_model_calls=%s\n' "$calls"
printf 'backup=%s\n' "$backup"
printf 'report=%s\n' "$report"
