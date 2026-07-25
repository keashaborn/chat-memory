#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Finishes the already-applied Neko reinforcement run after
# its original wrapper reached the replay phase with a preflight-order defect.
# Installs the clone-proven replay-safe function, proves all three replays are
# zero-write, verifies the complete memory data set and Qdrant are unchanged,
# and preserves an immutable recovery audit.

if [[ "${MEMORY_V1_V5_2_NEKO_CORRECTION_REINFORCEMENT_RECOVERY:-}" \
      != authorized ]]; then
  echo 'MEMORY_V1_V5_2_NEKO_CORRECTION_REINFORCEMENT_RECOVERY=authorized is required' >&2
  exit 1
fi
if [[ "$#" -ne 1 ]]; then
  echo 'usage: ..._neko_correction_reinforcement_recovery.sh ARTIFACT_DIR' >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel)
artifact_dir=$(realpath "$1")
review_root=/home/ubuntu/memory-v1-reviews
snapshot_dir=/home/ubuntu/brains/snapshots
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
observation=5261da41-f863-42cd-8e3f-6e947f9743f2
claim=8e3f4d82-8c21-4bbd-bbe8-91dd585f6fc9
execution_head=b0ae019f1bc571b14eb0f04861a035c2d82978ba
migration=ops/sql/20260725_memory_v1_v5_2_projection_reinforcement.sql
security_test=tests/memory_v1_v5_2_projection_reinforcement_security.sql
stage_runner=scripts/memory_v1_v5_2_neko_correction_reinforcement_stage.py
review_runner=scripts/memory_v1_v5_2_neko_correction_reinforcement_review_batch.py
apply_runner=scripts/memory_v1_v5_2_neko_correction_reinforcement_apply_batch.py
container=brains-postgres-1
database=memory
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_neko_correction_reinforcement_recovery.lock

stage_manifest="$artifact_dir/stage-manifest.json"
stage_authorization="$artifact_dir/stage-authorization.json"
cross_owner="$artifact_dir/stage-cross-owner.json"
stage_apply="$artifact_dir/stage-apply.json"
review_manifest="$artifact_dir/review-manifest.json"
review_apply="$artifact_dir/review-apply.json"
apply_manifest="$artifact_dir/apply-manifest.json"
apply_result="$artifact_dir/apply-result.json"
stage_replay="$artifact_dir/stage-replay-recovery.json"
review_replay="$artifact_dir/review-replay-recovery.json"
apply_replay="$artifact_dir/apply-replay-recovery.json"

