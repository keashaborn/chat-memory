#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Runs four hash-locked production rollback suites after the ACL
# recovery and proves zero persistent row/vector changes.

if [[ $# -ne 2 ]]; then
  echo "usage: $0 VERIFICATION_PLAN.json AUTHORIZATION.json" >&2
  exit 2
fi
if [[ "${MEMORY_V1_COMPONENT_PROJECTION_ACL_VERIFY:-}" != "authorized" ]]; then
  echo "MEMORY_V1_COMPONENT_PROJECTION_ACL_VERIFY=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
plan=$(realpath "$1")
authorization=$(realpath "$2")
expected_plan="$repo_root/ops/manifests/memory_v1_v5_component_projection_acl_verification_plan_20260717.json"
verifier="$repo_root/scripts/memory_v1_v5_component_projection_acl_recovery_plan.py"
container=brains-postgres-1
database=memory
database_role=sage
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_component_projection_acl_verify.lock
phase=initialization
status_file=
units_quiesced=0
unit_state_before=

[[ "$plan" == "$expected_plan" ]] || {
  echo "verification plan must be the committed canonical plan" >&2
  exit 1
}
[[ "$authorization" != "$repo_root"/* ]] || {
  echo "authorization must be outside the Git worktree" >&2
  exit 1
}
[[ -z "$(git -C "$repo_root" status --porcelain)" ]] || {
  echo "verification requires a clean Git worktree" >&2
  exit 1
}
python3 "$verifier" --manifest "$plan" --repo-root "$repo_root" \
  --authorization "$authorization" >/dev/null

exec 9>"$lock_file"
flock -n 9 || { echo "another ACL verification holds $lock_file" >&2; exit 1; }
umask 077
auth_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["authorization_id"])' "$authorization")
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$auth_id"
status_file="$snapshot_dir/memory_v1_v5_component_projection_acl_verify_${run_id}.status"
unit_state_before="$snapshot_dir/memory_v1_v5_component_projection_acl_verify_units_before_${run_id}.tsv"

restore_units() {
  if [[ "$units_quiesced" -ne 1 || ! -s "$unit_state_before" ]]; then
    return 0
  fi
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]] || return 1
    if [[ "$active" == "active" ]]; then
      sudo systemctl start "$unit"
    else
      sudo systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$unit_state_before"
  units_quiesced=0
}

record_exit() {
  code=$?
  if [[ "$units_quiesced" -eq 1 ]]; then
    saved_phase=$phase
    phase=restore_timer_state_after_failure
    restore_units || true
    phase=$saved_phase
  fi
  if [[ -n "$status_file" ]]; then
    {
      printf 'run_id=%s\n' "$run_id"
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$code"
      printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$status_file"
    chmod 0600 "$status_file"
  fi
}
trap record_exit EXIT

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U "$database_role" -d "$database" -c "$1"
}

run_sql_file() {
  local file=$1
  docker exec \
    -e PGOPTIONS='-c lock_timeout=5s -c statement_timeout=180s' \
    -i "$container" psql -X -v ON_ERROR_STOP=1 \
    -U "$database_role" -d "$database" <"$repo_root/$file"
}

qdrant_signature() {
  curl --fail --silent --show-error \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id)' \
    | sha256sum | awk '{print $1}'
}

capture_memory_state() {
  local table_list=$1
  local output=$2
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]] || return 1
    state=$(psql_scalar "
      SELECT count(*)::text || E'\\t' ||
             encode(public.digest(coalesce(string_agg(row_json,E'\\n'
               ORDER BY row_json),''),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(table_row)::text AS row_json
        FROM memory.\"$table\" AS table_row
      ) AS rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

phase=preflight
preflight="$snapshot_dir/memory_v1_v5_component_projection_acl_verify_preflight_${run_id}.json"
python3 "$verifier" --manifest "$plan" --repo-root "$repo_root" \
  --authorization "$authorization" --production-preflight --output "$preflight" \
  >/dev/null
chmod 0600 "$preflight"
mapfile -t tests < <(
  python3 "$verifier" --manifest "$plan" --repo-root "$repo_root" --list-kind rolled_back_test
)
[[ ${#tests[@]} -eq 4 ]] || { echo "expected four production rollback tests" >&2; exit 1; }

phase=capture_timer_state
: >"$unit_state_before"
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]] || exit 1
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$unit_state_before"
done < <(python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1]))["maintenance_quiescence"]["units"]))' "$plan")
chmod 0600 "$unit_state_before"

phase=quiesce_timers
while IFS=$'\t' read -r unit _enabled _active; do
  sudo systemctl stop "$unit"
done <"$unit_state_before"
units_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  systemctl is-active --quiet "$service" && {
    echo "$service did not quiesce within 30 seconds" >&2
    exit 1
  }
  [[ "$(systemctl is-active "$unit")" == "inactive" ]]
done <"$unit_state_before"

phase=baseline_capture
tables="$snapshot_dir/memory_v1_v5_component_projection_acl_verify_tables_${run_id}.txt"
before="$snapshot_dir/memory_v1_v5_component_projection_acl_verify_before_${run_id}.tsv"
after="$snapshot_dir/memory_v1_v5_component_projection_acl_verify_after_${run_id}.tsv"
qdrant_before_file="$snapshot_dir/memory_v1_v5_component_projection_acl_verify_qdrant_before_${run_id}.sha256"
qdrant_after_file="$snapshot_dir/memory_v1_v5_component_projection_acl_verify_qdrant_after_${run_id}.sha256"
psql_scalar "SELECT table_name FROM information_schema.tables WHERE table_schema='memory' AND table_type='BASE TABLE' ORDER BY table_name" >"$tables"
chmod 0600 "$tables"
capture_memory_state "$tables" "$before"
qdrant_signature >"$qdrant_before_file"
chmod 0600 "$qdrant_before_file"

phase=backup
backup_partial="$snapshot_dir/.memory_pre_v5_component_projection_acl_verify_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_component_projection_acl_verify_${run_id}.dump"
catalog="$backup.catalog"
checksum="$backup.sha256"
docker exec "$container" pg_dump -U "$database_role" -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$catalog"
sha256sum "$backup" >"$checksum"
chmod 0600 "$checksum"

# Revalidate the short-lived authorization immediately before transient tests.
python3 "$verifier" --manifest "$plan" --repo-root "$repo_root" \
  --authorization "$authorization" >/dev/null

phase=rolled_back_security_tests
test_log="$snapshot_dir/memory_v1_v5_component_projection_acl_verify_${run_id}.log"
: >"$test_log"
for test_file in "${tests[@]}"; do
  printf 'TEST %s\n' "$test_file" >>"$test_log"
  run_sql_file "$test_file" >>"$test_log" 2>&1
done
chmod 0600 "$test_log"

phase=postflight
capture_memory_state "$tables" "$after"
cmp -s "$before" "$after" || {
  diff -u "$before" "$after" >&2 || true
  echo "memory rows changed during rollback-only verification" >&2
  exit 1
}
qdrant_signature >"$qdrant_after_file"
chmod 0600 "$qdrant_after_file"
cmp -s "$qdrant_before_file" "$qdrant_after_file" || {
  echo "Qdrant changed during rollback-only verification" >&2
  exit 1
}
postflight_ok=$(psql_scalar "
  SELECT (
    has_function_privilege('memory_v5_extraction_maintainer','memory.v5_project_scope_valid(jsonb)','EXECUTE')
    AND NOT has_function_privilege('brains_app','memory.v5_project_scope_valid(jsonb)','EXECUTE')
    AND (SELECT count(*)=0 FROM memory.project_component_v5)
    AND (SELECT count(*)=0 FROM memory.project_component_alias_v5)
    AND (SELECT count(*)=0 FROM memory.project_component_registration_event_v5)
    AND (SELECT count(*)=0 FROM memory.project_knowledge_head_v5 WHERE component_key IS NOT NULL)
    AND (SELECT count(*)=0 FROM memory.projection_project_payload WHERE component_key IS NOT NULL)
  )::integer
")
[[ "$postflight_ok" == "1" ]] || { echo "ACL verification postflight failed" >&2; exit 1; }

phase=restore_timer_state
restore_units
unit_state_after="$snapshot_dir/memory_v1_v5_component_projection_acl_verify_units_after_${run_id}.tsv"
: >"$unit_state_after"
while IFS=$'\t' read -r unit _enabled _active; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$unit_state_after"
done <"$unit_state_before"
chmod 0600 "$unit_state_after"
cmp -s "$unit_state_before" "$unit_state_after" || {
  echo "timer state was not restored exactly" >&2
  exit 1
}

phase=report
report="$snapshot_dir/memory_v1_v5_component_projection_acl_verify_${run_id}.json"
PLAN="$plan" AUTHORIZATION="$authorization" BACKUP="$backup" \
  PREFLIGHT="$preflight" TEST_LOG="$test_log" BEFORE="$before" AFTER="$after" \
  QDRANT_BEFORE="$qdrant_before_file" QDRANT_AFTER="$qdrant_after_file" \
  UNIT_BEFORE="$unit_state_before" UNIT_AFTER="$unit_state_after" REPORT="$report" \
  python3 - <<'PY'
import datetime as dt
import hashlib
import json
import os
from pathlib import Path

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

plan=json.loads(Path(os.environ["PLAN"]).read_text())
auth=json.loads(Path(os.environ["AUTHORIZATION"]).read_text())
report={
  "contract_version":"memory_v1_v5_component_projection_acl_verification_report_v1",
  "completed_at":dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00","Z"),
  "plan_id":plan["plan_id"],
  "authorization_id":auth["authorization_id"],
  "expected_head_commit":auth["expected_head_commit"],
  "backup":os.environ["BACKUP"],
  "backup_sha256":sha(os.environ["BACKUP"]),
  "preflight_sha256":sha(os.environ["PREFLIGHT"]),
  "test_log_sha256":sha(os.environ["TEST_LOG"]),
  "memory_before_sha256":sha(os.environ["BEFORE"]),
  "memory_after_sha256":sha(os.environ["AFTER"]),
  "qdrant_before":Path(os.environ["QDRANT_BEFORE"]).read_text().strip(),
  "qdrant_after":Path(os.environ["QDRANT_AFTER"]).read_text().strip(),
  "timer_before_sha256":sha(os.environ["UNIT_BEFORE"]),
  "timer_after_sha256":sha(os.environ["UNIT_AFTER"]),
  "rollback_suites_passed":4,
  "persistent_database_writes":0,
  "component_rows_created":0,
  "qdrant_changed":False,
  "runtime_activated":False,
  "hard_stop":plan["hard_stop"],
}
Path(os.environ["REPORT"]).write_text(json.dumps(report,indent=2,sort_keys=True)+"\n")
PY
chmod 0600 "$report"

phase=complete
printf 'component_projection_acl_verification=PASS\n'
printf 'report=%s\n' "$report"
printf 'backup=%s\n' "$backup"
printf 'hard_stop=%s\n' "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["hard_stop"])' "$plan")"
