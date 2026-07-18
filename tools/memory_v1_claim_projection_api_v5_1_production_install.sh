#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs additive owner-scoped V5.1 claim projection
# functions, runs rollback-only live security tests, and proves no Memory rows
# or Qdrant points changed. It does not persist entailments or projection plans.

if [[ "${MEMORY_V1_CLAIM_PROJECTION_V5_1_INSTALL:-}" != authorized ]]; then
  echo 'MEMORY_V1_CLAIM_PROJECTION_V5_1_INSTALL=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
required_ancestor=1980a86b3500
migration=ops/sql/20260718_memory_v1_claim_projection_api_v5_1.sql
expected_migration_sha=bdd4e13c964dea74a0576dafc918e8cb87e149ab20e2bdc91bf9da9efd3b62e8
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_claim_projection_v5_1_install.lock
phase=initialization
status_file=
units_quiesced=0
unit_state_before=
timers=(
  memory-v1-consolidation.timer
  memory-v1-deferred-reconciliation-scan.timer
  memory-v1-evidence-intake-dispatcher.timer
  memory-v1-governance.timer
  memory-v1-projection.timer
  memory-v1-v5-chat-capture.timer
)

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(sha256sum "$repo_root/$migration" | awk '{print $1}')" == \
  "$expected_migration_sha" ]]

mkdir -p "$snapshot_dir"
exec 9>"$lock_file"
flock -n 9
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_claim_projection_v5_1_install_${run_id}.status"
unit_state_before="$snapshot_dir/memory_v1_claim_projection_v5_1_units_before_${run_id}.tsv"

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
}

restore_units() {
  if [[ "$units_quiesced" -ne 1 || ! -s "$unit_state_before" ]]; then
    return 0
  fi
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$unit_state_before"
  units_quiesced=0
}

