#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Materializes a hash-locked mixed create/reinforce claim
# batch. It deliberately creates no projection_outbox rows, makes no model or
# Qdrant calls, and restores every Memory V1 timer to its exact prior state.

if [[ "${MEMORY_V1_V5_2_REVIEWED_CLAIM_PRODUCTION_APPLY:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_REVIEWED_CLAIM_PRODUCTION_APPLY=authorized is required' >&2
  exit 1
fi
if [[ "$#" -ne 4 ]]; then
  echo 'usage: reviewed_claim_apply_production.sh MANIFEST PREFLIGHT APPLY REPLAY' >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel)
manifest=$(realpath "$1")
preflight_result=$(realpath -m "$2")
apply_result=$(realpath -m "$3")
replay_result=$(realpath -m "$4")
review_root=/home/ubuntu/memory-v1-reviews
snapshot_root=/home/ubuntu/brains/snapshots
container=brains-postgres-1
database=memory
runner=scripts/memory_v1_v5_2_reviewed_claim_apply_batch.py
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_reviewed_claim_apply.lock
phase=initialization
run_tag=
status_file=
units_quiesced=0
brains_quiesced=0
brains_state_before=
unit_state=$(mktemp /tmp/memory-reviewed-claim-units.XXXXXX)
table_list=$(mktemp /tmp/memory-reviewed-claim-tables.XXXXXX)

restore_timers() {
  [[ "$units_quiesced" -eq 1 ]] || return 0
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
  if [[ "$brains_quiesced" -eq 1 || "$units_quiesced" -eq 1 ]]; then
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

authenticated_health() {
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

psql_row() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | sed -n '1p'
}

capture_partition() {
  local partition=$1
  local output=$2
  local table has_owner predicate state
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
    state=$(
      psql_row \
        "SELECT count(*)::text || E'\\t' || encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'UTF8'),'sha256'),'hex') FROM (SELECT to_jsonb(value)::text AS row_json FROM memory.\"$table\" AS value WHERE $predicate) AS rows"
    )
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

capture_qdrant() {
  local target_output=$1
  local other_output=$2
  local response
  response=$(mktemp /tmp/memory-reviewed-claim-qdrant.XXXXXX)
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    >"$response"
  jq -cS --arg owner "$target_owner" \
    '.result.points|sort_by(.id|tostring)|.[]|select(.payload.owner_user_id==$owner)' \
    "$response" >"$target_output"
  jq -cS --arg owner "$target_owner" \
    '.result.points|sort_by(.id|tostring)|.[]|select(.payload.owner_user_id!=$owner)' \
    "$response" >"$other_output"
  chmod 0600 "$target_output" "$other_output"
  rm -f "$response"
}

verify_target_delta() {
  BEFORE="$1" AFTER="$2" MANIFEST="$manifest" python3 - <<'PY'
import json
import os
from pathlib import Path


def load(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        table, count, digest = line.split("\t")
        result[table] = (int(count), digest)
    return result


before = load(os.environ["BEFORE"])
after = load(os.environ["AFTER"])
expected = json.loads(Path(os.environ["MANIFEST"]).read_text())[
    "expected_table_rows"
]
if before.keys() != after.keys():
    raise SystemExit("target table set changed")
for table in before:
    wanted = expected.get(table, 0)
    delta = after[table][0] - before[table][0]
    if delta != wanted:
        raise SystemExit(f"unexpected target delta {table}: {delta} != {wanted}")
    if wanted == 0 and before[table][1] != after[table][1]:
        raise SystemExit(f"unexpected target mutation {table}")
PY
}

[[ "$manifest" == "$review_root"/* ]]
[[ -f "$manifest" && "$(stat -c '%a' "$manifest")" == 600 ]]
for output in "$preflight_result" "$apply_result" "$replay_result"; do
  [[ "$output" == "$review_root"/* && ! -e "$output" ]]
done
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
head=$(git -C "$repo_root" rev-parse HEAD)
[[ "$(jq -er '.required_head_commit' "$manifest")" == "$head" ]]
target_owner=$(jq -er '.owner_user_id' "$manifest")
item_count=$(jq -er '.items|length' "$manifest")
expected_insert=$(jq -er '.expected_insert_rows' "$manifest")
expected_mutated=$(jq -er '.expected_mutated_rows' "$manifest")
[[ "$item_count" -ge 1 && "$item_count" -le 32 ]]
if [[ "$target_owner" == 557ea042-cb82-48f8-9429-472e96c957ef ]]; then
  other_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
else
  other_owner=557ea042-cb82-48f8-9429-472e96c957ef
fi

set -a
source /opt/chat-memory/.env
set +a
[[ -n "${POSTGRES_DSN:-}" && -n "${QDRANT_URL:-}" ]]
authenticated_health

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_${head:0:12}"
status_file="$snapshot_root/memory_v1_v5_2_reviewed_claim_apply_${run_tag}.status"
target_before="$snapshot_root/reviewed_claim_target_before_${run_tag}.tsv"
target_apply="$snapshot_root/reviewed_claim_target_apply_${run_tag}.tsv"
target_replay="$snapshot_root/reviewed_claim_target_replay_${run_tag}.tsv"
other_before="$snapshot_root/reviewed_claim_other_before_${run_tag}.tsv"
other_apply="$snapshot_root/reviewed_claim_other_apply_${run_tag}.tsv"
other_replay="$snapshot_root/reviewed_claim_other_replay_${run_tag}.tsv"
q_target_before="$snapshot_root/reviewed_claim_qdrant_target_before_${run_tag}.jsonl"
q_target_after="$snapshot_root/reviewed_claim_qdrant_target_after_${run_tag}.jsonl"
q_other_before="$snapshot_root/reviewed_claim_qdrant_other_before_${run_tag}.jsonl"
q_other_after="$snapshot_root/reviewed_claim_qdrant_other_after_${run_tag}.jsonl"
backup_partial="$snapshot_root/memory_pre_reviewed_claim_apply_${run_tag}.dump.partial"
backup="${backup_partial%.partial}"
backup_sha="${backup}.sha256"
report="$snapshot_root/memory_v1_v5_2_reviewed_claim_apply_${run_tag}.json"

phase=capture_timer_state
: >"$unit_state"
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" \
    "$(systemctl is-active "$unit")" \
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
units_quiesced=1
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
docker exec "$container" psql -X -A -t -F $'\t' -U sage -d "$database" -c \
  "SELECT table_name,EXISTS(SELECT 1 FROM information_schema.columns AS column_info WHERE column_info.table_schema='memory' AND column_info.table_name=tables.table_name AND column_info.column_name='owner_user_id') FROM information_schema.tables AS tables WHERE table_schema='memory' AND table_type='BASE TABLE' ORDER BY table_name" \
  >"$table_list"
[[ -s "$table_list" ]]
capture_partition target "$target_before"
capture_partition other "$other_before"
capture_qdrant "$q_target_before" "$q_other_before"

phase=backup
docker exec "$container" pg_dump -U sage -d "$database" -Fc \
  --no-owner --no-privileges >"$backup_partial"
mv "$backup_partial" "$backup"
sha256sum "$backup" >"$backup_sha"
chmod 0600 "$backup" "$backup_sha"

phase=zero_write_preflight
MEMORY_V1_REQUIRED_HEAD="$head" PYTHONPATH="$repo_root/scripts:$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$runner" \
  --mode preflight --manifest "$manifest" --output "$preflight_result"
[[ "$(jq -er '.insert_rows' "$preflight_result")" == 0 ]]
[[ "$(jq -er '.mutated_rows' "$preflight_result")" == 0 ]]

phase=transactional_apply
MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_REVIEWED_CLAIM_APPLY=authorized \
PYTHONPATH="$repo_root/scripts:$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$runner" \
  --mode apply --manifest "$manifest" --output "$apply_result"
[[ "$(jq -er '.insert_rows' "$apply_result")" == "$expected_insert" ]]
[[ "$(jq -er '.mutated_rows' "$apply_result")" == "$expected_mutated" ]]
[[ "$(jq -er '.qdrant_writes' "$apply_result")" == 0 ]]
[[ "$(jq -er '.projection_outbox_rows_written' "$apply_result")" == 0 ]]
capture_partition target "$target_apply"
capture_partition other "$other_apply"
verify_target_delta "$target_before" "$target_apply"
cmp -s "$other_before" "$other_apply"

phase=zero_write_replay
MEMORY_V1_REQUIRED_HEAD="$head" PYTHONPATH="$repo_root/scripts:$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$runner" \
  --mode replay --manifest "$manifest" \
  --apply-result "$apply_result" --output "$replay_result"
[[ "$(jq -er '.insert_rows' "$replay_result")" == 0 ]]
[[ "$(jq -er '.mutated_rows' "$replay_result")" == 0 ]]
capture_partition target "$target_replay"
capture_partition other "$other_replay"
cmp -s "$target_apply" "$target_replay"
cmp -s "$other_apply" "$other_replay"

phase=cross_owner_isolation
probe_plan=$(jq -er '.items[0].plan_id' "$manifest")
probe_review=$(jq -er '.items[0].review_id' "$manifest")
psql "$POSTGRES_DSN" -X -q -v ON_ERROR_STOP=1 \
  -v probe_plan="$probe_plan" -v probe_review="$probe_review" \
  -v other_owner="$other_owner" <<'SQL'
BEGIN READ ONLY;
SELECT set_config('app.user_id', :'other_owner', true);
SELECT set_config('test.probe_plan', :'probe_plan', true);
SELECT set_config('test.probe_review', :'probe_review', true);
DO $isolation$
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_projection_apply_v5(
      current_setting('test.probe_plan')::uuid,
      'p01',
      current_setting('test.probe_review')::uuid
    );
    RAISE EXCEPTION 'cross-owner projection apply preflight unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN
    NULL;
  END;
END
$isolation$;
ROLLBACK;
SQL

phase=verify_qdrant
capture_qdrant "$q_target_after" "$q_other_after"
cmp -s "$q_target_before" "$q_target_after"
cmp -s "$q_other_before" "$q_other_after"

phase=restore_runtime
restore_runtime
authenticated_health

phase=write_report
REPORT="$report" BACKUP="$backup" BACKUP_SHA="$backup_sha" \
MANIFEST="$manifest" PREFLIGHT="$preflight_result" APPLY="$apply_result" \
REPLAY="$replay_result" HEAD="$head" python3 - <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path


manifest = json.loads(Path(os.environ["MANIFEST"]).read_text())
apply = json.loads(Path(os.environ["APPLY"]).read_text())
report = {
    "contract_version": "memory_v1_v5_2_reviewed_claim_apply_report_v1",
    "completed_at": datetime.now(timezone.utc).isoformat(),
    "head_commit": os.environ["HEAD"],
    "owner_user_id": manifest["owner_user_id"],
    "manifest_sha256": manifest["manifest_sha256"],
    "backup": os.environ["BACKUP"],
    "backup_sha256_file": os.environ["BACKUP_SHA"],
    "evidence": {
        "preflight": os.environ["PREFLIGHT"],
        "apply": os.environ["APPLY"],
        "replay": os.environ["REPLAY"],
    },
    "verification": {
        "action_counts": manifest["action_counts"],
        "insert_rows": apply["insert_rows"],
        "mutated_rows": apply["mutated_rows"],
        "zero_write_replay": True,
        "account_isolation_verified": True,
        "non_target_database_unchanged": True,
        "qdrant_unchanged": True,
        "projection_outbox_rows_written": 0,
        "retrieval_activated": False,
        "prompt_influence_activated": False,
        "timers_restored_exactly": True,
        "brains_service_restored_exactly": True,
        "service_health_verified": True,
    },
    "hard_stop": "before_qdrant_projection_or_new_retrieval_influence",
}
Path(os.environ["REPORT"]).write_text(
    json.dumps(report, indent=2, sort_keys=True) + "\n"
)
Path(os.environ["REPORT"]).chmod(0o600)
PY

phase=complete
printf 'report=%s\n' "$report"
printf 'backup=%s\n' "$backup"
printf 'manifest_sha256=%s\n' "$(jq -er '.manifest_sha256' "$manifest")"
printf 'insert_rows=%s\n' "$expected_insert"
printf 'mutated_rows=%s\n' "$expected_mutated"
printf '%s\n' 'zero_write_replay=true'
printf '%s\n' 'qdrant_unchanged=true'
printf '%s\n' 'memory_v1_v5_2_reviewed_claim_apply_production: PASS'
