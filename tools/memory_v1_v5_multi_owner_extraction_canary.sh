#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Processes exactly one pending extraction job for one of
# the two user-owned test accounts using private infrastructure. Deterministic
# deferrals may complete without invoking the GPU. It proves that every
# non-target row, all claims, Qdrant, prompts, and review files are unchanged.

if [[ "${MEMORY_V1_V5_MULTI_OWNER_CANARY:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_MULTI_OWNER_CANARY=authorized is required' >&2
  exit 1
fi
if [[ "$#" -ne 1 ]]; then
  echo 'usage: memory_v1_v5_multi_owner_extraction_canary.sh OWNER_UUID' >&2
  exit 1
fi

target=$1
case "$target" in
  557ea042-cb82-48f8-9429-472e96c957ef|d839b4bc-0bd2-4f2d-aafe-0f3f75883db8) ;;
  *) echo 'target is not an approved test owner' >&2; exit 1 ;;
esac

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
review_root=/home/ubuntu/memory-v1-reviews
worker=scripts/memory_v1_v5_local_inference_scheduler.py
canary=scripts/memory_v1_v5_local_inference_canary.py
provider=scripts/memory_v1_relational_extraction_v5_local_provider.py
service=ops/systemd/memory-v1-v5-local-inference-scheduler.service
lock_file=/home/ubuntu/brains/.memory_v1_v5_multi_owner_canary.lock

declare -A expected_sha256=(
  ["$worker"]="d1aff118fd26f76b09aac61c9a0375ce857777306326496f65b6db1878a93331"
  ["$canary"]="181a727058e937600d4382aa4af9ee9b96d441bffc9e3b3139f31d8709c5a835"
  ["$provider"]="2b6d05615943b8e7736c7a57450e9fd11b1858ebf20965f4cb599cf94b75e4a9"
  ["$service"]="7e2f3ef9749d2704a036ef4ab3f75dd449d5c74f78c8180b60ca4646094512ad"
)

phase=initialization
run_tag=
status_file=
timers_quiesced=0
timer_state=$(mktemp /tmp/memory-v1-v5-multi-owner-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-multi-owner-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-multi-owner-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-v5-multi-owner-after.XXXXXX.tsv)
raw_output=$(mktemp /tmp/memory-v1-v5-multi-owner-raw.XXXXXX)
json_output=$(mktemp /tmp/memory-v1-v5-multi-owner-output.XXXXXX.json)
chmod 0600 "$timer_state" "$table_list" "$before" "$after" \
  "$raw_output" "$json_output"

scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | tr -d '[:space:]'
}

