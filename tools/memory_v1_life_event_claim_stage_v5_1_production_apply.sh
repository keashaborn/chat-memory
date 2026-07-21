#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Applies exactly one reviewed, hash-locked V5.1
# death-event projection plan. It creates four manual-review staging rows and
# no claim, vector, retrieval, or prompt-influence state.

if [[ "${MEMORY_V1_LIFE_EVENT_CLAIM_V5_1_STAGE_APPLY:-}" != authorized ]]; then
  echo 'MEMORY_V1_LIFE_EVENT_CLAIM_V5_1_STAGE_APPLY=authorized is required' >&2
  exit 1
fi
if [[ "$#" -ne 4 ]]; then
  echo 'usage: memory_v1_life_event_claim_stage_v5_1_production_apply.sh BUNDLE PREFLIGHT APPLY REPLAY' >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel)
required_ancestor=4928cf884c1d1919d4b9df4f934a8fe702f17f25
runner=scripts/memory_v1_life_event_claim_stage_v5_1.py
projector=scripts/memory_v1_life_event_claim_projection_v5_1.py
expected_runner_sha=9d80ce730579e8f437f6d8e2ae269cd7d6616a08226d154b371443ae84b2d4db
expected_projector_sha=33f8ebbf66cb7cd98b7044dcc93e4b2be978b54819a599eb89f2aaf519bdb653
bundle=$(realpath "$1")
preflight_result=$(realpath -m "$2")
apply_result=$(realpath -m "$3")
replay_result=$(realpath -m "$4")
review_root=/home/ubuntu/memory-v1-reviews
target_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_life_event_claim_v5_1_stage.lock
phase=initialization
status_file=
run_id=
units_quiesced=0
brains_quiesced=0
brains_state_before=
unit_state=$(mktemp /tmp/memory-v1-life-event-v5-1-stage-units.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-life-event-v5-1-stage-tables.XXXXXX)

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

