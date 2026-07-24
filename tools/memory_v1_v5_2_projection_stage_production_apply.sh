#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Applies one exact, private, hash-locked four-item V5.2
# projection-stage manifest. Writes accepted entailments and pending-review
# projection plans only. It never materializes claims or touches Qdrant.

if [[ "${MEMORY_V1_V5_2_PROJECTION_STAGE_PRODUCTION_APPLY:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_PROJECTION_STAGE_PRODUCTION_APPLY=authorized is required' >&2
  exit 1
fi
if [[ "$#" -ne 2 ]]; then
  echo 'usage: ..._projection_stage_production_apply.sh MANIFEST AUTHORIZATION' >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel)
manifest=$(realpath "$1")
authorization=$(realpath "$2")
review_root=/home/ubuntu/memory-v1-reviews
snapshot_dir=/home/ubuntu/brains/snapshots
target_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
target_evidence=22bd0732-3539-4180-8f89-8f84114131c0
container=brains-postgres-1
database=memory
runner=scripts/memory_v1_v5_2_projection_stage_batch.py
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_projection_stage_apply.lock
phase=initialization
run_tag=
status_file=
units_quiesced=0
brains_quiesced=0
brains_state_before=
unit_state=$(mktemp /tmp/memory-v1-v5-2-projection-units.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-2-projection-tables.XXXXXX)

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
  rm -f "$unit_state" "$table_list"
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
      SELECT count(*)::text || E'\\t' || encode(public.digest(convert_to(
        coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
        'UTF8'),'sha256'),'hex')
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
  BEFORE="$target_before" AFTER="$target_after" python3 - <<'PY'
import os
from pathlib import Path

def load(path):
    rows = {}
    for line in Path(path).read_text().splitlines():
        table, count, digest = line.split("\t")
        rows[table] = (int(count), digest)
    return rows

before = load(os.environ["BEFORE"])
after = load(os.environ["AFTER"])
expected = {
    "observation_entailment_v5": 4,
    "relational_operation_request": 4,
    "projection_plan": 4,
    "projection_plan_item": 4,
    "projection_claim_payload": 4,
    "projection_plan_observation": 4,
}
if before.keys() != after.keys():
    raise SystemExit("target-owner table set changed")
for table in before:
    delta = after[table][0] - before[table][0]
    wanted = expected.get(table, 0)
    if delta != wanted:
        raise SystemExit(f"unexpected target-owner delta {table}: {delta} != {wanted}")
    if wanted == 0 and before[table][1] != after[table][1]:
        raise SystemExit(f"unexpected target-owner mutation {table}")
if sum(expected.values()) != 24:
    raise SystemExit("projection-stage row budget mismatch")
PY
}

