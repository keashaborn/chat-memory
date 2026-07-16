#!/usr/bin/env bash
set -euo pipefail

if [[ "${MEMORY_V1_FREEZE_V4_CAPTURE_INSTALL:-}" != "authorized" ]]; then
  echo "MEMORY_V1_FREEZE_V4_CAPTURE_INSTALL=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
production_repo=/opt/chat-memory
migration=ops/sql/20260716_memory_v1_freeze_v4_capture.sql
rollback=ops/sql/20260716_memory_v1_freeze_v4_capture_rollback.sql
test_sql=tests/memory_v1_freeze_v4_capture.sql
required_ancestor=7cd794deb91473617c66e8aa1e99f589249c5cd7
expected_production_head=7484c4432cd9b920bc20024277776109584b70b7
expected_migration_sha=4c326b0f1c6a06fba265e6c7aeb0d9c86a14b50892d5b15962e9c663a26a18a1
expected_rollback_sha=3dd1f2d096e9c17f0134369e073b7e592124e0f8a835534c31ec2e77b73b8e23
expected_test_sha=964b6adff9ebce33d602083380b8e83172aa7cbd36981c5e939a33fbb737a305
container=brains-postgres-1
database=memory
timer=memory-v1-consolidation.timer
service=memory-v1-consolidation.service
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_freeze_v4_capture.lock
phase=initialization
status_file=
trigger_disabled_by_run=0
timer_disabled_by_run=0

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ -z "$(git -C "$production_repo" status --porcelain)" ]]
[[ "$(git -C "$production_repo" rev-parse HEAD)" == "$expected_production_head" ]]
[[ "$(sha256sum "$repo_root/$migration" | awk '{print $1}')" == "$expected_migration_sha" ]]
[[ "$(sha256sum "$repo_root/$rollback" | awk '{print $1}')" == "$expected_rollback_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | awk '{print $1}')" == "$expected_test_sha" ]]

exec 9>"$lock_file"
flock -n 9 || {
  echo "another Phase 0 V4 freeze holds the lock" >&2
  exit 1
}
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_freeze_v4_capture_${run_id}.status"

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
}

run_sql_file() {
  docker exec -e PGOPTIONS='-c lock_timeout=5s -c statement_timeout=120s' \
    -i "$container" psql -X -v ON_ERROR_STOP=1 -U sage -d "$database" \
    <"$repo_root/$1"
}

record_exit() {
  code=$?
  if [[ "$code" -ne 0 ]]; then
    if [[ "$trigger_disabled_by_run" -eq 1 ]]; then
      phase=automatic_trigger_rollback_after_failure
      run_sql_file "$rollback" \
        >"$snapshot_dir/memory_v1_freeze_v4_capture_rollback_${run_id}.log" \
        2>&1 || true
    fi
    if [[ "$timer_disabled_by_run" -eq 1 ]]; then
      phase=automatic_timer_restore_after_failure
      sudo systemctl enable --now "$timer" >/dev/null 2>&1 || true
    fi
  fi
  printf 'run_id=%s\nphase=%s\nexit_code=%s\ncompleted_at=%s\n' \
    "$run_id" "$phase" "$code" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    >"$status_file"
  chmod 0600 "$status_file"
}
trap record_exit EXIT