record_exit() {
  code=$?
  if [[ "$units_quiesced" -eq 1 ]]; then
    restore_units || code=1
  fi
  {
    printf 'run_id=%s\n' "$run_id"
    printf 'phase=%s\n' "$phase"
    printf 'exit_code=%s\n' "$code"
    printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"$status_file"
  chmod 0600 "$status_file"
  exit "$code"
}
trap record_exit EXIT

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

capture_memory_state() {
  local output=$1 table state
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    state=$(psql_scalar "
      SELECT count(*)::text || E'\\t' ||
             encode(public.digest(convert_to(coalesce(string_agg(
               row_json,E'\\n' ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(table_row)::text AS row_json
        FROM memory.\"$table\" AS table_row
      ) rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done < <(psql_scalar "
    SELECT table_name FROM information_schema.tables
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name
  ")
  chmod 0600 "$output"
}

phase=preflight
[[ "$(psql_scalar "SELECT (
  to_regprocedure('memory.v5_claim_literal_value_v5_1(text,jsonb)') IS NULL
  AND to_regprocedure('memory.render_claim_projection_text_v5_1(text,text,text,text,text,text,jsonb)') IS NULL
  AND to_regprocedure('memory.preflight_claim_projection_source_v5_1(uuid)') IS NULL
  AND to_regprocedure('memory.preflight_claim_projection_packet_v5_1(uuid,text)') IS NULL
  AND to_regprocedure('memory.stage_claim_projection_plan_v5_1(uuid,text,text)') IS NULL
)::integer")" == 1 ]]

phase=capture_timer_state
: >"$unit_state_before"
for unit in "${timers[@]}"; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$unit_state_before"
done
chmod 0600 "$unit_state_before"

phase=quiesce_timers
while IFS=$'\t' read -r unit _enabled active; do
  if [[ "$active" == active ]]; then
    sudo -n systemctl stop "$unit"
  fi
done <"$unit_state_before"
units_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
  [[ "$(systemctl is-active "$unit")" == inactive ]]
done <"$unit_state_before"

phase=baseline_capture
baseline="$snapshot_dir/memory_v1_claim_projection_v5_1_before_${run_id}.tsv"
post="$snapshot_dir/memory_v1_claim_projection_v5_1_after_${run_id}.tsv"
capture_memory_state "$baseline"
qdrant_before=$(qdrant_signature)

phase=backup
backup_partial="$snapshot_dir/.memory_pre_claim_projection_v5_1_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_claim_projection_v5_1_${run_id}.dump"
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

phase=schema_install
install_log="$snapshot_dir/memory_v1_claim_projection_v5_1_install_${run_id}.log"
docker exec -e PGOPTIONS='-c lock_timeout=5s -c statement_timeout=180s' \
  -i "$container" psql -X -v ON_ERROR_STOP=1 -U sage -d "$database" \
  <"$repo_root/$migration" >"$install_log" 2>&1
chmod 0600 "$install_log"

phase=rolled_back_functional_security_test
set -a
source "$repo_root/.env"
set +a
[[ -n "${POSTGRES_DSN:-}" ]]
PYTHONPATH="$repo_root/scripts" /opt/chat-memory/venv/bin/python \
  "$repo_root/scripts/memory_v1_v5_claim_projection_preflight_test.py" \
  >>"$install_log" 2>&1
PYTHONPATH="$repo_root/scripts" /opt/chat-memory/venv/bin/python \
  "$repo_root/scripts/memory_v1_v5_claim_projection_security_probe.py" \
  >>"$install_log" 2>&1

phase=postflight
[[ "$(psql_scalar "SELECT (
  (SELECT count(*) FROM pg_proc WHERE oid IN (
    'memory.preflight_claim_projection_source_v5_1(uuid)'::regprocedure,
    'memory.preflight_claim_projection_packet_v5_1(uuid,text)'::regprocedure,
    'memory.stage_claim_projection_plan_v5_1(uuid,text,text)'::regprocedure
  ) AND prosecdef AND proowner='memory_v5_writer'::regrole
    AND proconfig @> ARRAY['search_path=\"\"']::text[])=3
  AND has_function_privilege('brains_app','memory.preflight_claim_projection_source_v5_1(uuid)','EXECUTE')
  AND has_function_privilege('brains_app','memory.preflight_claim_projection_packet_v5_1(uuid,text)','EXECUTE')
  AND has_function_privilege('brains_app','memory.stage_claim_projection_plan_v5_1(uuid,text,text)','EXECUTE')
  AND NOT has_function_privilege('public','memory.preflight_claim_projection_source_v5_1(uuid)','EXECUTE')
  AND NOT has_function_privilege('public','memory.preflight_claim_projection_packet_v5_1(uuid,text)','EXECUTE')
  AND NOT has_function_privilege('public','memory.stage_claim_projection_plan_v5_1(uuid,text,text)','EXECUTE')
  AND NOT has_function_privilege('brains_app','memory.render_claim_projection_text_v5_1(text,text,text,text,text,text,jsonb)','EXECUTE')
)::integer")" == 1 ]]
capture_memory_state "$post"
cmp -s "$baseline" "$post"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=restore_timer_state
restore_units
unit_state_after="$snapshot_dir/memory_v1_claim_projection_v5_1_units_after_${run_id}.tsv"
: >"$unit_state_after"
for unit in "${timers[@]}"; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$unit_state_after"
done
chmod 0600 "$unit_state_after"
cmp -s "$unit_state_before" "$unit_state_after"

phase=report
report="$snapshot_dir/memory_v1_claim_projection_v5_1_install_${run_id}.json"
REPORT="$report" BACKUP="$backup" CATALOG="$catalog" LOG="$install_log" \
BASELINE="$baseline" POST="$post" UNIT_BEFORE="$unit_state_before" \
UNIT_AFTER="$unit_state_after" QDRANT_BEFORE="$qdrant_before" \
QDRANT_AFTER="$qdrant_after" HEAD="$(git -C "$repo_root" rev-parse HEAD)" \
MIGRATION_SHA="$expected_migration_sha" python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

report = {
    "contract_version": "memory_v1_claim_projection_v5_1_install_report_v1",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "migration_sha256": os.environ["MIGRATION_SHA"],
    "backup": {"path": os.environ["BACKUP"], "catalog": os.environ["CATALOG"]},
    "evidence": {
        "install_log": os.environ["LOG"],
        "memory_state_before": os.environ["BASELINE"],
        "memory_state_after": os.environ["POST"],
        "timer_state_before": os.environ["UNIT_BEFORE"],
        "timer_state_after": os.environ["UNIT_AFTER"],
    },
    "verification": {
        "additive_functions_installed": True,
        "rollback_only_security_probe": "pass",
        "memory_rows_unchanged": True,
        "qdrant_before_sha256": os.environ["QDRANT_BEFORE"],
        "qdrant_after_sha256": os.environ["QDRANT_AFTER"],
        "timers_restored_exactly": True,
        "durable_entailments_written": 0,
        "durable_projection_plans_written": 0,
        "claims_written": 0,
        "retrieval_activated": False,
        "prompt_influence_activated": False,
    },
}
path = Path(os.environ["REPORT"])
path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
path.chmod(0o600)
PY

phase=complete
printf 'backup=%s\nreport=%s\n' "$backup" "$report"
printf 'memory_v1_claim_projection_api_v5_1_production_install: PASS\n'