for input in "$manifest" "$authorization"; do
  [[ "$input" == "$review_root"/* ]]
  [[ -f "$input" && "$(stat -c '%a' "$input")" == 600 ]]
done
[[ "$(jq -er '.target_server' "$manifest")" == seebx ]]
[[ "$(jq -er '.owner_user_id' "$manifest")" == "$target_owner" ]]
[[ "$(jq -er '.evidence_id' "$manifest")" == "$target_evidence" ]]
[[ "$(jq -er '.items|length' "$manifest")" == 4 ]]
[[ "$(jq -er '.expected_new_rows' "$manifest")" == 24 ]]
head=$(git -C "$repo_root" rev-parse HEAD)
[[ "$(jq -er '.required_head_commit' "$manifest")" == "$head" ]]

set -a
source "$repo_root/.env"
set +a
[[ -n "${POSTGRES_DSN:-}" ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_2_projection_stage_${run_tag}.status"
apply_result="$review_root/v5-2-projection-stage-apply-${run_tag}.json"
replay_result="$review_root/v5-2-projection-stage-replay-${run_tag}.json"
target_before="$snapshot_dir/memory_v1_v5_2_projection_target_before_${run_tag}.tsv"
target_after="$snapshot_dir/memory_v1_v5_2_projection_target_after_${run_tag}.tsv"
target_replay="$snapshot_dir/memory_v1_v5_2_projection_target_replay_${run_tag}.tsv"
non_target_before="$snapshot_dir/memory_v1_v5_2_projection_non_target_before_${run_tag}.tsv"
non_target_after="$snapshot_dir/memory_v1_v5_2_projection_non_target_after_${run_tag}.tsv"
non_target_replay="$snapshot_dir/memory_v1_v5_2_projection_non_target_replay_${run_tag}.tsv"

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
backup_partial="$snapshot_dir/.memory_pre_v5_2_projection_stage_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_2_projection_stage_${run_tag}.dump"
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
docker exec "$container" psql -X -A -t -F $'\t' -v ON_ERROR_STOP=1 \
  -U sage -d "$database" -c "
    SELECT table_name, EXISTS (
      SELECT 1 FROM information_schema.columns AS column_info
      WHERE column_info.table_schema='memory'
        AND column_info.table_name=tables.table_name
        AND column_info.column_name='owner_user_id')
    FROM information_schema.tables AS tables
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name" >"$table_list"
[[ -s "$table_list" ]]
capture_partition target "$target_before"
capture_partition non_target "$non_target_before"
qdrant_before=$(qdrant_signature)

phase=apply
MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_PROJECTION_STAGE_BATCH_APPLY=authorized \
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$repo_root/$runner" apply \
  --manifest "$manifest" --authorization "$authorization" \
  --confirm STAGE_OWNER_V5_2_REPORTED_STANCE_PROJECTIONS_ONLY \
  --output "$apply_result"
[[ "$(jq -er '.rows_written' "$apply_result")" == 24 ]]
[[ "$(jq -er '.item_count' "$apply_result")" == 4 ]]
capture_partition target "$target_after"
capture_partition non_target "$non_target_after"
verify_target_delta
cmp -s "$non_target_before" "$non_target_after"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

phase=replay
MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_PROJECTION_STAGE_BATCH_APPLY=authorized \
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$repo_root/$runner" replay \
  --manifest "$manifest" --authorization "$authorization" \
  --confirm STAGE_OWNER_V5_2_REPORTED_STANCE_PROJECTIONS_ONLY \
  --output "$replay_result"
[[ "$(jq -er '.rows_written' "$replay_result")" == 0 ]]
capture_partition target "$target_replay"
capture_partition non_target "$non_target_replay"
cmp -s "$target_after" "$target_replay"
cmp -s "$non_target_after" "$non_target_replay"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

phase=postflight
[[ "$(psql_row "SELECT count(*) FROM memory.projection_plan AS plan
  JOIN memory.projection_plan_observation AS link
    ON link.owner_user_id=plan.owner_user_id AND link.plan_id=plan.plan_id
  JOIN memory.observation AS observation
    ON observation.owner_user_id=link.owner_user_id
   AND observation.observation_id=link.observation_id
  WHERE plan.owner_user_id='$target_owner'::uuid
    AND observation.evidence_id='$target_evidence'::uuid
    AND plan.status='pending_review'
    AND plan.predicate_registry_version='memory_predicate_registry_v5_2'")" == 4 ]]
[[ "$(psql_row "SELECT count(*) FROM memory.claim_observation AS link
  JOIN memory.observation AS observation
    ON observation.owner_user_id=link.owner_user_id
   AND observation.observation_id=link.observation_id
  WHERE link.owner_user_id='$target_owner'::uuid
    AND observation.evidence_id='$target_evidence'::uuid")" == 0 ]]

phase=restore
restore_runtime
[[ "$(systemctl is-active brains-postgres.service)" == active ]]
curl --fail --silent --show-error --max-time 30 \
  http://127.0.0.1:8000/health >/dev/null

phase=report
report="$snapshot_dir/memory_v1_v5_2_projection_stage_${run_tag}.json"
REPORT="$report" BACKUP="$backup" MANIFEST="$manifest" \
APPLY="$apply_result" REPLAY="$replay_result" HEAD="$head" \
QDRANT="$qdrant_before" python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

manifest = json.loads(Path(os.environ["MANIFEST"]).read_text())
value = {
    "contract_version": "memory_v1_v5_2_projection_stage_production_report_v1",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "owner_user_id": manifest["owner_user_id"],
    "evidence_id": manifest["evidence_id"],
    "manifest_sha256": manifest["manifest_sha256"],
    "backup": os.environ["BACKUP"],
    "verification": {
        "items": 4,
        "rows_written": 24,
        "zero_write_replay": True,
        "non_target_memory_unchanged": True,
        "qdrant_sha256": os.environ["QDRANT"],
        "qdrant_unchanged": True,
        "claims_written": 0,
        "retrieval_activated": False,
        "prompt_influence_activated": False,
        "timers_restored_exactly": True,
        "brains_service_restored_exactly": True,
    },
    "evidence": {
        "apply_result": os.environ["APPLY"],
        "apply_result_sha256": json.loads(Path(os.environ["APPLY"]).read_text())["result_sha256"],
        "replay_result": os.environ["REPLAY"],
        "replay_result_sha256": json.loads(Path(os.environ["REPLAY"]).read_text())["result_sha256"],
    },
}
path = Path(os.environ["REPORT"])
path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
path.chmod(0o600)
PY

phase=complete
printf 'backup=%s\nreport=%s\n' "$backup" "$report"
printf 'memory_v1_v5_2_projection_stage_production_apply: PASS\n'
