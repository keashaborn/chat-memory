#!/usr/bin/env bash
set -euo pipefail

if [[ "${MEMORY_V1_EVIDENCE_DISPATCHER_SCHEDULER_ACTIVATE:-}" != "authorized" ]]; then
  echo "MEMORY_V1_EVIDENCE_DISPATCHER_SCHEDULER_ACTIVATE=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
dispatcher=scripts/memory_v1_evidence_intake_dispatcher.py
service_unit=ops/systemd/memory-v1-evidence-intake-dispatcher.service
timer_unit=ops/systemd/memory-v1-evidence-intake-dispatcher.timer
service=memory-v1-evidence-intake-dispatcher.service
timer=memory-v1-evidence-intake-dispatcher.timer
required_ancestor=f377791ba6843a8581612dd08a3942a148002068
expected_dispatcher_sha=fb77f1f6bc1eeea80629d2e28a4162cd28109b250422f56a6ef43eecbab35253
expected_service_sha=379695e024bdd6da7f3ee100a97f3fac2f15929c5505bf00825d8453eb5b75fd
expected_timer_sha=6cd93cfa08f8cb6fd15e1c17d9110e3ebcf433b58c48490d505cbc2d18ff4c0d
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_evidence_dispatcher_scheduler.lock
phase=initialization
status_file=
units_installed=0
timer_enabled=0
owners=(
  1240822d-ac9a-4096-95aa-e2b24d36ef50
  557ea042-cb82-48f8-9429-472e96c957ef
  d839b4bc-0bd2-4f2d-aafe-0f3f75883db8
  818b60b9-89bd-442a-998c-fc1924184dfc
  5c9f624a-a66d-4183-babb-b3a0f0f4e733
  673d64a3-c4ba-4d1c-89e3-e0c579022fad
)

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(sha256sum "$repo_root/$dispatcher" | awk '{print $1}')" == "$expected_dispatcher_sha" ]]
[[ "$(sha256sum "$repo_root/$service_unit" | awk '{print $1}')" == "$expected_service_sha" ]]
[[ "$(sha256sum "$repo_root/$timer_unit" | awk '{print $1}')" == "$expected_timer_sha" ]]
systemd-analyze verify \
  "$repo_root/$service_unit" "$repo_root/$timer_unit"

if rg -n '(^|[^a-zA-Z])(OpenAI|responses\.create|chat\.completions)' \
  "$repo_root/$dispatcher"; then
  echo "evidence dispatcher contains an external model caller" >&2
  exit 1
fi
rg -q '^IPAddressDeny=any$' "$repo_root/$service_unit"
rg -q '^IPAddressAllow=localhost$' "$repo_root/$service_unit"

exec 9>"$lock_file"
flock -n 9 || {
  echo "another evidence dispatcher activation holds the lock" >&2
  exit 1
}
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_evidence_dispatcher_scheduler_${run_id}.status"

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
}

