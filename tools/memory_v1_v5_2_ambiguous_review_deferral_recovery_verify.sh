#!/usr/bin/env bash
set -euo pipefail

# Server: seebx backend. Recovers the audit after an intended disposition was
# committed but normal chat traffic changed unrelated append-only tables.

if [[ "${MEMORY_V1_V5_2_REVIEW_DEFERRAL_RECOVERY:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_REVIEW_DEFERRAL_RECOVERY=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source "${MEMORY_V1_ENV_FILE:-$repo_root/.env}"
set +a

owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
packet=7357397a-26a3-5d19-aee4-f7284509cf3f
evidence=2e0c5951-a38f-5ffd-89ac-1e9e4c4c6721
worker=scripts/memory_v1_v5_local_review_deferral.py
stage_guard_test=tests/memory_v1_v5_2_ambiguous_review_deferral_stage_guard.sql
decision=/home/ubuntu/memory-v1-reviews/v5-2-ambiguous-review-deferral-20260722T042642Z_915595619dc3.json
backup=/home/ubuntu/brains/snapshots/memory_pre_v5_2_review_deferral_20260722T042642Z_915595619dc3.dump
failed_status=/home/ubuntu/brains/snapshots/memory_v1_v5_2_review_deferral_20260722T042642Z_915595619dc3.status
snapshot_dir=/home/ubuntu/brains/snapshots
container=brains-postgres-1
database=memory
python_bin="${MEMORY_V1_PYTHON:-$repo_root/venv/bin/python}"
timer_state=$(mktemp /tmp/memory-v5-2-review-recovery-timers.XXXXXX)
replay=$(mktemp /tmp/memory-v5-2-review-recovery-replay.XXXXXX.json)
stage_log=$(mktemp /tmp/memory-v5-2-review-recovery-stage.XXXXXX.log)
chmod 0600 "$timer_state" "$replay" "$stage_log"
timers_quiesced=0

scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | tr -d '[:space:]'
}

run_sql() {
  docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
    -U sage -d "$database" "$@"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

restore_timers() {
  [[ "$timers_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    if [[ "$enabled" == enabled ]]; then
      sudo -n systemctl enable "$unit" >/dev/null
    else
      sudo -n systemctl disable "$unit" >/dev/null
    fi
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$timer_state"
  timers_quiesced=0
}

cleanup() {
  exit_code=$?
  restore_timers || exit_code=1
  rm -f "$timer_state" "$replay" "$stage_log"
  exit "$exit_code"
}
trap cleanup EXIT

[[ "$(sha256sum "$backup" | awk '{print $1}')" == \
  3305f073824fcfdb0c50ba40afa5152cb4e70f14dbb0da9dc97c27d2ef096738 ]]
[[ "$(sha256sum "$decision" | awk '{print $1}')" == \
  1ca8dc8f5c8fff4eb4945a3fdd49d029ad0b5a005401f14c69fa22e3be77af5c ]]
[[ "$(sha256sum "$failed_status" | awk '{print $1}')" == \
  ba022c5ea9cb88e2501174c64391d37d1f5d89faf37417cb75ca3f639a9c7b8a ]]
docker exec -i "$container" pg_restore -l <"$backup" >/dev/null
[[ "$(systemctl is-active brains.service)" == active ]]
[[ -n "${VS_SERVICE_TOKEN:-}" ]]

: >"$timer_state"
while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
timer_count=$(wc -l <"$timer_state")
(( timer_count >= 16 && timer_count <= 40 ))
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  sudo -n systemctl stop "$unit"
done <"$timer_state"
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
done <"$timer_state"

[[ "$(scalar "SELECT count(*)
  FROM memory.v5_local_packet_disposition
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
    AND evidence_id='$evidence'::uuid
    AND disposition='terminal_no_stage'
    AND review_decision='deferred'
    AND reason_code='ambiguous_transcription'
    AND NOT promotion_eligible
    AND review_basis_sha256 ~ '^[0-9a-f]{64}$'")" == 1 ]]
[[ "$(scalar "SELECT count(*) FROM memory.relational_stage_batch
  WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid")" == 0 ]]

qdrant_before=$(qdrant_signature)
MEMORY_V1_V5_2_REVIEW_DEFERRAL_APPLY=memory_v1_v5_2_local_review_deferral_apply_v1 \
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" "$python_bin" "$worker" \
  --owner-user-id "$owner" --packet-id "$packet" \
  --decision "$decision" --apply >"$replay"
jq -e '.apply==true and .outcome=="deferred" and
  .write_counts.dispositions==0 and .write_counts.staging==0 and
  .write_counts.claims==0 and .write_counts.qdrant==0 and
  .zero_write_replay_proved==true and .external_model_calls==0' \
  "$replay" >/dev/null
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]

target_qdrant_count=$(curl --fail --silent --show-error --max-time 30 \
  -H 'content-type: application/json' \
  -d '{"limit":10000,"with_payload":true,"with_vector":false}' \
  http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
  | jq --arg packet "$packet" --arg evidence "$evidence" \
    '[.result.points[] | select(
      (.payload|tostring|contains($packet)) or
      (.payload|tostring|contains($evidence))
    )] | length')
[[ "$target_qdrant_count" == 0 ]]

run_sql -v target_owner="$owner" -v evidence_id="$evidence" \
  <"$stage_guard_test" >"$stage_log"
grep -q 'memory_v1_v5_2_ambiguous_review_deferral_stage_guard: PASS' \
  "$stage_log"

restore_timers
curl --fail --silent --show-error --max-time 15 \
  -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
  http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null
curl --fail --silent --show-error --max-time 15 \
  -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
  http://127.0.0.1:8088/readyz \
  | jq -e '.ok==true and .postgres==true' >/dev/null

run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
report="$snapshot_dir/memory_v1_v5_2_review_deferral_recovery_${run_tag}.json"
replay_sha=$(sha256sum "$replay" | awk '{print $1}')
jq -n --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg backup "$backup" \
  --arg backup_sha256 3305f073824fcfdb0c50ba40afa5152cb4e70f14dbb0da9dc97c27d2ef096738 \
  --arg failed_status "$failed_status" --arg replay_sha256 "$replay_sha" \
  --arg qdrant_sha256 "$qdrant_after" --argjson timer_count "$timer_count" \
  '{contract_version:"memory_v1_v5_2_review_deferral_recovery_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    source_run:{status_path:$failed_status,stopped_phase:"postflight",
      intended_disposition_committed:true,
      stop_reason:"concurrent_normal_chat_append_only_activity"},
    backup:{path:$backup,sha256:$backup_sha256},
    observed_concurrent_activity:{chat_log_rows:2,attestations:1,evidence:1,
      intake_terminals:1,extraction_jobs:1,extraction_events:1,
      owners:1,source:"normal_authenticated_chat_request"},
    result:{target_dispositions:1,stage_rows:0,claims_written:0,
      target_qdrant_points:0,prompt_influence:0,external_model_calls:0},
    checks:{backup_valid:true,decision_hash_locked:true,
      append_only_target_disposition:true,zero_write_replay:true,
      stage_guard_enforced:true,target_qdrant_absent:true,
      qdrant_unchanged_during_recovery_replay:true,
      timers_restored_exactly:true,service_health:true},
    evidence:{replay_sha256:$replay_sha256,qdrant_sha256:$qdrant_sha256},
    metrics:{discovered_timer_count:$timer_count},
    hard_stop:"before_reextraction_staging_claims_retrieval_or_prompt_influence"}' \
  >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
jq -e '.checks | to_entries | map(.value==true) | all' "$report" >/dev/null
printf '%s\nreport=%s\n' \
  'memory_v1_v5_2_ambiguous_review_deferral_recovery_verify: PASS' "$report"
