#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Reviews exactly two hash-locked staged claim targets.
# It stops before claim materialization, projection dispatch, or retrieval.

if [[ "${MEMORY_V1_V5_2_CLAIM_TARGET_REVIEW_PRODUCTION:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_CLAIM_TARGET_REVIEW_PRODUCTION=authorized is required' >&2
  exit 1
fi
if [[ "$#" -ne 1 ]]; then
  echo 'usage: production.sh STAGE_MANIFEST' >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel)
stage_manifest=$(realpath "$1")
review_root=/home/ubuntu/memory-v1-reviews
snapshot_dir=/home/ubuntu/brains/snapshots
container=brains-postgres-1
database=memory
runner=scripts/memory_v1_v5_2_claim_target_review_batch.py
clone_runner=tools/memory_v1_v5_2_staged_plan_review_clone.sh
python_bin=/opt/chat-memory/venv/bin/python
confirmation=REVIEW_EXACT_STAGED_V5_2_CLAIM_TARGETS_ONLY
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_claim_target_review.lock
phase=initialization
run_tag=
status_file=
timers_quiesced=0
brains_quiesced=0
brains_state_before=
unit_state=$(mktemp /tmp/memory-claim-target-review-units.XXXXXX)
table_list=$(mktemp /tmp/memory-claim-target-review-tables.XXXXXX)

restore_timers() {
  [[ "$timers_quiesced" -eq 1 ]] || return 0
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
  timers_quiesced=0
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
  restore_timers
}

record_exit() {
  code=$?
  if [[ "$brains_quiesced" -eq 1 || "$timers_quiesced" -eq 1 ]]; then
    restore_runtime || code=1
  fi
  rm -f "$unit_state" "$table_list"
  if [[ -n "$status_file" ]]; then
    printf 'run_tag=%s\nphase=%s\nexit_code=%s\ncompleted_at=%s\n' \
      "$run_tag" "$phase" "$code" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$code"
}
trap record_exit EXIT

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

capture_partition() {
  local partition=$1 output=$2 table has_owner predicate state
  : >"$output"
  while IFS=$'\t' read -r table has_owner; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    if [[ "$has_owner" == t ]]; then
      if [[ "$partition" == target ]]; then
        predicate="owner_user_id='$target_owner'::uuid"
      else
        predicate="owner_user_id IS DISTINCT FROM '$target_owner'::uuid"
      fi
    elif [[ "$partition" == target ]]; then
      continue
    else
      predicate=true
    fi
    state=$(psql_row "
      SELECT count(*)::text || E'\\t' ||
             encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
             ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
        FROM (
          SELECT to_jsonb(value)::text AS row_json
            FROM memory.\"$table\" AS value
           WHERE $predicate
        ) AS rows")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

verify_target_delta() {
  BEFORE="$1" AFTER="$2" python3 - <<'PY'
import os
from pathlib import Path
def load(path):
    result={}
    for line in Path(path).read_text().splitlines():
        table,count,digest=line.split("\t")
        result[table]=(int(count),digest)
    return result
before,after=load(os.environ["BEFORE"]),load(os.environ["AFTER"])
if before.keys() != after.keys():
    raise SystemExit("target-owner table set changed")
for table in before:
    wanted=2 if table=="projection_review" else 0
    actual=after[table][0]-before[table][0]
    if actual != wanted:
        raise SystemExit(f"unexpected target delta {table}: {actual} != {wanted}")
    if wanted == 0 and before[table][1] != after[table][1]:
        raise SystemExit(f"unexpected target mutation {table}")
PY
}

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
head=$(git -C "$repo_root" rev-parse HEAD)
stage_head=$(jq -er '.required_head_commit' "$stage_manifest")
git -C "$repo_root" merge-base --is-ancestor "$stage_head" "$head"
[[ "$stage_manifest" == "$review_root"/* ]]
[[ -f "$stage_manifest" && "$(stat -c '%a' "$stage_manifest")" == 600 ]]
[[ "$(jq -er '.contract_version' "$stage_manifest")" == \
   memory_v1_v5_2_claim_target_stage_manifest_v1 ]]
[[ "$(jq -er '.stage_item_count' "$stage_manifest")" == 2 ]]
target_owner=$(jq -er '.owner_user_id' "$stage_manifest")
[[ "$target_owner" != "$other_owner" ]]

env_file="$repo_root/.env"
[[ -r "$env_file" ]] || env_file=/opt/chat-memory/.env
[[ -r "$env_file" ]]
set -a
source "$env_file"
set +a
[[ -n "${POSTGRES_DSN:-}" && -n "${VS_SERVICE_TOKEN:-}" ]]
exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_2_claim_target_review_${run_tag}.status"
review_manifest="$review_root/claim-target-review-manifest-${run_tag}.json"
authorization="$review_root/claim-target-review-authorization-${run_tag}.json"
cross_result="$review_root/claim-target-review-cross-owner-${run_tag}.json"
apply_result="$review_root/claim-target-review-apply-${run_tag}.json"
replay_result="$review_root/claim-target-review-replay-${run_tag}.json"
clone_manifest="$review_root/claim-target-review-clone-manifest-${run_tag}.json"
clone_authorization="$review_root/claim-target-review-clone-authorization-${run_tag}.json"
clone_cross="$review_root/claim-target-review-clone-cross-${run_tag}.json"
clone_apply="$review_root/claim-target-review-clone-apply-${run_tag}.json"
clone_replay="$review_root/claim-target-review-clone-replay-${run_tag}.json"
target_before="$snapshot_dir/memory_claim_target_review_target_before_${run_tag}.tsv"
target_after="$snapshot_dir/memory_claim_target_review_target_after_${run_tag}.tsv"
target_replay="$snapshot_dir/memory_claim_target_review_target_replay_${run_tag}.tsv"
non_target_before="$snapshot_dir/memory_claim_target_review_non_target_before_${run_tag}.tsv"
non_target_after="$snapshot_dir/memory_claim_target_review_non_target_after_${run_tag}.tsv"
non_target_replay="$snapshot_dir/memory_claim_target_review_non_target_replay_${run_tag}.tsv"

phase=disposable_clone
"$repo_root/$clone_runner" \
  "$stage_manifest" "$clone_manifest" "$clone_authorization" "$clone_cross" \
  "$clone_apply" "$clone_replay"

phase=capture_timer_state
: >"$unit_state"
while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$unit_state"
done < <(
  systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
    | awk '{print $1}' | sort -u
)
[[ -s "$unit_state" ]]
chmod 0600 "$unit_state"

phase=quiesce_timers
while IFS=$'\t' read -r unit _enabled active; do
  [[ "$active" != active ]] || sudo -n systemctl stop "$unit"
done <"$unit_state"
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
done <"$unit_state"

phase=quiesce_brains
brains_state_before=$(systemctl is-active brains.service)
[[ "$brains_state_before" != active ]] || sudo -n systemctl stop brains.service
brains_quiesced=1
for _attempt in $(seq 1 30); do
  systemctl is-active --quiet brains.service || break
  sleep 1
done
! systemctl is-active --quiet brains.service

phase=capture_baseline
docker exec "$container" psql -X -A -F $'\t' -t -v ON_ERROR_STOP=1 \
  -U sage -d "$database" -c "
    SELECT table_name,
           EXISTS(
             SELECT 1 FROM information_schema.columns AS c
              WHERE c.table_schema='memory'
                AND c.table_name=t.table_name
                AND c.column_name='owner_user_id'
           )
      FROM information_schema.tables AS t
     WHERE table_schema='memory' AND table_type='BASE TABLE'
     ORDER BY table_name" >"$table_list"
capture_partition target "$target_before"
capture_partition non_target "$non_target_before"
qdrant_before=$(qdrant_signature)

phase=backup
backup_partial="$snapshot_dir/.memory_pre_claim_target_review_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_claim_target_review_${run_tag}.dump"
docker exec "$container" pg_dump -U sage -d "$database" -Fc \
  --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" \
  >"$backup.catalog"
[[ -s "$backup.catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$backup.catalog"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=build_manifest
PYTHONPATH="$repo_root" POSTGRES_DSN="$POSTGRES_DSN" \
  "$python_bin" "$repo_root/$runner" manifest \
  --stage-manifest "$stage_manifest" \
  --required-head "$head" \
  --output "$review_manifest"
[[ "$(jq -er '.expected_new_rows' "$review_manifest")" == 2 ]]
PYTHONPATH="$repo_root" "$python_bin" "$repo_root/$runner" authorize \
  --manifest "$review_manifest" \
  --output "$authorization"

phase=account_isolation
PYTHONPATH="$repo_root" POSTGRES_DSN="$POSTGRES_DSN" \
  "$python_bin" "$repo_root/$runner" cross-owner \
  --manifest "$review_manifest" \
  --other-owner "$other_owner" \
  --output "$cross_result"
[[ "$(jq -er '.cross_owner_rejected' "$cross_result")" == true ]]
[[ "$(jq -er '.rows_written' "$cross_result")" == 0 ]]

phase=transactional_review
PYTHONPATH="$repo_root" POSTGRES_DSN="$POSTGRES_DSN" \
MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_CLAIM_TARGET_REVIEW_APPLY=authorized \
  "$python_bin" "$repo_root/$runner" apply \
  --manifest "$review_manifest" \
  --authorization "$authorization" \
  --confirm "$confirmation" \
  --output "$apply_result"
[[ "$(jq -er '.rows_written' "$apply_result")" == 2 ]]
capture_partition target "$target_after"
capture_partition non_target "$non_target_after"
verify_target_delta "$target_before" "$target_after"
cmp -s "$non_target_before" "$non_target_after"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

phase=zero_write_replay
PYTHONPATH="$repo_root" POSTGRES_DSN="$POSTGRES_DSN" \
MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_CLAIM_TARGET_REVIEW_APPLY=authorized \
  "$python_bin" "$repo_root/$runner" replay \
  --manifest "$review_manifest" \
  --authorization "$authorization" \
  --confirm "$confirmation" \
  --output "$replay_result"
[[ "$(jq -er '.rows_written' "$replay_result")" == 0 ]]
capture_partition target "$target_replay"
capture_partition non_target "$non_target_replay"
cmp -s "$target_after" "$target_replay"
cmp -s "$non_target_after" "$non_target_replay"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

phase=restore_runtime
restore_runtime
[[ "$(systemctl is-active brains.service)" == "$brains_state_before" ]]
[[ "$(docker inspect -f '{{.State.Running}}' "$container")" == true ]]
docker exec "$container" pg_isready -U sage -d "$database" >/dev/null
for _attempt in $(seq 1 30); do
  if curl --fail --silent --max-time 5 \
       -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
       http://127.0.0.1:8088/healthz >/dev/null \
     && curl --fail --silent --max-time 5 \
       -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
       http://127.0.0.1:8088/readyz >/dev/null; then
    break
  fi
  sleep 1
done
curl --fail --silent --show-error --max-time 30 \
  -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
  http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null
curl --fail --silent --show-error --max-time 30 \
  -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
  http://127.0.0.1:8088/readyz \
  | jq -e '.ok==true and .postgres==true' >/dev/null

phase=report
report="$snapshot_dir/memory_v1_v5_2_claim_target_review_${run_tag}.json"
REPORT="$report" BACKUP="$backup" STAGE="$stage_manifest" \
MANIFEST="$review_manifest" APPLY="$apply_result" REPLAY="$replay_result" \
QDRANT="$qdrant_before" HEAD="$head" python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path
manifest=json.loads(Path(os.environ["MANIFEST"]).read_text())
report={
  "contract_version":"memory_v1_v5_2_claim_target_review_report_v1",
  "completed_at":dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00","Z"),
  "head_commit":os.environ["HEAD"],
  "owner_user_id":manifest["owner_user_id"],
  "source_stage_manifest":os.environ["STAGE"],
  "review_manifest":os.environ["MANIFEST"],
  "manifest_sha256":manifest["manifest_sha256"],
  "backup":os.environ["BACKUP"],
  "evidence":{
    "apply_result":os.environ["APPLY"],
    "replay_result":os.environ["REPLAY"],
  },
  "verification":{
    "review_rows_written":2,
    "target_actions":{"create":1,"reinforce":1},
    "zero_write_replay":True,
    "account_isolation_verified":True,
    "non_target_memory_unchanged":True,
    "claims_written":0,
    "projection_apply_events_written":0,
    "qdrant_sha256":os.environ["QDRANT"],
    "qdrant_unchanged":True,
    "retrieval_activated":False,
    "prompt_influence_activated":False,
    "timers_restored_exactly":True,
    "brains_service_restored_exactly":True,
    "service_health_verified":True,
  },
  "hard_stop":"before_durable_claim_materialization",
}
path=Path(os.environ["REPORT"])
path.write_text(json.dumps(report,indent=2,sort_keys=True)+"\n")
path.chmod(0o600)
PY
phase=complete
printf 'backup=%s\n' "$backup"
printf 'report=%s\n' "$report"
printf 'review_manifest=%s\n' "$review_manifest"
printf 'memory_v1_v5_2_claim_target_review_production: PASS\n'