authenticated_health() {
  set -a
  source "$repo_root/.env"
  set +a
  [[ -n "${VS_SERVICE_TOKEN:-}" ]]
  for _attempt in $(seq 1 30); do
    if [[ "$(systemctl is-active brains.service)" == active ]] \
       && curl --fail --silent --max-time 5 \
          -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
          http://127.0.0.1:8088/healthz \
          | jq -e '.status=="ok"' >/dev/null \
       && curl --fail --silent --max-time 5 \
          -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
          http://127.0.0.1:8088/readyz \
          | jq -e '.ok==true and .postgres==true' >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

restore_timers() {
  [[ "$units_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$unit_state"
  units_quiesced=0
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
  if [[ "$code" -eq 0 && "$phase" != complete ]]; then
    code=1
  fi
  restore_runtime || code=1
  rm -f "$unit_state" "$table_list"
  if [[ -n "$status_file" ]]; then
    {
      printf 'run_id=%s\n' "$run_id"
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$code"
      printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$code"
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
      ) AS rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

verify_target_delta() {
  BEFORE="$1" AFTER="$2" python3 - <<'PY'
import os
from pathlib import Path

expected = {
    "projection_plan": 1,
    "projection_plan_item": 1,
    "projection_claim_payload": 1,
    "projection_plan_observation": 1,
}

def load(path):
    rows = {}
    for line in Path(path).read_text().splitlines():
        table, count, digest = line.split("\t")
        rows[table] = (int(count), digest)
    return rows

before = load(os.environ["BEFORE"])
after = load(os.environ["AFTER"])
if before.keys() != after.keys():
    raise SystemExit("target-owner table set changed")
for table in before:
    delta = after[table][0] - before[table][0]
    wanted = expected.get(table, 0)
    if delta != wanted:
        raise SystemExit(f"unexpected target-owner delta {table}: {delta} != {wanted}")
    if wanted == 0 and before[table][1] != after[table][1]:
        raise SystemExit(f"unexpected target-owner mutation {table}")
PY
}

[[ "$bundle" == "$review_root"/* && -f "$bundle" ]]
[[ "$(stat -c '%a' "$bundle")" == 600 ]]
for output in "$preflight_result" "$apply_result" "$replay_result"; do
  [[ "$output" == "$review_root"/* && "$output" == *.json && ! -e "$output" ]]
done
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(sha256sum "$repo_root/$runner" | awk '{print $1}')" == "$expected_runner_sha" ]]
[[ "$(sha256sum "$repo_root/$projector" | awk '{print $1}')" == "$expected_projector_sha" ]]
head=$(git -C "$repo_root" rev-parse HEAD)
[[ "$(jq -er '.contract_version' "$bundle")" == memory_v1_life_event_claim_stage_bundle_v5_1 ]]
[[ "$(jq -er '.owner_user_id' "$bundle")" == "$target_owner" ]]
[[ "$(jq -er '.required_head_commit' "$bundle")" == "$head" ]]
[[ "$(jq -er '.expected_new_rows' "$bundle")" == 4 ]]
[[ "$(jq -er '.predicate' "$bundle")" == life_event.died ]]

set -a
source "$repo_root/.env"
set +a
[[ -n "${POSTGRES_DSN:-}" ]]
authenticated_health

exec 9>"$lock_file"
flock -n 9
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_life_event_claim_v5_1_stage_${run_id}.status"
target_before="$snapshot_dir/memory_v1_life_event_claim_v5_1_stage_target_before_${run_id}.tsv"
target_preflight="$snapshot_dir/memory_v1_life_event_claim_v5_1_stage_target_preflight_${run_id}.tsv"
target_after="$snapshot_dir/memory_v1_life_event_claim_v5_1_stage_target_after_${run_id}.tsv"
target_replay="$snapshot_dir/memory_v1_life_event_claim_v5_1_stage_target_replay_${run_id}.tsv"
non_target_before="$snapshot_dir/memory_v1_life_event_claim_v5_1_stage_non_target_before_${run_id}.tsv"
non_target_preflight="$snapshot_dir/memory_v1_life_event_claim_v5_1_stage_non_target_preflight_${run_id}.tsv"
non_target_after="$snapshot_dir/memory_v1_life_event_claim_v5_1_stage_non_target_after_${run_id}.tsv"
non_target_replay="$snapshot_dir/memory_v1_life_event_claim_v5_1_stage_non_target_replay_${run_id}.tsv"

phase=capture_timer_state
: >"$unit_state"
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$unit_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ -s "$unit_state" ]]
chmod 0600 "$unit_state"

phase=quiesce_timers
while IFS=$'\t' read -r unit _enabled active; do
  [[ "$active" != active ]] || sudo -n systemctl stop "$unit"
done <"$unit_state"
units_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
  [[ "$(systemctl is-active "$unit")" == inactive ]]
done <"$unit_state"

phase=quiesce_brains
brains_state_before=$(systemctl is-active brains.service)
if [[ "$brains_state_before" == active ]]; then
  sudo -n systemctl stop brains.service
fi
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
      EXISTS (
        SELECT 1 FROM information_schema.columns AS column_row
        WHERE column_row.table_schema='memory'
          AND column_row.table_name=table_row.table_name
          AND column_row.column_name='owner_user_id'
      )
    FROM information_schema.tables AS table_row
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name
  " >"$table_list"
capture_partition target "$target_before"
capture_partition non_target "$non_target_before"
qdrant_before=$(qdrant_signature)

phase=backup
backup_partial="$snapshot_dir/.memory_pre_life_event_claim_v5_1_stage_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_life_event_claim_v5_1_stage_${run_id}.dump"
catalog="$backup.catalog"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$catalog"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=zero_write_preflight
MEMORY_V1_REQUIRED_HEAD="$head" PYTHONPATH="$repo_root/scripts" \
  "$repo_root/venv/bin/python" "$repo_root/$runner" \
  --mode preflight --bundle "$bundle" --output "$preflight_result"
[[ "$(jq -er '.rows_written' "$preflight_result")" == 0 ]]
capture_partition target "$target_preflight"
capture_partition non_target "$non_target_preflight"
cmp -s "$target_before" "$target_preflight"
cmp -s "$non_target_before" "$non_target_preflight"

phase=cross_owner_probe
cross_owner_error="$snapshot_dir/memory_v1_life_event_claim_v5_1_stage_cross_owner_${run_id}.log"
if psql "$POSTGRES_DSN" -X -v ON_ERROR_STOP=1 \
  -v owner="$other_owner" -v plan="$(jq -er '.plan_id' "$bundle")" \
  -v packet="$(jq -er '.packet_text' "$bundle")" \
  >"$cross_owner_error" 2>&1 <<'SQL'
BEGIN READ ONLY;
SELECT set_config('app.user_id', :'owner', true);
SELECT * FROM memory.preflight_claim_projection_packet_v5_1(
  :'plan'::uuid, :'packet'
);
ROLLBACK;
SQL
then
  echo 'cross-owner death-event packet was accepted' >&2
  exit 1
fi
rg -q 'complete accepted owner-scoped death-event source not found' \
  "$cross_owner_error"
chmod 0600 "$cross_owner_error"

phase=transactional_apply
MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_LIFE_EVENT_CLAIM_STAGE_APPLY=authorized \
PYTHONPATH="$repo_root/scripts" "$repo_root/venv/bin/python" \
  "$repo_root/$runner" --mode apply --bundle "$bundle" \
  --output "$apply_result"
[[ "$(jq -er '.rows_written' "$apply_result")" == 4 ]]
capture_partition target "$target_after"
capture_partition non_target "$non_target_after"
verify_target_delta "$target_before" "$target_after"
cmp -s "$non_target_before" "$non_target_after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=zero_write_replay
MEMORY_V1_REQUIRED_HEAD="$head" PYTHONPATH="$repo_root/scripts" \
  "$repo_root/venv/bin/python" "$repo_root/$runner" \
  --mode replay --bundle "$bundle" --output "$replay_result"
[[ "$(jq -er '.rows_written' "$replay_result")" == 0 ]]
[[ "$(jq -er '.outcome' "$replay_result")" == replayed ]]
capture_partition target "$target_replay"
capture_partition non_target "$non_target_replay"
cmp -s "$target_after" "$target_replay"
cmp -s "$non_target_after" "$non_target_replay"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

phase=restore_runtime
restore_runtime
authenticated_health

phase=report
report="$snapshot_dir/memory_v1_life_event_claim_v5_1_stage_${run_id}.json"
REPORT="$report" BACKUP="$backup" CATALOG="$catalog" BUNDLE="$bundle" \
PREFLIGHT="$preflight_result" APPLY="$apply_result" REPLAY="$replay_result" \
CROSS_OWNER="$cross_owner_error" TARGET_BEFORE="$target_before" \
TARGET_AFTER="$target_after" NON_TARGET_BEFORE="$non_target_before" \
NON_TARGET_AFTER="$non_target_after" QDRANT="$qdrant_before" HEAD="$head" \
python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

bundle = json.loads(Path(os.environ["BUNDLE"]).read_text())
report = {
    "contract_version": "memory_v1_life_event_claim_v5_1_stage_report_v1",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "owner_user_id": bundle["owner_user_id"],
    "bundle_sha256": bundle["bundle_sha256"],
    "backup": {"path": os.environ["BACKUP"], "catalog": os.environ["CATALOG"]},
    "evidence": {
        "preflight_result": os.environ["PREFLIGHT"],
        "apply_result": os.environ["APPLY"],
        "replay_result": os.environ["REPLAY"],
        "cross_owner_rejection": os.environ["CROSS_OWNER"],
        "target_before": os.environ["TARGET_BEFORE"],
        "target_after": os.environ["TARGET_AFTER"],
        "non_target_before": os.environ["NON_TARGET_BEFORE"],
        "non_target_after": os.environ["NON_TARGET_AFTER"],
    },
    "verification": {
        "rows_written": 4,
        "exact_target_delta": True,
        "zero_write_replay": True,
        "cross_owner_rejected": True,
        "non_target_and_global_memory_unchanged": True,
        "qdrant_sha256": os.environ["QDRANT"],
        "qdrant_unchanged": True,
        "claims_written": 0,
        "retrieval_activated": False,
        "prompt_influence_activated": False,
        "timers_restored_exactly": True,
        "brains_service_restored": True,
    },
    "hard_stop": "before_life_event_plan_review_or_claim_materialization",
}
path = Path(os.environ["REPORT"])
path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
path.chmod(0o600)
PY
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
phase=complete
printf 'backup=%s\nreport=%s\n' "$backup" "$report"
printf 'memory_v1_life_event_claim_stage_v5_1_production_apply: PASS\n'
