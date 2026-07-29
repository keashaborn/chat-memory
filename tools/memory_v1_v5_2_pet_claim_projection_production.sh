#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Copies exactly eleven validated pet claim vectors from
# the isolated shadow collection to memory_claim_v1 and records their exact
# owner-scoped outbox completion. No model call is made. Because governed
# memory is already active for the owner, successful completion makes these
# claims eligible for directly relevant live answers.

if [[ "${MEMORY_V1_V5_2_PET_CLAIM_LIVE_ACTIVATION:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_PET_CLAIM_LIVE_ACTIVATION=authorized is required' >&2
  exit 1
fi

[[ "$EUID" -eq 0 ]]
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
plan="$repo_root/evals/memory_v1_v5_2_pet_claim_projection_plan_20260729.json"
runner="$repo_root/scripts/memory_v1_v5_2_pet_claim_projection_activate.py"
expected_plan_sha=115e06d6c78ea3187f7e0e593fe1e8a697ed50a4981b36de21f8c0da404ef7b7
source_collection=memory_claim_v1_shadow_pet_115e06d6c78e
target_collection=memory_claim_v1
container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
python_bin=/opt/chat-memory/venv/bin/python
review_root=/home/ubuntu/memory-v1-reviews
snapshot_root=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_pet_claim_live_activation.lock
phase=initialization
run_id=
artifact_dir=
status_file=
timers_quiesced=0
brains_quiesced=0
brains_state_before=
timer_state=$(mktemp /tmp/memory-v1-pet-live-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-pet-live-tables.XXXXXX)

psql_row() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | sed -n '1p'
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
  if [[ "$timers_quiesced" -eq 1 ]]; then
    while IFS=$'\t' read -r unit enabled active; do
      [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
      [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
      if [[ "$active" == active ]]; then
        sudo -n systemctl start "$unit"
      else
        sudo -n systemctl stop "$unit"
      fi
      [[ "$(systemctl is-active "$unit")" == "$active" ]]
    done <"$timer_state"
    timers_quiesced=0
  fi
}

record_exit() {
  code=$?
  if [[ "$code" -eq 0 && "$phase" != complete ]]; then code=1; fi
  restore_runtime || code=1
  rm -f "$timer_state" "$table_list"
  if [[ -n "$status_file" ]]; then
    printf 'run_id=%s\nphase=%s\nexit_code=%s\ncompleted_at=%s\n' \
      "$run_id" "$phase" "$code" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$code"
}
trap record_exit EXIT

capture_memory_non_outbox() {
  local output=$1 table state
  : >"$output"
  while IFS= read -r table; do
    state=$(psql_row "
      SELECT count(*)::text || E'\t' ||
             encode(public.digest(convert_to(
               coalesce(string_agg(row_json,E'\n' ORDER BY row_json),''),
               'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value
      ) AS rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

capture_non_target_outbox() {
  local output=$1 claim_csv
  claim_csv=$(jq -r '[.items[].claim_id]|join(",")' "$plan")
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "
    SELECT count(*)::text || E'\t' ||
           encode(public.digest(convert_to(
             coalesce(string_agg(row_json,E'\n' ORDER BY row_json),''),
             'UTF8'),'sha256'),'hex')
    FROM (
      SELECT to_jsonb(value)::text AS row_json
      FROM memory.projection_outbox AS value
      WHERE owner_user_id IS DISTINCT FROM '$owner'::uuid
         OR aggregate_id <> ALL(string_to_array('$claim_csv',',')::uuid[])
    ) AS rows
  " >"$output"
  chmod 0600 "$output"
}

capture_collection() {
  local collection=$1 output=$2
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    "$QDRANT_URL/collections/$collection/points/scroll" \
    | jq -cS '.result.points | sort_by(.id|tostring) | .[]' >"$output"
  chmod 0600 "$output"
}

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
[[ "$(sha256sum "$plan" | awk '{print $1}')" == "$expected_plan_sha" ]]
git -C "$repo_root" merge-base --is-ancestor \
  "$(jq -er '.required_ancestor_commit' "$plan")" HEAD
[[ "$(jq -er '.items|length' "$plan")" == 11 ]]

set -a
source "$repo_root/.env"
set +a
[[ -n "${POSTGRES_DSN:-}" && -n "${QDRANT_URL:-}" ]]
authenticated_health
exec 9>"$lock_file"
flock -n 9
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
artifact_dir="$review_root/pet-claim-projection-live-$run_id"
status_file="$snapshot_root/memory_v1_v5_2_pet_claim_projection_live_${run_id}.status"
mkdir -p "$artifact_dir"
chmod 0700 "$artifact_dir"

phase=capture_timer_state
: >"$timer_state"
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ -s "$timer_state" ]]
chmod 0600 "$timer_state"

phase=quiesce
while IFS=$'\t' read -r unit _enabled active; do
  [[ "$active" != active ]] || sudo -n systemctl stop "$unit"
done <"$timer_state"
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
done <"$timer_state"
brains_state_before=$(systemctl is-active brains.service)
[[ "$brains_state_before" != active ]] || sudo -n systemctl stop brains.service
brains_quiesced=1
for _attempt in $(seq 1 30); do
  systemctl is-active --quiet brains.service || break
  sleep 1
done
! systemctl is-active --quiet brains.service

phase=backup
backup_partial="$snapshot_root/.memory_pre_pet_claim_live_${run_id}.dump.partial"
backup="$snapshot_root/memory_pre_pet_claim_live_${run_id}.dump"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$backup.catalog"
[[ -s "$backup.catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$backup.catalog"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline
docker exec "$container" psql -X -A -t -U sage -d "$database" -c "
  SELECT table_name FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
    AND table_name <> 'projection_outbox'
  ORDER BY table_name
" >"$table_list"
memory_before="$artifact_dir/non-outbox-memory-before.tsv"
memory_after="$artifact_dir/non-outbox-memory-after.tsv"
outbox_other_before="$artifact_dir/non-target-outbox-before.tsv"
outbox_other_after="$artifact_dir/non-target-outbox-after.tsv"
live_before="$artifact_dir/live-qdrant-before.jsonl"
live_after="$artifact_dir/live-qdrant-after.jsonl"
shadow_before="$artifact_dir/shadow-qdrant-before.jsonl"
shadow_after="$artifact_dir/shadow-qdrant-after.jsonl"
capture_memory_non_outbox "$memory_before"
capture_non_target_outbox "$outbox_other_before"
capture_collection "$target_collection" "$live_before"
capture_collection "$source_collection" "$shadow_before"

phase=activate
apply="$artifact_dir/activation-apply.json"
replay="$artifact_dir/activation-replay.json"
MEMORY_V1_V5_2_PET_CLAIM_ACTIVATION=authorized \
MEMORY_V1_V5_2_PET_CLAIM_LIVE_ACTIVATION=authorized \
PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$runner" --mode apply --plan "$plan" \
  --source-collection "$source_collection" \
  --target-collection "$target_collection" --output "$apply"
[[ "$(jq -er '.mode' "$apply")" == apply ]]
[[ "$(jq -er '.claim_count' "$apply")" == 11 ]]
[[ "$(jq -er '.embedding_requests' "$apply")" == 0 ]]
[[ "$(jq -er '.external_model_calls' "$apply")" == 0 ]]
[[ "$(jq -er '.qdrant_writes' "$apply")" == 11 ]]
[[ "$(jq -er '.postgres_rows_mutated' "$apply")" == 33 ]]
[[ "$(jq -er '[.shadow_tests[]|select(
  .selected_count>=1 and .other_owner_database_record_count==0 and
  .prompt_influence==false)]|length' "$apply")" == 11 ]]

phase=replay
MEMORY_V1_V5_2_PET_CLAIM_ACTIVATION_REPLAY=authorized \
MEMORY_V1_V5_2_PET_CLAIM_LIVE_ACTIVATION=authorized \
PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$runner" --mode replay --plan "$plan" \
  --source-collection "$source_collection" \
  --target-collection "$target_collection" --output "$replay"
[[ "$(jq -er '.mode' "$replay")" == replay ]]
[[ "$(jq -er '.embedding_requests' "$replay")" == 0 ]]
[[ "$(jq -er '.qdrant_writes' "$replay")" == 0 ]]
[[ "$(jq -er '.postgres_rows_mutated' "$replay")" == 0 ]]

phase=verify
capture_memory_non_outbox "$memory_after"
capture_non_target_outbox "$outbox_other_after"
cmp -s "$memory_before" "$memory_after"
cmp -s "$outbox_other_before" "$outbox_other_after"
capture_collection "$target_collection" "$live_after"
capture_collection "$source_collection" "$shadow_after"
cmp -s "$shadow_before" "$shadow_after"

PLAN="$plan" BEFORE="$live_before" AFTER="$live_after" SHADOW="$shadow_after" \
  "$python_bin" - <<'PY'
import json
import os
from pathlib import Path


def load(path: str) -> dict[str, dict]:
    result = {}
    for line in Path(path).read_text().splitlines():
        value = json.loads(line)
        result[str(value["id"])] = value
    return result


plan = json.loads(Path(os.environ["PLAN"]).read_text())
before = load(os.environ["BEFORE"])
after = load(os.environ["AFTER"])
shadow = load(os.environ["SHADOW"])
items = {item["claim_id"]: item for item in plan["items"]}
ids = set(items)
if set(before) & ids != {
    item["claim_id"] for item in plan["items"] if item["prior_qdrant"] == "revision_2"
}:
    raise SystemExit("live target baseline differs")
if set(after) != set(before) | ids:
    raise SystemExit("live point set delta differs")
for point_id, value in before.items():
    if point_id not in ids and after.get(point_id) != value:
        raise SystemExit("non-target live point changed")
for point_id, item in items.items():
    if after.get(point_id) != shadow.get(point_id):
        raise SystemExit("live target does not match validated shadow point")
    payload = after[point_id].get("payload") or {}
    if (
        payload.get("owner_user_id")
        != "1240822d-ac9a-4096-95aa-e2b24d36ef50"
        or payload.get("claim_id") != point_id
        or payload.get("predicate") != item["predicate"]
        or payload.get("revision_number") != item["revision_number"]
    ):
        raise SystemExit("live target payload differs")
PY

claim_csv=$(jq -r '[.items[].claim_id]|join(",")' "$plan")
[[ "$(psql_row "
  SELECT count(*) FROM memory.projection_outbox
  WHERE owner_user_id='$owner'::uuid
    AND aggregate_id=ANY(string_to_array('$claim_csv',',')::uuid[])
    AND status='done' AND attempts=1
")" == 11 ]]

phase=restore_runtime
restore_runtime
authenticated_health

phase=report
report="$artifact_dir/report.json"
jq -n \
  --arg head "$(git -C "$repo_root" rev-parse HEAD)" \
  --arg backup "$backup" \
  --arg backup_sha256 "$(awk '{print $1}' "$backup.sha256")" \
  --arg plan_sha256 "$expected_plan_sha" \
  --arg apply_sha256 "$(sha256sum "$apply" | awk '{print $1}')" \
  --arg replay_sha256 "$(sha256sum "$replay" | awk '{print $1}')" \
  '{
    contract_version:"memory_v1_v5_2_pet_claim_projection_live_report_v1",
    head_commit:$head,
    plan_sha256:$plan_sha256,
    backup:$backup,
    backup_sha256:$backup_sha256,
    apply_file_sha256:$apply_sha256,
    replay_file_sha256:$replay_sha256,
    claims_activated:11,
    new_live_points:10,
    replaced_live_points:1,
    embedding_requests:0,
    external_model_calls:0,
    qdrant_writes:11,
    exact_outbox_rows:11,
    owner_shadow_tests_passed:11,
    cross_owner_isolation:true,
    non_target_memory_unchanged:true,
    non_target_qdrant_unchanged:true,
    validated_shadow_collection_unchanged:true,
    replay_writes:0,
    prompt_configuration_changes:0,
    answer_eligibility_changed_for_directly_relevant_owner_queries:true,
    services_healthy:true,
    timers_restored:true
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
phase=complete
printf 'report=%s\n' "$report"
printf 'report_sha256=%s\n' "$(awk '{print $1}' "$report.sha256")"
printf 'memory_v1_v5_2_pet_claim_projection_production: PASS\n'
