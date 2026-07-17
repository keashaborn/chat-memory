#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Requeues one hash-locked skipped extraction job without
# resetting attempts, calling an external model, or creating a packet.

repo_root=$(git rev-parse --show-toplevel)
manifest=${1:-ops/manifests/memory_v1_v5_reviewed_retry_apply_20260717.json}
case "$manifest" in
  ops/manifests/memory_v1_v5_reviewed_retry_apply_20260717.json)
    manifest_sha=b317e3c24793f2f0d9a053951c3408775a7e6ffc53279c3c956f684d682bcde2
    ;;
  ops/manifests/memory_v1_v5_reviewed_retry_second_apply_20260717.json)
    manifest_sha=7d7cefc63efe3ae47d8d7869792f33273485eab56de94f195aab8841590b0d4f
    ;;
  *)
    printf 'unapproved reviewed-retry manifest: %s\n' "$manifest" >&2
    exit 1
    ;;
esac
container=brains-postgres-1
snapshot_dir=/home/ubuntu/brains/snapshots
required_ancestor=$(jq -er '.authorization.required_commit' "$repo_root/$manifest")
owner=$(jq -er '.target.owner_user_id' "$repo_root/$manifest")
job_id=$(jq -er '.target.job_id' "$repo_root/$manifest")
content_sha=$(jq -er '.target.evidence_content_sha256' "$repo_root/$manifest")
binding_event=$(jq -er '.target.binding_event_id' "$repo_root/$manifest")
prior_failure=$(jq -er '.target.prior_failure_operation_id' "$repo_root/$manifest")
operation_id=$(jq -er '.operation.operation_id' "$repo_root/$manifest")
error_class=$(jq -er '.target.expected_error_class' "$repo_root/$manifest")
attempts=$(jq -er '.target.expected_attempts' "$repo_root/$manifest")
reason_code=$(jq -er '.target.reason_code' "$repo_root/$manifest")
units=(
  memory-v1-projection.timer
  memory-v1-governance.timer
  memory-v1-evidence-intake-dispatcher.timer
  memory-v1-v5-chat-capture.timer
  memory-v1-deferred-reconciliation-scan.timer
)

execution_id=$(cat /proc/sys/kernel/random/uuid)
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup="$snapshot_dir/memory_pre_v5_reviewed_retry_apply_${stamp}_${execution_id}.dump"
report="$snapshot_dir/memory_v1_v5_reviewed_retry_apply_${stamp}_${execution_id}.json"
lock=/run/lock/memory-v1-v5-reviewed-retry-apply.lock
tables=$(mktemp /tmp/memory-v1-v5-retry-apply-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-retry-apply-before.XXXXXX)
after=$(mktemp /tmp/memory-v1-v5-retry-apply-after.XXXXXX)
scope_before=$(mktemp /tmp/memory-v1-v5-retry-scope-before.XXXXXX)
scope_after=$(mktemp /tmp/memory-v1-v5-retry-scope-after.XXXXXX)
timer_state=$(mktemp /tmp/memory-v1-v5-retry-apply-timers.XXXXXX)
qdrant_before=$(mktemp /tmp/memory-v1-v5-retry-qdrant-before.XXXXXX)
qdrant_after=$(mktemp /tmp/memory-v1-v5-retry-qdrant-after.XXXXXX)
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
    "$timer_state" "$qdrant_before" "$qdrant_after"
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
git merge-base --is-ancestor "$required_ancestor" HEAD
[[ -z "$(git status --short)" ]]
[[ "$(jq -r '.authorization.apply_authorized' "$repo_root/$manifest")" == true ]]
[[ "$(jq -r '.target.owner_user_id' "$repo_root/$manifest")" == "$owner" ]]
[[ "$(jq -r '.target.job_id' "$repo_root/$manifest")" == "$job_id" ]]
[[ "$(jq -r '.operation.operation_id' "$repo_root/$manifest")" == "$operation_id" ]]

