#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Deploys the reviewed generic create/reinforce staging
# boundary. It writes projection review plans only, never governed claims.

if [[ "$EUID" -ne 0 ]]; then
  echo 'run through sudo' >&2
  exit 1
fi
if [[ "${MEMORY_V1_V5_2_CLAIM_TARGET_STAGE_PRODUCTION:-}" != authorized ]]; then
  echo 'production authorization environment is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source .env
set +a

required_base=84303eda237fb1bc6b85c3a9aa395d26703e2d26
required_head=${MEMORY_V1_V5_2_CLAIM_TARGET_STAGE_EXPECTED_HEAD:-}
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
container=brains-postgres-1
database=memory
python_bin=/opt/chat-memory/venv/bin/python
review_dir=/home/ubuntu/memory-v1-reviews
snapshot_root=/home/ubuntu/brains/snapshots
clone_test=tools/memory_v1_v5_2_claim_target_stage_clone.sh
stage_script=scripts/memory_v1_v5_2_claim_target_stage.py
timer_state=$(mktemp /tmp/memory-v5-2-claim-target-stage-timers.XXXXXX)
timers_quiesced=0
run_id=$(date -u +%Y%m%dT%H%M%SZ)-$$
status="$snapshot_root/memory_v1_v5_2_claim_target_stage_$run_id.status"
manifest="$review_dir/claim-target-stage-production-manifest-$run_id.json"
authorization="$review_dir/claim-target-stage-production-authorization-$run_id.json"
cross_owner="$review_dir/claim-target-stage-production-cross-owner-$run_id.json"
apply_result="$review_dir/claim-target-stage-production-apply-$run_id.json"
replay_result="$review_dir/claim-target-stage-production-replay-$run_id.json"

restore_timers() {
  [[ "$timers_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    case "$enabled" in
      enabled) systemctl enable "$unit" >/dev/null ;;
      disabled) systemctl disable "$unit" >/dev/null ;;
      *) return 1 ;;
    esac
    case "$active" in
      active) systemctl start "$unit" ;;
      inactive) systemctl stop "$unit" ;;
      *) return 1 ;;
    esac
    [[ "$(systemctl is-enabled "$unit" 2>/dev/null || true)" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit" 2>/dev/null || true)" == "$active" ]]
  done <"$timer_state"
  timers_quiesced=0
}

cleanup() {
  code=$?
  trap - EXIT
  restore_timers || code=1
  rm -f "$timer_state"
  {
    printf 'exit_code=%s\n' "$code"
    printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"$status"
  chmod 0600 "$status"
  exit "$code"
}
trap cleanup EXIT

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | cut -d' ' -f1
}

tables=(
  entity observation observation_entity_binding observation_entailment_v5
  claim claim_revision claim_observation
  projection_plan projection_plan_item projection_claim_payload
  projection_plan_observation projection_review projection_apply_event
  projection_outbox
)
snapshot_counts() {
  for table in "${tables[@]}"; do
    docker exec "$container" psql -X -A -t -U sage -d "$database" \
      -v ON_ERROR_STOP=1 -c \
      "SELECT '$table='||count(*) FROM memory.$table;"
  done
}

count_from() {
  local snapshot=$1 table=$2
  printf '%s\n' "$snapshot" | sed -n "s/^$table=//p"
}

[[ -n "$required_head" ]]
[[ "$(git rev-parse HEAD)" == "$required_head" ]]
[[ "$(git merge-base "$required_base" HEAD)" == "$required_base" ]]
[[ -z "$(git status --porcelain)" ]]
bash -n "$clone_test"
PYTHONPATH="$repo_root:$repo_root/scripts" \
  "$python_bin" -m py_compile "$stage_script"

clone_output=$("$clone_test")
printf '%s\n' "$clone_output"
grep -qx 'CLAIM_TARGET_STAGE_CLONE=PASS' <<<"$clone_output"
review=$(sed -n 's/^REVIEW=//p' <<<"$clone_output")
[[ -f "$review" && "$(stat -c '%a' "$review")" == 600 ]]
[[ "$(jq -er '.report_sha256' "$review")" \
  == c701d774ed68f03f7e58fddef5378cc68391fef2d788f933ef717b9ca4a20bf3 ]]

while IFS= read -r unit; do
  enabled=$(systemctl is-enabled "$unit" 2>/dev/null || true)
  active=$(systemctl is-active "$unit" 2>/dev/null || true)
  [[ "$enabled" == enabled || "$enabled" == disabled ]]
  [[ "$active" == active || "$active" == inactive ]]
  printf '%s\t%s\t%s\n' "$unit" "$enabled" "$active" >>"$timer_state"
done < <(
  systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
    | sed -E 's/[[:space:]].*$//' | sort -u
)
[[ -s "$timer_state" ]]

timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  systemctl stop "$unit"
done <"$timer_state"
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 60); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
done <"$timer_state"

partial="$snapshot_root/.memory_pre_v5_2_claim_target_stage_$run_id.dump.partial"
backup="$snapshot_root/memory_pre_v5_2_claim_target_stage_$run_id.dump"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$partial"
[[ -s "$partial" ]]
mv "$partial" "$backup"
chmod 0600 "$backup"
backup_sha256=$(sha256sum "$backup" | cut -d' ' -f1)
printf '%s  %s\n' "$backup_sha256" "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

before=$(snapshot_counts)
qdrant_before=$(qdrant_signature)

common=(
  env
  POSTGRES_DSN="$POSTGRES_DSN"
  PYTHONPATH="$repo_root:$repo_root/scripts"
)
"${common[@]}" "$python_bin" "$stage_script" manifest \
  --owner "$owner" \
  --review-report "$review" \
  --required-head "$required_head" \
  --output "$manifest"
"${common[@]}" "$python_bin" "$stage_script" authorize \
  --manifest "$manifest" \
  --output "$authorization"
"${common[@]}" "$python_bin" "$stage_script" cross-owner \
  --manifest "$manifest" \
  --other-owner "$other_owner" \
  --output "$cross_owner"
[[ "$(jq -er '.cross_owner_rejected' "$cross_owner")" == true ]]

"${common[@]}" \
  MEMORY_V1_REQUIRED_HEAD="$required_head" \
  MEMORY_V1_V5_2_CLAIM_TARGET_STAGE_APPLY=authorized \
  "$python_bin" "$stage_script" apply \
  --manifest "$manifest" \
  --authorization "$authorization" \
  --confirm STAGE_REVIEWED_V5_2_CLAIM_TARGETS_ONLY \
  --output "$apply_result"
"${common[@]}" \
  MEMORY_V1_REQUIRED_HEAD="$required_head" \
  MEMORY_V1_V5_2_CLAIM_TARGET_STAGE_APPLY=authorized \
  "$python_bin" "$stage_script" replay \
  --manifest "$manifest" \
  --authorization "$authorization" \
  --confirm STAGE_REVIEWED_V5_2_CLAIM_TARGETS_ONLY \
  --output "$replay_result"

[[ "$(jq -er '.rows_written' "$apply_result")" == 8 ]]
[[ "$(jq -er '.rows_written' "$replay_result")" == 0 ]]
[[ "$(jq -er '.claims_written' "$apply_result")" == 0 ]]

after=$(snapshot_counts)
for table in projection_plan projection_plan_item projection_claim_payload \
  projection_plan_observation; do
  [[ "$(( $(count_from "$after" "$table") - $(count_from "$before" "$table") ))" \
    == 2 ]]
done
for table in entity observation observation_entity_binding \
  observation_entailment_v5 claim claim_revision claim_observation \
  projection_review projection_apply_event projection_outbox; do
  [[ "$(count_from "$after" "$table")" == "$(count_from "$before" "$table")" ]]
done
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

restore_timers
[[ "$(systemctl is-active brains.service)" == active ]]
curl --fail --silent --max-time 5 \
  -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
  http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null

report="$snapshot_root/memory_v1_v5_2_claim_target_stage_$run_id.json"
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$required_head" \
  --arg backup "$backup" \
  --arg backup_sha256 "$backup_sha256" \
  --arg manifest "$manifest" \
  --arg manifest_sha256 "$(jq -er '.manifest_sha256' "$manifest")" \
  --arg qdrant_sha256 "$qdrant_before" \
  '{
    contract_version:"memory_v1_v5_2_claim_target_stage_production_v1",
    completed_at:$completed_at,
    head_commit:$head_commit,
    backup:{path:$backup,sha256:$backup_sha256},
    manifest:{path:$manifest,sha256:$manifest_sha256},
    results:{
      staged_create:1,
      staged_reinforce:1,
      held_manual_review:1,
      staging_rows:8,
      replay_rows:0,
      claim_changes:0,
      qdrant_changes:0,
      retrieval_changes:0,
      prompt_influence:0,
      cross_owner_rejected:true,
      timers_restored:true
    },
    qdrant_sha256:$qdrant_sha256
  }' >"$report"
chmod 0600 "$report"

printf '%s\n' \
  'CLAIM_TARGET_STAGE_PRODUCTION=PASS' \
  "HEAD=$required_head" \
  "BACKUP=$backup" \
  "BACKUP_SHA256=$backup_sha256" \
  "MANIFEST=$manifest" \
  "REPORT=$report" \
  'STAGING_ROWS=8' \
  'CLAIM_WRITES=0' \
  'QDRANT_WRITES=0'