record_exit() {
  code=$?
  if [[ "$code" -ne 0 ]]; then
    if [[ "$timer_enabled" -eq 1 ]]; then
      phase=automatic_timer_disable_after_failure
      sudo systemctl disable --now "$timer" >/dev/null 2>&1 || true
    fi
    if [[ "$units_installed" -eq 1 ]]; then
      sudo rm -f \
        "/etc/systemd/system/$service" \
        "/etc/systemd/system/$timer"
      sudo systemctl daemon-reload >/dev/null 2>&1 || true
    fi
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

capture_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    state=$(psql_scalar "
      SELECT count(*)::text || E'\\t' ||
             encode(digest(coalesce(string_agg(row_json,E'\\n'
               ORDER BY row_json),''),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(table_row)::text AS row_json
        FROM memory.\"$table\" AS table_row
      ) rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done < <(psql_scalar "
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name
  ")
  chmod 0600 "$output"
}

owner_args=()
for owner in "${owners[@]}"; do
  owner_args+=(--owner-user-id "$owner")
done

phase=preflight
[[ "$(psql_scalar "SELECT (
  to_regclass('memory.evidence_extraction_job') IS NOT NULL
  AND to_regclass('memory.evidence_extraction_event') IS NOT NULL
  AND to_regprocedure(
    'memory.enqueue_owner_evidence_extraction_v1(uuid,text,text,text,text)'
  ) IS NOT NULL
  AND (SELECT count(*) FROM memory.evidence_extraction_job)=0
  AND (SELECT count(*) FROM memory.evidence_extraction_event)=0
  AND NOT EXISTS (
    SELECT 1
    FROM memory.evidence_intake_terminal
    WHERE outcome='dispatched'
  )
)::int")" == "1" ]]
if systemctl is-active --quiet "$timer"; then
  echo "evidence dispatcher timer is already active" >&2
  exit 1
fi
if systemctl is-enabled --quiet "$timer"; then
  echo "evidence dispatcher timer is already enabled" >&2
  exit 1
fi
[[ ! -e "/etc/systemd/system/$service" ]]
[[ ! -e "/etc/systemd/system/$timer" ]]

set -a
source /opt/chat-memory/.env
set +a
preflight_report="$snapshot_dir/memory_v1_evidence_dispatcher_preflight_${run_id}.json"
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$dispatcher" \
  "${owner_args[@]}" \
  --selector-version 20260716_v1 \
  --limit 100 \
  --report-path "$preflight_report"
chmod 0600 "$preflight_report"
PREFLIGHT_REPORT="$preflight_report" python3 - <<'PY'
import json
import os
from pathlib import Path

report = json.loads(Path(os.environ["PREFLIGHT_REPORT"]).read_text())
assert report["apply"] is False
assert report["model_calls"] == 0
assert len(report["owners"]) == 6
assert sum(owner["before"]["rows"] for owner in report["owners"]) == 0
PY

phase=backup
partial="$snapshot_dir/.memory_pre_evidence_dispatcher_scheduler_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_evidence_dispatcher_scheduler_${run_id}.dump"
catalog="$backup.catalog"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$partial"
[[ -s "$partial" ]]
docker exec -i "$container" pg_restore -l <"$partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$partial" "$backup"
chmod 0600 "$backup" "$catalog"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline_capture
baseline="$snapshot_dir/memory_v1_evidence_dispatcher_baseline_${run_id}.tsv"
post="$snapshot_dir/memory_v1_evidence_dispatcher_post_${run_id}.tsv"
journal="$snapshot_dir/memory_v1_evidence_dispatcher_journal_${run_id}.log"
timer_state="$snapshot_dir/memory_v1_evidence_dispatcher_timer_${run_id}.txt"
security_state="$snapshot_dir/memory_v1_evidence_dispatcher_security_${run_id}.txt"
capture_state "$baseline"
qdrant_before=$(qdrant_signature)
started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

phase=unit_install
sudo install -o root -g root -m 0644 \
  "$repo_root/$service_unit" "/etc/systemd/system/$service"
sudo install -o root -g root -m 0644 \
  "$repo_root/$timer_unit" "/etc/systemd/system/$timer"
units_installed=1
[[ "$(sha256sum "/etc/systemd/system/$service" | awk '{print $1}')" == "$expected_service_sha" ]]
[[ "$(sha256sum "/etc/systemd/system/$timer" | awk '{print $1}')" == "$expected_timer_sha" ]]
sudo systemctl daemon-reload
systemd-analyze verify "$service" "$timer"
systemd-analyze security "$service" --no-pager >"$security_state"
chmod 0600 "$security_state"

phase=manual_cycle
sudo systemctl start "$service"
[[ "$(systemctl show "$service" -p Result --value)" == "success" ]]
[[ "$(systemctl show "$service" -p ExecMainStatus --value)" == "0" ]]
journalctl -u "$service" --since "$started_at" --no-pager -o cat >"$journal"
chmod 0600 "$journal"

JOURNAL="$journal" python3 - <<'PY'
import json
import os
from pathlib import Path

reports = []
for line in Path(os.environ["JOURNAL"]).read_text().splitlines():
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        continue
    if value.get("owners") == 6 and "queued" in value:
        reports.append(value)
assert len(reports) == 1
report = reports[0]
assert report["apply"] is True
assert report["outcomes"] == {}
assert report["queued"] == 0
assert report["terminal_recorded"] == 0
PY

phase=postflight
[[ "$(psql_scalar "SELECT (
  (SELECT count(*) FROM memory.evidence_extraction_job)=0
  AND (SELECT count(*) FROM memory.evidence_extraction_event)=0
  AND NOT EXISTS (
    SELECT 1
    FROM memory.evidence_intake_terminal
    WHERE outcome='dispatched'
  )
)::int")" == "1" ]]
capture_state "$post"
cmp -s "$baseline" "$post"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=timer_enable
sudo systemctl enable --now "$timer"
timer_enabled=1
systemctl is-active --quiet "$timer"
systemctl is-enabled --quiet "$timer"
[[ -n "$(systemctl show "$timer" -p NextElapseUSecMonotonic --value)" ]]
systemctl list-timers "$timer" --no-pager >"$timer_state"
chmod 0600 "$timer_state"

phase=report
report="$snapshot_dir/memory_v1_evidence_dispatcher_scheduler_${run_id}.json"
BACKUP="$backup" CATALOG="$catalog" BASELINE="$baseline" POST="$post" \
JOURNAL="$journal" TIMER_STATE="$timer_state" SECURITY_STATE="$security_state" \
PREFLIGHT_REPORT="$preflight_report" REPORT="$report" \
QDRANT_BEFORE="$qdrant_before" QDRANT_AFTER="$qdrant_after" \
HEAD="$(git -C "$repo_root" rev-parse HEAD)" \
python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

value = {
    "contract_version": "memory_v1_evidence_dispatcher_scheduler_report_v1",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "backup": {"path": os.environ["BACKUP"], "catalog": os.environ["CATALOG"]},
    "preflight_report": os.environ["PREFLIGHT_REPORT"],
    "evidence": {
        "baseline": os.environ["BASELINE"],
        "post": os.environ["POST"],
        "journal": os.environ["JOURNAL"],
        "timer_state": os.environ["TIMER_STATE"],
        "security_state": os.environ["SECURITY_STATE"],
        "qdrant_before_sha256": os.environ["QDRANT_BEFORE"],
        "qdrant_after_sha256": os.environ["QDRANT_AFTER"],
    },
    "checks": {
        "owner_count": 6,
        "manual_dispatch_rows": 0,
        "manual_queue_rows": 0,
        "manual_terminal_rows": 0,
        "all_owner_transaction": True,
        "existing_memory_rows_unchanged": True,
        "qdrant_unchanged": True,
        "timer_enabled": True,
        "timer_interval": "2min",
        "network_loopback_only": True,
        "model_calls": 0,
        "model_consumer_enabled": False,
        "candidate_writes": 0,
        "claim_writes": 0,
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
printf 'memory_v1_evidence_intake_dispatcher_scheduler_activate: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