capture_state() {
  local output=$1
  : >"$output"
  while IFS=$'\t' read -r schema table; do
    state=$(psql_scalar "
      SELECT count(*)::text || E'\\t' ||
             encode(
               digest(
                 coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
                 'sha256'
               ),
               'hex'
             )
      FROM (
        SELECT to_jsonb(table_row)::text AS row_json
        FROM \"$schema\".\"$table\" AS table_row
      ) rows
    ")
    printf '%s.%s\t%s\n' "$schema" "$table" "$state" >>"$output"
  done < <(psql_scalar "
    SELECT table_schema || E'\\t' || table_name
    FROM information_schema.tables
    WHERE table_type='BASE TABLE'
      AND (
        table_schema='memory'
        OR (table_schema='public' AND table_name='chat_log')
      )
    ORDER BY table_schema,table_name
  ")
  chmod 0600 "$output"
}

qdrant_metadata_signature() {
  {
    curl --fail --silent --show-error \
      http://127.0.0.1:6333/collections/memory_raw
    curl --fail --silent --show-error \
      http://127.0.0.1:6333/collections/memory_raw/snapshots
    curl --fail --silent --show-error \
      http://127.0.0.1:6333/collections/memory_claim_v1
    curl --fail --silent --show-error \
      http://127.0.0.1:6333/collections/memory_claim_v1/snapshots
  } | jq -cS . | sha256sum | awk '{print $1}'
}

phase=preflight
[[ "$(psql_scalar "SELECT tgenabled FROM pg_trigger WHERE tgrelid='public.chat_log'::regclass AND tgname='chat_log_enqueue_memory_v1_consolidation' AND NOT tgisinternal")" == "O" ]]
systemctl is-enabled --quiet "$timer"
systemctl is-active --quiet "$timer"
if systemctl is-active --quiet "$service"; then
  echo "V4 consolidation service is currently running; retry after it exits" >&2
  exit 1
fi

phase=backup
partial="$snapshot_dir/.memory_pre_v4_capture_freeze_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_v4_capture_freeze_${run_id}.dump"
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

phase=baseline
baseline="$snapshot_dir/memory_v1_freeze_v4_capture_baseline_${run_id}.tsv"
post="$snapshot_dir/memory_v1_freeze_v4_capture_post_${run_id}.tsv"
timer_before="$snapshot_dir/memory_v1_freeze_v4_capture_timer_before_${run_id}.txt"
timer_after="$snapshot_dir/memory_v1_freeze_v4_capture_timer_after_${run_id}.txt"
systemctl show "$timer" -p ActiveState -p UnitFileState -p NextElapseUSecRealtime \
  >"$timer_before"
chmod 0600 "$timer_before"
capture_state "$baseline"
qdrant_before=$(qdrant_metadata_signature)
pending_before=$(psql_scalar "SELECT count(*) FROM memory.consolidation_job WHERE status='pending'")
total_before=$(psql_scalar "SELECT count(*) FROM memory.consolidation_job")

phase=trigger_freeze
log="$snapshot_dir/memory_v1_freeze_v4_capture_${run_id}.log"
run_sql_file "$migration" >"$log" 2>&1
trigger_disabled_by_run=1
run_sql_file "$test_sql" >>"$log" 2>&1
chmod 0600 "$log"
[[ "$(psql_scalar "SELECT tgenabled FROM pg_trigger WHERE tgrelid='public.chat_log'::regclass AND tgname='chat_log_enqueue_memory_v1_consolidation' AND NOT tgisinternal")" == "D" ]]
[[ "$(psql_scalar "SELECT tgenabled FROM pg_trigger WHERE tgrelid='public.chat_log'::regclass AND tgname='chat_log_guard_canonical_owner' AND NOT tgisinternal")" == "O" ]]
[[ "$(psql_scalar "SELECT tgenabled FROM pg_trigger WHERE tgrelid='public.chat_log'::regclass AND tgname='chat_log_guard_immutable' AND NOT tgisinternal")" == "O" ]]

phase=timer_freeze
sudo systemctl disable --now "$timer"
timer_disabled_by_run=1
if systemctl is-enabled --quiet "$timer"; then
  echo "V4 consolidation timer remained enabled" >&2
  exit 1
fi
if systemctl is-active --quiet "$timer"; then
  echo "V4 consolidation timer remained active" >&2
  exit 1
fi
if systemctl is-active --quiet "$service"; then
  echo "V4 consolidation service is active after timer freeze" >&2
  exit 1
fi
systemctl show "$timer" -p ActiveState -p UnitFileState -p NextElapseUSecRealtime \
  >"$timer_after"
chmod 0600 "$timer_after"

phase=postflight
capture_state "$post"
cmp -s "$baseline" "$post"
pending_after=$(psql_scalar "SELECT count(*) FROM memory.consolidation_job WHERE status='pending'")
total_after=$(psql_scalar "SELECT count(*) FROM memory.consolidation_job")
[[ "$pending_after" == "$pending_before" ]]
[[ "$total_after" == "$total_before" ]]
qdrant_after=$(qdrant_metadata_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=report
report="$snapshot_dir/memory_v1_freeze_v4_capture_${run_id}.json"
BACKUP="$backup" CATALOG="$catalog" BASELINE="$baseline" POST="$post" \
LOG="$log" REPORT="$report" TIMER_BEFORE="$timer_before" \
TIMER_AFTER="$timer_after" QDRANT_BEFORE="$qdrant_before" \
QDRANT_AFTER="$qdrant_after" PENDING_BEFORE="$pending_before" \
PENDING_AFTER="$pending_after" TOTAL_BEFORE="$total_before" \
TOTAL_AFTER="$total_after" HEAD="$(git -C "$repo_root" rev-parse HEAD)" \
PRODUCTION_HEAD="$expected_production_head" python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

value = {
    "contract_version": "memory_v1_freeze_v4_capture_report_v1",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "phase0_head_commit": os.environ["HEAD"],
    "production_head_commit": os.environ["PRODUCTION_HEAD"],
    "backup": {"path": os.environ["BACKUP"], "catalog": os.environ["CATALOG"]},
    "evidence": {
        "baseline": os.environ["BASELINE"],
        "post": os.environ["POST"],
        "log": os.environ["LOG"],
        "timer_before": os.environ["TIMER_BEFORE"],
        "timer_after": os.environ["TIMER_AFTER"],
        "qdrant_before_sha256": os.environ["QDRANT_BEFORE"],
        "qdrant_after_sha256": os.environ["QDRANT_AFTER"],
    },
    "counts": {
        "pending_jobs_before": int(os.environ["PENDING_BEFORE"]),
        "pending_jobs_after": int(os.environ["PENDING_AFTER"]),
        "total_jobs_before": int(os.environ["TOTAL_BEFORE"]),
        "total_jobs_after": int(os.environ["TOTAL_AFTER"]),
    },
    "checks": {
        "v4_capture_trigger_disabled": True,
        "canonical_owner_trigger_enabled": True,
        "chat_immutability_trigger_enabled": True,
        "chat_insert_rollback_test_passed": True,
        "v4_job_not_created": True,
        "consolidation_timer_disabled": True,
        "consolidation_timer_inactive": True,
        "consolidation_service_inactive": True,
        "postgres_rows_unchanged": True,
        "qdrant_metadata_unchanged": True,
        "model_calls": 0,
    },
}
Path(os.environ["REPORT"]).write_text(
    json.dumps(value, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

trigger_disabled_by_run=0
timer_disabled_by_run=0
phase=complete
printf 'memory_v1_freeze_v4_capture_production_apply: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
