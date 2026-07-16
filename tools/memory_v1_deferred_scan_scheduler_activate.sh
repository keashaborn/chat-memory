#!/usr/bin/env bash
set -euo pipefail

if [[ "${MEMORY_V1_DEFERRED_SCAN_SCHEDULER_ACTIVATE:-}" != "authorized" ]]; then
  echo "MEMORY_V1_DEFERRED_SCAN_SCHEDULER_ACTIVATE=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
worker=scripts/memory_v1_deferred_reconciliation_scan_worker.py
roster=ops/config/memory_v1_deferred_scanner_owner_roster_v1.json
service_unit=ops/systemd/memory-v1-deferred-reconciliation-scan.service
timer_unit=ops/systemd/memory-v1-deferred-reconciliation-scan.timer
service=memory-v1-deferred-reconciliation-scan.service
timer=memory-v1-deferred-reconciliation-scan.timer
source_ancestor=4fae1bc73109b1a1c53ed848da9de6342944d528
runtime_ancestor=2a188dd2b5dbbb74689a8f7cf70f43988eecb332
expected_worker_sha=688874f6a45977fcc1bd140acc534562dd2ce4c028cc10f9d0d94ca7d6b87d6c
expected_roster_sha=c690823ff1a6311b76c52a6d47670a7741b6b8452b5729448a5a5c804fab100c
expected_service_sha=25fb48b554d9ba19c234bd57fd45201b5d546e516327dc3e59f5796654e43447
expected_timer_sha=e67bf1b5dff51edeea37f1da92caa5645a216a5e9e82fce4013c3bc614810860
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_deferred_scan_scheduler_activate.lock
phase=initialization
status_file=
timer_enabled_by_run=0

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
if ! git -C "$repo_root" merge-base --is-ancestor "$source_ancestor" HEAD \
   && ! git -C "$repo_root" merge-base --is-ancestor "$runtime_ancestor" HEAD; then
  echo "scheduler source commit is not an ancestor" >&2
  exit 1
fi
[[ "$(sha256sum "$repo_root/$worker" | awk '{print $1}')" == "$expected_worker_sha" ]]
[[ "$(sha256sum "$repo_root/$roster" | awk '{print $1}')" == "$expected_roster_sha" ]]
[[ "$(sha256sum "$repo_root/$service_unit" | awk '{print $1}')" == "$expected_service_sha" ]]
[[ "$(sha256sum "$repo_root/$timer_unit" | awk '{print $1}')" == "$expected_timer_sha" ]]

mapfile -t owners < <(jq -er '.owners[]' "$repo_root/$roster")
[[ "${#owners[@]}" -eq 6 ]]

exec 9>"$lock_file"
flock -n 9 || {
  echo "another deferred scan scheduler activation holds the lock" >&2
  exit 1
}
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_deferred_scan_scheduler_${run_id}.status"

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
}

record_exit() {
  code=$?
  if [[ "$code" -ne 0 && "$timer_enabled_by_run" -eq 1 ]]; then
    phase=automatic_timer_disable_after_failure
    sudo systemctl disable --now "$timer" >/dev/null 2>&1 || true
  fi
  printf 'run_id=%s\nphase=%s\nexit_code=%s\ncompleted_at=%s\n' \
    "$run_id" "$phase" "$code" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    >"$status_file"
  chmod 0600 "$status_file"
}
trap record_exit EXIT

qdrant_signature() {
  curl --fail --silent --show-error \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id)' \
    | sha256sum | awk '{print $1}'
}

