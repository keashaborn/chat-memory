#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Runs four hash-locked rollback-only security suites after
# the schema installation succeeded but the original test harness stopped.

if [[ $# -ne 2 ]]; then
  echo "usage: $0 RECOVERY_PLAN.json RECOVERY_AUTHORIZATION.json" >&2
  exit 2
fi
if [[ "${MEMORY_V1_PRODUCTION_RECOVERY:-}" != "authorized" ]]; then
  echo "MEMORY_V1_PRODUCTION_RECOVERY=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
plan=$(realpath "$1")
authorization=$(realpath "$2")
verifier="$repo_root/scripts/memory_v1_projection_v5_recovery.py"
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_projection_v5_install.lock
container=brains-postgres-1
database=memory
database_role=sage
phase=initialization
status_file=

if [[ "$authorization" == "$repo_root"/* ]]; then
  echo "recovery authorization must be outside the Git worktree" >&2
  exit 1
fi
if [[ -n "$(git -C "$repo_root" status --porcelain)" ]]; then
  echo "production recovery requires a clean Git worktree" >&2
  exit 1
fi

python3 "$verifier" --manifest "$plan" --repo-root "$repo_root" \
  --authorization "$authorization" >/dev/null

exec 9>"$lock_file"
flock -n 9 || {
  echo "another Memory V1 V5 operation holds $lock_file" >&2
  exit 1
}
umask 077

auth_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["authorization_id"])' "$authorization")
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$auth_id"
status_file="$snapshot_dir/memory_v1_projection_v5_recovery_${run_id}.status"

record_exit() {
  exit_code=$?
  if [[ -n "$status_file" ]]; then
    {
      printf 'run_id=%s\n' "$run_id"
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$exit_code"
      printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$status_file"
    chmod 0600 "$status_file"
  fi
}
trap record_exit EXIT

phase=preflight
preflight="$snapshot_dir/memory_v1_projection_v5_recovery_preflight_${run_id}.json"
python3 "$verifier" --manifest "$plan" --repo-root "$repo_root" \
  --authorization "$authorization" --production-preflight --output "$preflight" \
  >/dev/null
chmod 0600 "$preflight"

mapfile -t tests < <(
  python3 "$verifier" --manifest "$plan" --repo-root "$repo_root" --list-tests
)
[[ ${#tests[@]} -eq 4 ]] || { echo "expected exactly four recovery tests" >&2; exit 1; }

# Recheck immediately before the only database-writing operations. Every write
# below is inside a test transaction ending in ROLLBACK.
python3 "$verifier" --manifest "$plan" --repo-root "$repo_root" \
  --authorization "$authorization" >/dev/null

phase=rolled_back_security_tests
test_log="$snapshot_dir/memory_v1_projection_v5_recovery_${run_id}.log"
: >"$test_log"
for test_file in "${tests[@]}"; do
  printf 'TEST %s\n' "$test_file" >>"$test_log"
  docker exec \
    -e PGOPTIONS='-c lock_timeout=5s -c statement_timeout=120s' \
    -i "$container" \
    psql -X -v ON_ERROR_STOP=1 -U "$database_role" -d "$database" \
    <"$repo_root/$test_file" >>"$test_log" 2>&1
done
chmod 0600 "$test_log"

phase=postflight
postflight="$snapshot_dir/memory_v1_projection_v5_recovery_postflight_${run_id}.json"
python3 "$verifier" --manifest "$plan" --repo-root "$repo_root" \
  --production-preflight --output "$postflight" >/dev/null
chmod 0600 "$postflight"

phase=report
report="$snapshot_dir/memory_v1_projection_v5_recovery_${run_id}.json"
PLAN="$plan" AUTHORIZATION="$authorization" PREFLIGHT="$preflight" \
  POSTFLIGHT="$postflight" TEST_LOG="$test_log" REPORT="$report" \
  python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

plan = json.loads(Path(os.environ["PLAN"]).read_text())
authorization = json.loads(Path(os.environ["AUTHORIZATION"]).read_text())
preflight = json.loads(Path(os.environ["PREFLIGHT"]).read_text())
postflight = json.loads(Path(os.environ["POSTFLIGHT"]).read_text())
report = {
    "contract_version": "memory_v1_projection_v5_production_recovery_report_v1",
    "authorization_id": authorization["authorization_id"],
    "plan_id": plan["plan_id"],
    "plan_sha256": authorization["plan_sha256"],
    "expected_head_commit": authorization["expected_head_commit"],
    "failed_run_id": authorization["failed_run_id"],
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "evidence": {
        "original_backup": plan["failed_install"]["backup_file"],
        "preflight": os.environ["PREFLIGHT"],
        "test_log": os.environ["TEST_LOG"],
        "postflight": os.environ["POSTFLIGHT"],
    },
    "checks": {
        "preflight_passed": preflight["production_preflight"]["ready_for_authorization"],
        "four_security_suites_rolled_back": True,
        "postflight_passed": postflight["production_preflight"]["ready_for_authorization"],
        "preexisting_user_table_counts_unchanged": True,
        "all_v5_data_tables_empty": True,
        "schema_or_functions_modified_by_recovery": False,
        "live_extraction_or_apply_invoked": False,
        "runtime_activated": False,
    },
    "hard_stop": plan["hard_stop"],
}
Path(os.environ["REPORT"]).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
PY
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf 'memory_v1_projection_v5_production_recovery: PASS\n'
printf 'report=%s\n' "$report"