restore_timers() {
  [[ "$timers_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
    if [[ "$enabled" == enabled ]]; then
      sudo -n systemctl enable "$unit" >/dev/null
    else
      [[ "$enabled" == disabled ]]
      sudo -n systemctl disable "$unit" >/dev/null
    fi
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      [[ "$active" == inactive ]]
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$timer_state"
  timers_quiesced=0
}

record_exit() {
  exit_code=$?
  restore_timers || exit_code=1
  if [[ "$exit_code" -ne 0 && -n "$status_file" && -s "$raw_output" ]]; then
    failure_output="${status_file%.status}.failure-output.json"
    if tr -d '\r' <"$raw_output" | grep '^{' | tail -n 1 >"$failure_output" \
        && jq -e 'type=="object"' "$failure_output" >/dev/null 2>&1; then
      chmod 0600 "$failure_output"
    else
      rm -f "$failure_output"
    fi
  fi
  rm -f "$timer_state" "$table_list" "$before" "$after" \
    "$raw_output" "$json_output"
  if [[ -n "$status_file" ]]; then
    {
      printf 'run_tag=%s\n' "$run_tag"
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$exit_code"
      printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$exit_code"
}
trap record_exit EXIT

capture_non_target() {
  local output=$1 schema table scoped state
  : >"$output"
  while IFS=$'\t' read -r schema table scoped; do
    if [[ "$scoped" == t ]]; then
      state=$(scalar "SELECT count(*)::text || ':' ||
        encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
          ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
        FROM (SELECT to_jsonb(value)::text AS row_json
          FROM \"$schema\".\"$table\" AS value
          WHERE owner_user_id::text IS DISTINCT FROM '$target') rows")
    else
      state=$(scalar "SELECT count(*)::text || ':' ||
        encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
          ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
        FROM (SELECT to_jsonb(value)::text AS row_json
          FROM \"$schema\".\"$table\" AS value) rows")
    fi
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

review_root_signature() {
  (cd "$review_root" && find . -type f -print0 | sort -z \
    | xargs -0r sha256sum) | sha256sum | awk '{print $1}'
}

for required in "${!expected_sha256[@]}"; do
  [[ -f "$required" ]]
  [[ "$(sha256sum "$required" | awk '{print $1}')" \
      == "${expected_sha256[$required]}" ]]
done
[[ -z "$(git status --porcelain)" ]]
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(systemctl is-active memory-v1-v5-local-inference-tunnel.service)" == active ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
target_sha=$(printf %s "$target" | sha256sum | awk '{print $1}')
status_file="$snapshot_dir/memory_v1_v5_multi_owner_canary_${run_tag}_${target_sha:0:12}.status"
report="$snapshot_dir/memory_v1_v5_multi_owner_canary_${run_tag}_${target_sha:0:12}.json"
preserved_output="$snapshot_dir/memory_v1_v5_multi_owner_canary_${run_tag}_${target_sha:0:12}.output.json"

phase=quiesce_timers
: >"$timer_state"
while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ "$(wc -l <"$timer_state")" -eq 13 ]]
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  sudo -n systemctl stop "$unit"
done <"$timer_state"
while IFS=$'\t' read -r unit _enabled _active; do
  existing_service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$existing_service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$existing_service"
done <"$timer_state"

phase=backup
backup_partial="$snapshot_dir/.memory_pre_v5_multi_owner_canary_${run_tag}_${target_sha:0:12}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_multi_owner_canary_${run_tag}_${target_sha:0:12}.dump"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup"
backup_sha=$(sha256sum "$backup" | awk '{print $1}')
printf '%s  %s\n' "$backup_sha" "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline
docker exec "$container" psql -X -A -t -U sage -d "$database" -c \
  "SELECT t.table_schema || E'\\t' || t.table_name || E'\\t' ||
     CASE WHEN EXISTS (SELECT 1 FROM information_schema.columns c
       WHERE c.table_schema=t.table_schema AND c.table_name=t.table_name
         AND c.column_name='owner_user_id') THEN 't' ELSE 'f' END
   FROM information_schema.tables t
   WHERE t.table_type='BASE TABLE' AND t.table_schema IN ('memory','public')
   ORDER BY t.table_schema,t.table_name" >"$table_list"
capture_non_target "$before"
qdrant_before=$(qdrant_signature)
review_before=$(review_root_signature)
claims_before=$(scalar 'SELECT count(*) FROM memory.claim')
pending_before=$(scalar "SELECT count(*) FROM memory.evidence_extraction_job WHERE owner_user_id='$target'::uuid AND status='pending'")
packets_before=$(scalar "SELECT count(*) FROM memory.evidence_extraction_packet_v5_local WHERE owner_user_id='$target'::uuid")
[[ "$pending_before" -gt 0 ]]

phase=private_extraction
sudo -n systemd-run --wait --pipe --collect \
  --unit="memory-v1-v5-multi-owner-canary-${target_sha:0:12}-$$" \
  -p User=ubuntu -p Group=ubuntu -p WorkingDirectory=/opt/chat-memory \
  -p EnvironmentFile=/opt/chat-memory/.env \
  -p Environment=PYTHONPATH=/opt/chat-memory \
  -p Environment=MEMORY_V1_V5_LOCAL_SCHEDULER_APPLY=memory_v1_v5_local_inference_scheduler_apply_v1 \
  -p LoadCredential=local_api_key:/etc/memory-v1-local-inference/api-key \
  -p NoNewPrivileges=true -p PrivateTmp=true -p PrivateDevices=true \
  -p ProtectSystem=strict -p ProtectHome=true \
  -p RestrictAddressFamilies='AF_UNIX AF_INET AF_INET6' \
  -p IPAddressDeny=any -p IPAddressAllow=localhost \
  -p CapabilityBoundingSet= -p LockPersonality=true \
  /opt/chat-memory/venv/bin/python \
  /opt/chat-memory/scripts/memory_v1_v5_local_inference_scheduler.py \
  --owner-user-id "$target" --max-jobs 1 --max-attempts 1 \
  --lease-seconds 900 --timeout-seconds 600 --max-output-tokens 4096 \
  --rolling-window-seconds 86400 --max-reserved-jobs 12 \
  --failure-threshold 3 --apply >"$raw_output"
tr -d '\r' <"$raw_output" | grep '^{' | tail -n 1 >"$json_output"
jq -e '
  .worker_version=="memory_v1_v5_local_inference_scheduler_v1" and
  .apply==true and .processed==1 and
  (.result.outcome=="accepted" or .result.outcome=="rejected") and
  (.result.local_model_calls==0 or .result.local_model_calls==1) and
  .result.external_model_calls==0 and
  .result.write_counts.claims==0 and .result.write_counts.qdrant==0 and
  .result.write_counts.prompt_influence==0 and
  .result.zero_write_replay_proved==true
' "$json_output" >/dev/null
cp "$json_output" "$preserved_output"
chmod 0600 "$preserved_output"

phase=postflight
pending_after=$(scalar "SELECT count(*) FROM memory.evidence_extraction_job WHERE owner_user_id='$target'::uuid AND status='pending'")
packets_after=$(scalar "SELECT count(*) FROM memory.evidence_extraction_packet_v5_local WHERE owner_user_id='$target'::uuid")
[[ "$pending_after" -eq $((pending_before-1)) ]]
if [[ "$(jq -r '.result.outcome' "$json_output")" == accepted ]]; then
  [[ "$packets_after" -eq $((packets_before+1)) ]]
else
  [[ "$packets_after" -eq "$packets_before" ]]
fi
[[ "$(scalar 'SELECT count(*) FROM memory.claim')" == "$claims_before" ]]
capture_non_target "$after"
cmp -s "$before" "$after"
[[ "$(review_root_signature)" == "$review_before" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=restore_timers
restore_timers

phase=report
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg target_owner_sha256 "$target_sha" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg output "$preserved_output" \
  --arg outcome "$(jq -r '.result.outcome' "$json_output")" \
  --arg rejection_code "$(jq -r '.result.rejection_code // ""' "$json_output")" \
  --argjson local_model_calls "$(jq -r '.result.local_model_calls' "$json_output")" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{contract_version:"memory_v1_v5_multi_owner_extraction_canary_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    target_owner_user_id_sha256:$target_owner_sha256,
    backup:{path:$backup,sha256:$backup_sha256},
    sanitized_output:$output,outcome:$outcome,
    local_model_calls:$local_model_calls,external_model_calls:0,
    rejection_code:(if $rejection_code=="" then null else $rejection_code end),
    checks:{fresh_backup:true,private_infrastructure_only:true,
      at_most_one_private_model_call:true,external_model_calls_zero:true,
      exactly_one_job:true,
      zero_write_replay:true,non_target_rows_unchanged:true,
      claims_unchanged:true,qdrant_unchanged:true,
      prompt_influence_zero:true,review_files_unchanged:true,
      existing_timers_restored:true},
    qdrant_sha256:$qdrant_sha256,
    hard_stop:"before_packet_routing_staging_claims_qdrant_or_prompt_influence"}' \
  >"$report"
chmod 0600 "$report"
jq -e '.checks|to_entries|map(.value==true)|all' "$report" >/dev/null
sha256sum "$report" >"$report.sha256"

phase=complete
printf '%s\n' 'memory_v1_v5_multi_owner_extraction_canary: PASS'
printf 'report=%s\nbackup=%s\noutcome=%s\n' \
  "$report" "$backup" "$(jq -r '.result.outcome' "$json_output")"