preflight=$(scalar "
  SELECT jsonb_build_object(
    'function_present',to_regprocedure(
      'memory.requeue_owner_skipped_evidence_job_v5(uuid,uuid,text,uuid,uuid,text,integer,text)'
    ) IS NOT NULL,
    'target_skipped',(SELECT count(*) FROM memory.evidence_extraction_job
      WHERE owner_user_id='$owner' AND job_id='$job_id'
        AND evidence_content_sha256='$content_sha'
        AND status='skipped' AND attempts=$attempts
        AND split_part(coalesce(last_error,''),':',1)='$error_class'),
    'prior_failure',(SELECT count(*) FROM memory.evidence_extraction_event
      WHERE owner_user_id='$owner' AND job_id='$job_id'
        AND operation_id='$prior_failure' AND event_type='skipped'
        AND details->>'error_class'='$error_class'
        AND (details->>'attempt')::integer=$attempts),
    'binding',(SELECT count(*) FROM memory.current_project_thread_binding_v5
      WHERE owner_user_id='$owner' AND binding_event_id='$binding_event'),
    'operation_absent',(SELECT count(*)=0 FROM memory.evidence_extraction_event
      WHERE owner_user_id='$owner' AND operation_id='$operation_id'),
    'packets',(SELECT count(*) FROM memory.evidence_extraction_packet_v5)
  )::text
")
[[ "$(jq -r '.function_present and .operation_absent' <<<"$preflight")" == true ]]
[[ "$(jq -r '.target_skipped' <<<"$preflight")" == 1 ]]
[[ "$(jq -r '.prior_failure' <<<"$preflight")" == 1 ]]
[[ "$(jq -r '.binding' <<<"$preflight")" == 1 ]]
[[ "$(jq -r '.packets' <<<"$preflight")" == 0 ]]

set -a
source /opt/chat-memory/.env
set +a
[[ -n "${POSTGRES_DSN:-}" ]]

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

apply_result=$(
  psql "$POSTGRES_DSN" -X -A -t -q -v ON_ERROR_STOP=1 <<SQL
BEGIN;
SELECT set_config('app.user_id','$owner',true);
SELECT status || E'\t' || attempts::text || E'\t' || apply_outcome
FROM memory.requeue_owner_skipped_evidence_job_v5(
  '$operation_id','$job_id','$content_sha','$binding_event',
  '$prior_failure','$error_class',$attempts,'$reason_code'
);
COMMIT;
SQL
)
[[ "$(tail -n 1 <<<"$apply_result")" == pending$'\t'"$attempts"$'\t'applied ]]

postflight=$(scalar "
  SELECT jsonb_build_object(
    'target_pending',(SELECT count(*) FROM memory.evidence_extraction_job
      WHERE owner_user_id='$owner' AND job_id='$job_id'
        AND status='pending' AND attempts=$attempts AND last_error IS NULL),
    'retry_events',(SELECT count(*) FROM memory.evidence_extraction_event
      WHERE owner_user_id='$owner' AND job_id='$job_id'
        AND operation_id='$operation_id' AND event_type='queued'
        AND from_status='skipped' AND to_status='pending'
        AND actor_type='admin' AND actor_ref='reviewed_retry'
        AND details->>'reason_code'='$reason_code'),
    'packets',(SELECT count(*) FROM memory.evidence_extraction_packet_v5)
  )::text
")
[[ "$(jq -r '.target_pending' <<<"$postflight")" == 1 ]]
[[ "$(jq -r '.retry_events' <<<"$postflight")" == 1 ]]
[[ "$(jq -r '.packets' <<<"$postflight")" == 0 ]]

replay=$(
  psql "$POSTGRES_DSN" -X -A -t -q -v ON_ERROR_STOP=1 <<SQL
BEGIN;
SELECT set_config('app.user_id','$owner',true);
SELECT status || E'\t' || attempts::text || E'\t' || apply_outcome
FROM memory.requeue_owner_skipped_evidence_job_v5(
  '$operation_id','$job_id','$content_sha','$binding_event',
  '$prior_failure','$error_class',$attempts,'$reason_code'
);
ROLLBACK;
SQL
)
[[ "$(tail -n 1 <<<"$replay")" == pending$'\t'"$attempts"$'\t'replayed ]]

capture_unchanged_tables "$after"
capture_isolation_scope "$scope_after"
capture_qdrant "$qdrant_after"
qdrant_after_sha=$(sha256sum "$qdrant_after" | cut -d' ' -f1)
cmp -s "$before" "$after"
cmp -s "$scope_before" "$scope_after"
[[ "$qdrant_before_sha" == "$qdrant_after_sha" ]]

restore_timers

jq -n \
  --arg execution_id "$execution_id" \
  --arg created_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg commit "$(git rev-parse HEAD)" \
  --arg manifest_sha256 "$manifest_sha" \
  --arg backup_path "$backup" \
  --arg backup_sha256 "$backup_sha" \
  --arg qdrant_sha256 "$qdrant_after_sha" \
  --argjson postflight "$postflight" \
  '{
    report_version:"memory_v1_v5_reviewed_retry_apply_v1",
    status:"passed",execution_id:$execution_id,created_at:$created_at,
    commit:$commit,manifest_sha256:$manifest_sha256,
    backup:{path:$backup_path,sha256:$backup_sha256},
    postflight:$postflight,
    proofs:{attempts_preserved:true,replay_zero_write:true,
      other_memory_tables_unchanged:true,non_target_rows_unchanged:true,
      account_isolation:true,qdrant_unchanged:true,
      qdrant_sha256:$qdrant_sha256,timers_restored:true,
      external_model_calls:0,packet_writes:0,candidate_writes:0,
      claim_writes:0,projection_writes:0,prompt_influence:false}
  }' >"$report"
chmod 0600 "$report"

printf 'memory_v1_v5_reviewed_retry_apply: PASS\n'
printf 'backup=%s\n' "$backup"
printf 'report=%s\n' "$report"