capture_existing_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    state=$(psql_scalar "
      SELECT count(*)::text || E'\\t' ||
             encode(digest(coalesce(string_agg(row_json,E'\\n'
               ORDER BY row_json),''),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(table_row)::text row_json
        FROM memory.\"$table\" AS table_row
      ) rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done < <(psql_scalar "
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema='memory'
      AND table_type='BASE TABLE'
      AND table_name<>'deferred_reconciliation_scan_run_v5'
    ORDER BY table_name
  ")
  chmod 0600 "$output"
}

owner_scan_count() {
  local actor=$1
  docker exec -i "$container" psql -X -q -A -t \
    -v ON_ERROR_STOP=1 -U sage -d "$database" -v actor="$actor" <<'SQL'
BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id', :'actor', true) \gset
SELECT count(*) FROM memory.scan_deferred_entailment_reconciliation_v5(25);
ROLLBACK;
RESET SESSION AUTHORIZATION;
SQL
}

phase=preflight
[[ "$(psql_scalar "SELECT (
  to_regclass('memory.deferred_reconciliation_scan_run_v5') IS NOT NULL
  AND to_regprocedure(
    'memory.run_deferred_reconciliation_scan_v5(uuid,integer,text,text)'
  ) IS NOT NULL
)::int")" == "1" ]]
[[ "$(psql_scalar "SELECT count(*)
  FROM memory.deferred_reconciliation_scan_run_v5")" == "0" ]]
if systemctl is-active --quiet "$timer"; then
  echo "deferred scan timer is already active" >&2
  exit 1
fi
if systemctl is-enabled --quiet "$timer"; then
  echo "deferred scan timer is already enabled" >&2
  exit 1
fi
for owner in "${owners[@]}"; do
  [[ "$(owner_scan_count "$owner")" == "0" ]]
done

phase=baseline_capture
baseline="$snapshot_dir/memory_v1_deferred_scan_scheduler_baseline_${run_id}.tsv"
post="$snapshot_dir/memory_v1_deferred_scan_scheduler_post_${run_id}.tsv"
journal="$snapshot_dir/memory_v1_deferred_scan_scheduler_journal_${run_id}.log"
timer_state="$snapshot_dir/memory_v1_deferred_scan_scheduler_timer_${run_id}.txt"
capture_existing_state "$baseline"
qdrant_before=$(qdrant_signature)
started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

phase=unit_install
sudo install -o root -g root -m 0644 \
  "$repo_root/$service_unit" "/etc/systemd/system/$service"
sudo install -o root -g root -m 0644 \
  "$repo_root/$timer_unit" "/etc/systemd/system/$timer"
sudo systemctl daemon-reload

phase=manual_cycle
sudo systemctl start "$service"
[[ "$(systemctl show "$service" -p Result --value)" == "success" ]]
[[ "$(systemctl show "$service" -p ExecMainStatus --value)" == "0" ]]
journalctl -u "$service" --since "$started_at" --no-pager -o cat >"$journal"
chmod 0600 "$journal"

JOURNAL="$journal" EXPECTED_ROSTER_SHA="$expected_roster_sha" \
python3 - <<'PY'
import json
import os
from pathlib import Path

values = []
for line in Path(os.environ["JOURNAL"]).read_text().splitlines():
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        continue
    if value.get("contract_version") == "memory_v1_deferred_scan_worker_report_v1":
        values.append(value)
if len(values) != 1:
    raise SystemExit("expected exactly one structured worker report")
value = values[0]
if value["owner_roster_sha256"] != os.environ["EXPECTED_ROSTER_SHA"]:
    raise SystemExit("worker roster hash mismatch")
if value["owner_count"] != 6 or len(value["owners"]) != 6:
    raise SystemExit("worker owner count mismatch")
if value["candidate_count"] != 0 or value["automatic_apply"] is not False:
    raise SystemExit("manual cycle was not zero-work report-only")
if any(owner["candidate_count"] != 0 for owner in value["owners"].values()):
    raise SystemExit("an owner had unexpected reconciliation candidates")
PY

phase=postflight
[[ "$(psql_scalar "SELECT (
  count(*)=6
  AND count(DISTINCT owner_user_id)=6
  AND count(*) FILTER (
    WHERE owner_roster_sha256='$expected_roster_sha'
      AND candidate_count=0
      AND scanner_version='memory_v1_deferred_reconciliation_scanner_v5'
  )=6
  AND count(*) FILTER (
    WHERE owner_user_id NOT IN (
      '1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid,
      '557ea042-cb82-48f8-9429-472e96c957ef'::uuid,
      '5c9f624a-a66d-4183-babb-b3a0f0f4e733'::uuid,
      '673d64a3-c4ba-4d1c-89e3-e0c579022fad'::uuid,
      '818b60b9-89bd-442a-998c-fc1924184dfc'::uuid,
      'd839b4bc-0bd2-4f2d-aafe-0f3f75883db8'::uuid
    )
  )=0
)::int
FROM memory.deferred_reconciliation_scan_run_v5")" == "1" ]]
capture_existing_state "$post"
cmp -s "$baseline" "$post"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=timer_enable
sudo systemctl enable --now "$timer"
timer_enabled_by_run=1
systemctl is-active --quiet "$timer"
systemctl is-enabled --quiet "$timer"
[[ -n "$(systemctl show "$timer" -p NextElapseUSecMonotonic --value)" ]]
systemctl list-timers "$timer" --no-pager >"$timer_state"
chmod 0600 "$timer_state"
[[ "$(psql_scalar "SELECT count(*)
  FROM memory.deferred_reconciliation_scan_run_v5")" == "6" ]]

phase=report
report="$snapshot_dir/memory_v1_deferred_scan_scheduler_${run_id}.json"
BASELINE="$baseline" POST="$post" JOURNAL="$journal" TIMER_STATE="$timer_state" \
REPORT="$report" QDRANT_BEFORE="$qdrant_before" QDRANT_AFTER="$qdrant_after" \
HEAD="$(git -C "$repo_root" rev-parse HEAD)" ROSTER_SHA="$expected_roster_sha" \
python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

value = {
    "contract_version": "memory_v1_deferred_scan_scheduler_activation_report_v1",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "owner_roster_sha256": os.environ["ROSTER_SHA"],
    "evidence": {
        "baseline": os.environ["BASELINE"],
        "post": os.environ["POST"],
        "journal": os.environ["JOURNAL"],
        "timer_state": os.environ["TIMER_STATE"],
        "qdrant_before_sha256": os.environ["QDRANT_BEFORE"],
        "qdrant_after_sha256": os.environ["QDRANT_AFTER"],
    },
    "checks": {
        "owner_count": 6,
        "manual_audit_rows": 6,
        "manual_candidate_count": 0,
        "one_owner_per_transaction": True,
        "preexisting_memory_rows_unchanged": True,
        "qdrant_unchanged": True,
        "timer_enabled": True,
        "automatic_reconciliation": False,
        "retrieval_activation": False,
        "prompt_influence": False,
    },
}
Path(os.environ["REPORT"]).write_text(
    json.dumps(value, indent=2, sort_keys=True) + "\n"
)
PY
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
phase=complete
printf 'memory_v1_deferred_scan_scheduler_activate: PASS\n'
printf 'report=%s\n' "$report"