[[ "$repo_root" == /opt/chat-memory ]]
[[ "$artifact_dir" == "$review_root"/* ]]
[[ -d "$artifact_dir" ]]
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor \
  "$execution_head" "$(git -C "$repo_root" rev-parse HEAD)"
for required in \
  "$migration" "$security_test" "$stage_runner" "$review_runner" "$apply_runner"; do
  [[ -f "$repo_root/$required" ]]
done
for required in \
  "$stage_manifest" "$stage_authorization" "$cross_owner" "$stage_apply" \
  "$review_manifest" "$review_apply" "$apply_manifest" "$apply_result"; do
  [[ -f "$required" ]]
  [[ "$(stat -c '%a' "$required")" == 600 ]]
done
for output in "$stage_replay" "$review_replay" "$apply_replay"; do
  [[ ! -e "$output" ]]
done

set -a
source "$repo_root/.env"
set +a
[[ -n "${POSTGRES_DSN:-}" ]]
[[ -n "${VS_SERVICE_TOKEN:-}" ]]

phase=initialization
run_tag=
status_file=
units_quiesced=0
brains_quiesced=0
brains_state_before=
unit_state=$(mktemp /tmp/memory-v1-v5-2-neko-recovery-units.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-2-neko-recovery-tables.XXXXXX)
before_state=$(mktemp /tmp/memory-v1-v5-2-neko-recovery-before.XXXXXX)
after_state=$(mktemp /tmp/memory-v1-v5-2-neko-recovery-after.XXXXXX)

psql_row() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | sed -n '1p'
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

capture_memory_state() {
  local output=$1
  local table state
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    state=$(psql_row "
      SELECT count(*)::text || E'\\t' ||
             encode(public.digest(convert_to(
               coalesce(string_agg(row_text,E'\\n' ORDER BY row_text),''),
               'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_text
        FROM memory.\"$table\" AS value
      ) AS rows")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

restore_runtime() {
  if [[ "$brains_quiesced" -eq 1 ]]; then
    if [[ "$brains_state_before" == active ]]; then
      sudo -n systemctl start brains.service
    else
      sudo -n systemctl stop brains.service
    fi
    [[ "$(systemctl is-active brains.service)" == "$brains_state_before" ]]
    brains_quiesced=0
  fi
  if [[ "$units_quiesced" -eq 1 ]]; then
    while IFS=$'\t' read -r unit enabled active; do
      [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
      if [[ "$active" == active ]]; then
        sudo -n systemctl start "$unit"
      else
        sudo -n systemctl stop "$unit"
      fi
      [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
      [[ "$(systemctl is-active "$unit")" == "$active" ]]
    done <"$unit_state"
    units_quiesced=0
  fi
}

record_exit() {
  exit_code=$?
  if [[ "$exit_code" -eq 0 && "$phase" != complete ]]; then
    exit_code=1
  fi
  restore_runtime || exit_code=1
  rm -f "$unit_state" "$table_list" "$before_state" "$after_state"
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

exec 9>"$lock_file"
flock -n 9
umask 077
head=$(git -C "$repo_root" rev-parse HEAD)
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_2_neko_reinforcement_recovery_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_2_neko_reinforcement_recovery_${run_tag}.json"

phase=quiesce
: >"$unit_state"
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$unit_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ -s "$unit_state" ]]
units_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  sudo -n systemctl stop "$unit"
done <"$unit_state"
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
done <"$unit_state"

brains_state_before=$(systemctl is-active brains.service)
if [[ "$brains_state_before" == active ]]; then
  sudo -n systemctl stop brains.service
fi
brains_quiesced=1
! systemctl is-active --quiet brains.service

phase=backup
backup_partial="$snapshot_dir/.memory_pre_v5_2_neko_recovery_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_2_neko_recovery_${run_tag}.dump"
catalog="$backup.catalog"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$catalog"
backup_sha=$(sha256sum "$backup" | awk '{print $1}')
printf '%s  %s\n' "$backup_sha" "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline
docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
  -U sage -d "$database" -c "
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema='memory'
      AND table_type='BASE TABLE'
    ORDER BY table_name" >"$table_list"
[[ -s "$table_list" ]]
capture_memory_state "$before_state"
qdrant_before=$(qdrant_signature)

phase=install
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$database" <"$repo_root/$migration"
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$database" <"$repo_root/$migration"
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$database" <"$repo_root/$security_test"

phase=replay
MEMORY_V1_REQUIRED_HEAD="$execution_head" \
MEMORY_V1_V5_2_NEKO_CORRECTION_REINFORCEMENT_STAGE_APPLY=authorized \
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python \
  "$repo_root/$stage_runner" replay \
  --manifest "$stage_manifest" --authorization "$stage_authorization" \
  --confirm STAGE_EXACT_NEKO_CORRECTION_REINFORCEMENT_ONLY \
  --output "$stage_replay"
MEMORY_V1_REQUIRED_HEAD="$execution_head" \
MEMORY_V1_V5_2_NEKO_CORRECTION_REINFORCEMENT_REVIEW_APPLY=authorized \
PYTHONPATH="$repo_root/scripts:$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$review_runner" \
  --mode replay --manifest "$review_manifest" --output "$review_replay"
MEMORY_V1_REQUIRED_HEAD="$execution_head" \
PYTHONPATH="$repo_root/scripts:$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$apply_runner" \
  --mode replay --manifest "$apply_manifest" \
  --apply-result "$apply_result" --output "$apply_replay"
[[ "$(jq -er '.rows_written' "$stage_replay")" == 0 ]]
[[ "$(jq -er '.rows_written' "$review_replay")" == 0 ]]
[[ "$(jq -er '.rows_written' "$apply_replay")" == 0 ]]

phase=verify
capture_memory_state "$after_state"
cmp -s "$before_state" "$after_state"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(jq -er '.cross_owner_rejected' "$cross_owner")" == true ]]
[[ "$(jq -er '.rows_written' "$stage_apply")" == 6 ]]
[[ "$(jq -er '.rows_written' "$review_apply")" == 1 ]]
[[ "$(jq -er '.rows_written' "$apply_result")" == 3 ]]
[[ "$(psql_row "
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$owner' AND claim_id='$claim'
    AND status='supported'
    AND canonical_key='v5:d6ecf2322b47c85b98d5776c85252b80992bf7c3e78f4a3759054555cb4a8124'")" == 1 ]]
[[ "$(psql_row "
  SELECT count(*) FROM memory.claim_revision
  WHERE owner_user_id='$owner' AND claim_id='$claim'")" == 2 ]]
[[ "$(psql_row "
  SELECT count(*) FROM memory.claim_observation
  WHERE owner_user_id='$owner' AND claim_id='$claim'
    AND observation_id='$observation' AND stance='supports'")" == 1 ]]
[[ "$(psql_row "
  SELECT count(*) FROM memory.projection_apply_event AS event
  JOIN memory.projection_plan_observation AS link
    USING(owner_user_id,plan_id,projection_ref)
  WHERE event.owner_user_id='$owner'
    AND event.resulting_claim_id='$claim'
    AND event.resulting_claim_revision_number=2
    AND link.observation_id='$observation'
    AND event.outcome='applied'")" == 1 ]]
[[ "$(psql_row "
  SELECT count(*) FROM memory.projection_dispatch_v5 AS dispatch
  JOIN memory.projection_apply_event AS event
    ON event.owner_user_id=dispatch.owner_user_id
   AND event.event_id=dispatch.apply_event_id
  JOIN memory.projection_plan_observation AS link
    ON link.owner_user_id=event.owner_user_id
   AND link.plan_id=event.plan_id
   AND link.projection_ref=event.projection_ref
  WHERE dispatch.owner_user_id='$owner'
    AND dispatch.resulting_claim_id='$claim'
    AND link.observation_id='$observation'")" == 1 ]]
[[ "$(psql_row "
  SELECT strpos(pg_get_functiondef(
    'memory.guard_projection_item_complete_v5()'::regprocedure
  ),'memory.v5_2_canonical_name_reinforcement_policy_bridge(')>0")" == t ]]

phase=restore
restore_runtime
docker exec "$container" pg_isready -U sage -d "$database" >/dev/null
for _attempt in $(seq 1 30); do
  curl --fail --silent --show-error --max-time 3 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/healthz >/dev/null 2>&1 && break
  sleep 1
done
curl --fail --silent --show-error --max-time 30 \
  -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
  http://127.0.0.1:8088/healthz >/dev/null

phase=report
REPORT="$report" BACKUP="$backup" HEAD="$head" \
EXECUTION_HEAD="$execution_head" ARTIFACT="$artifact_dir" \
STAGE_REPLAY="$stage_replay" REVIEW_REPLAY="$review_replay" \
APPLY_REPLAY="$apply_replay" QDRANT="$qdrant_before" python3 - <<'PY'
import datetime as dt
import hashlib
import json
import os
from pathlib import Path

def load(name):
    return json.loads(Path(os.environ[name]).read_text())

def file_sha(name):
    return hashlib.sha256(Path(os.environ[name]).read_bytes()).hexdigest()

stage = load("STAGE_REPLAY")
review = load("REVIEW_REPLAY")
apply = load("APPLY_REPLAY")
value = {
    "contract_version":
        "memory_v1_v5_2_neko_correction_reinforcement_recovery_report_v1",
    "completed_at":
        dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "execution_head": os.environ["EXECUTION_HEAD"],
    "verification_head": os.environ["HEAD"],
    "artifact_dir": os.environ["ARTIFACT"],
    "backup": os.environ["BACKUP"],
    "owner_user_id": "1240822d-ac9a-4096-95aa-e2b24d36ef50",
    "claim_id": "8e3f4d82-8c21-4bbd-bbe8-91dd585f6fc9",
    "observation_id": "5261da41-f863-42cd-8e3f-6e947f9743f2",
    "original_wrapper": {
        "phase": "replay",
        "exit_code": 1,
        "failure": "post_apply_stage_preflight_rejected_existing_link",
        "memory_transaction_failure": False,
    },
    "verification": {
        "stage_replay_rows_written": stage["rows_written"],
        "review_replay_rows_written": review["rows_written"],
        "apply_replay_rows_written": apply["rows_written"],
        "all_memory_rows_unchanged_during_recovery": True,
        "account_isolation_verified": True,
        "qdrant_sha256": os.environ["QDRANT"],
        "qdrant_unchanged": True,
        "claim_revision_number": 2,
        "claim_content_unchanged": True,
        "supporting_observation_linked": True,
        "projection_outbox_written": False,
        "retrieval_or_prompt_configuration_changed": False,
        "timers_restored_exactly": True,
        "brains_service_restored_exactly": True,
    },
    "replay_artifact_sha256": {
        "stage": file_sha("STAGE_REPLAY"),
        "review": file_sha("REVIEW_REPLAY"),
        "apply": file_sha("APPLY_REPLAY"),
    },
}
path = Path(os.environ["REPORT"])
path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
path.chmod(0o600)
PY
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf 'backup=%s\n' "$backup"
printf 'report=%s\n' "$report"
printf 'memory_v1_v5_2_neko_correction_reinforcement_recovery: PASS\n'
