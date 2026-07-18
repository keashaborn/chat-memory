#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Applies one hash-locked reviewed V5 relational stage plan,
# proves exact target-owner deltas and zero-write replay, and proves that all
# non-target owner/global Memory rows plus Qdrant remain unchanged.

if [[ "${MEMORY_V1_V5_STAGE_BATCH_PRODUCTION_APPLY:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_STAGE_BATCH_PRODUCTION_APPLY=authorized is required' >&2
  exit 1
fi
if [[ "$#" -ne 3 ]]; then
  echo 'usage: stage_batch_production_apply.sh PLAN AUTHORIZATION STAGE_REPORT' >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel)
plan=$(realpath "$1")
authorization=$(realpath "$2")
stage_report=$(realpath -m "$3")
review_root=/home/ubuntu/memory-v1-reviews
target_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
runner=scripts/memory_v1_v5_stage_batch.py
lock_file=/home/ubuntu/brains/.memory_v1_v5_stage_batch_apply.lock
phase=initialization
run_tag=
status_file=
units_quiesced=0
unit_state=$(mktemp /tmp/memory-v1-v5-stage-apply-units.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-stage-apply-tables.XXXXXX)

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | tr -d '[:space:]'
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
  [[ "$units_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$unit_state"
  units_quiesced=0
}

record_exit() {
  exit_code=$?
  if [[ "$exit_code" -eq 0 && "$phase" != complete ]]; then
    exit_code=1
  fi
  restore_timers || exit_code=1
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
        predicate="owner_user_id<>'$target_owner'::uuid"
      fi
    elif [[ "$partition" == target ]]; then
      continue
    else
      predicate=true
    fi
    state=$(psql_scalar "
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
  PLAN="$plan" BEFORE="$target_before" AFTER="$target_after" \
    python3 - <<'PY'
import json, os
from pathlib import Path

def load(path):
    rows = {}
    for line in Path(path).read_text().splitlines():
        table, count, digest = line.split("\t")
        rows[table] = (int(count), digest)
    return rows

plan = json.loads(Path(os.environ["PLAN"]).read_text())
before = load(os.environ["BEFORE"])
after = load(os.environ["AFTER"])
if before.keys() != after.keys():
    raise SystemExit("target-owner table set changed")
totals = {key: 0 for key in (
    "mentions", "resolutions", "candidates", "observations", "temporals")}
for bundle in plan["bundles"]:
    for key in totals:
        totals[key] += int(bundle["expected_counts"][key])
expected = {
    "entity_mention": totals["mentions"],
    "entity_resolution_plan": totals["resolutions"],
    "entity_resolution_candidate": totals["candidates"],
    "observation": totals["observations"],
    "observation_temporal": totals["temporals"],
    "relational_stage_batch": int(plan["bundle_count"]),
    "relational_operation_request": int(plan["bundle_count"]),
}
for table in before:
    delta = after[table][0] - before[table][0]
    wanted = expected.get(table, 0)
    if delta != wanted:
        raise SystemExit(f"unexpected target-owner delta {table}: {delta} != {wanted}")
    if wanted == 0 and before[table][1] != after[table][1]:
        raise SystemExit(f"unexpected target-owner mutation {table}")
if sum(expected.values()) != int(plan["expected_new_rows"]):
    raise SystemExit("plan row budget does not match mapped stage tables")
PY
}

for input in "$plan" "$authorization"; do
  [[ "$input" == "$review_root"/* ]]
  [[ -f "$input" && "$(stat -c '%a' "$input")" == 600 ]]
done
[[ "$stage_report" == "$review_root"/* && ! -e "$stage_report" ]]
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
[[ -n "${POSTGRES_DSN:-}" ]]
head=$(git -C "$repo_root" rev-parse HEAD)
[[ "$(jq -er '.contract_version' "$plan")" == memory_v1_v5_stage_batch_plan_v1 ]]
[[ "$(jq -er '.target_server' "$plan")" == seebx ]]
[[ "$(jq -er '.owner_user_id' "$plan")" == "$target_owner" ]]
[[ "$(jq -er '.required_head_commit' "$plan")" == "$head" ]]
[[ "$(jq -er '.mode' "$plan")" == preflight_only_zero_write ]]
expected_rows=$(jq -er '.expected_new_rows' "$plan")
[[ "$expected_rows" =~ ^[0-9]+$ && "$expected_rows" -gt 0 && "$expected_rows" -le 1500 ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_stage_batch_apply_${run_tag}.status"
target_before="$snapshot_dir/memory_v1_v5_stage_batch_target_before_${run_tag}.tsv"
target_after="$snapshot_dir/memory_v1_v5_stage_batch_target_after_${run_tag}.tsv"
non_target_before="$snapshot_dir/memory_v1_v5_stage_batch_non_target_before_${run_tag}.tsv"
non_target_after="$snapshot_dir/memory_v1_v5_stage_batch_non_target_after_${run_tag}.tsv"

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

phase=backup
backup_partial="$snapshot_dir/.memory_pre_v5_stage_batch_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_stage_batch_${run_tag}.dump"
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
        AND column_info.column_name='owner_user_id'
    )
    FROM information_schema.tables AS tables
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name" >"$table_list"
[[ -s "$table_list" ]]
capture_partition target "$target_before"
capture_partition non_target "$non_target_before"
qdrant_before=$(qdrant_signature)

phase=apply
MEMORY_V1_V5_STAGE_BATCH_APPLY=authorized \
  PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$repo_root/$runner" apply \
  --plan "$plan" --authorization "$authorization" --output "$stage_report" \
  --review-root "$review_root" \
  --confirm STAGE_REVIEWED_OWNER_V5_PACKETS_ONLY
[[ -f "$stage_report" && "$(stat -c '%a' "$stage_report")" == 600 ]]
[[ "$(jq -er '.database_rows_created' "$stage_report")" == "$expected_rows" ]]
[[ "$(jq -er '.checks.replay_rows_written' "$stage_report")" == 0 ]]
[[ "$(jq -er '.checks.external_model_calls' "$stage_report")" == 0 ]]
[[ "$(jq -er '.checks.qdrant_calls' "$stage_report")" == 0 ]]

phase=postflight
capture_partition target "$target_after"
capture_partition non_target "$non_target_after"
cmp -s "$non_target_before" "$non_target_after"
verify_target_delta
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]

restore_timers

phase=report
report="$snapshot_dir/memory_v1_v5_stage_batch_apply_${run_tag}.json"
stage_report_sha=$(sha256sum "$stage_report" | awk '{print $1}')
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$head" --arg owner_user_id "$target_owner" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg stage_report "$stage_report" --arg stage_report_sha256 "$stage_report_sha" \
  --arg target_before "$target_before" --arg target_after "$target_after" \
  --arg non_target_before "$non_target_before" --arg non_target_after "$non_target_after" \
  --arg qdrant_sha256 "$qdrant_after" --argjson database_rows_created "$expected_rows" \
  '{contract_version:"memory_v1_v5_stage_batch_production_apply_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,owner_user_id:$owner_user_id,
    backup:{path:$backup,sha256:$backup_sha256},
    stage_report:{path:$stage_report,sha256:$stage_report_sha256},
    evidence:{target_before:$target_before,target_after:$target_after,
      non_target_before:$non_target_before,non_target_after:$non_target_after,
      qdrant_sha256:$qdrant_sha256},
    database_rows_created:$database_rows_created,
    checks:{fresh_backup:true,transactional_apply:true,exact_target_owner_deltas:true,
      zero_write_replay:true,non_target_and_global_memory_rows_unchanged:true,
      qdrant_unchanged:true,timers_restored:true,external_model_calls:0},
    hard_stop:"before_entity_resolution_review_or_apply_or_projection_or_live_retrieval"}' \
  >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf '%s\n' 'memory_v1_v5_stage_batch_production_apply: PASS'
printf 'report=%s\nstage_report=%s\nbackup=%s\n' "$report" "$stage_report" "$backup"
