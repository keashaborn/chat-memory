#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs owner-filtered read visibility on relational
# stage rows for the restricted local-disposition planner. No table rows are
# written.

if [[ "${MEMORY_V1_V5_LOCAL_DISPOSITION_VISIBILITY_INSTALL:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_LOCAL_DISPOSITION_VISIBILITY_INSTALL=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
staged_packet=2fa0db2a-0636-5353-9f3f-3f952bd4632b
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
migration=ops/sql/20260718_memory_v1_v5_local_packet_disposition_stage_visibility.sql
rollback=ops/sql/20260718_memory_v1_v5_local_packet_disposition_stage_visibility_rollback.sql
sql_test=tests/memory_v1_v5_local_packet_disposition_stage_visibility.sql
worker=scripts/memory_v1_v5_local_packet_disposition.py
lock_file=/home/ubuntu/brains/.memory_v1_v5_local_disposition_visibility.lock

declare -A expected_sha256=(
  ["$migration"]="40969eb59726a5fe7cb8f002972ee8f17fb9021eab2a4b6bde735ec192dc68da"
  ["$rollback"]="795b8e2e19e8ac8ce89586eff5293ac9d863e889cae3ce7154ce199e25bb1b76"
  ["$sql_test"]="1782df44cc614989ba9fad65c7fb3ee39550a0d1bd3b3f2be7e61a7dce023655"
  ["$worker"]="b63a63a5e352484b3ad010e1a849de2ba77043e249adf3cad5c5e4ba4dd09cb6"
)

phase=initialization
run_tag=
status_file=
timers_quiesced=0
migration_installed=0
installation_committed=0
timer_state=$(mktemp /tmp/memory-v1-v5-local-visibility-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-local-visibility-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-local-visibility-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-v5-local-visibility-after.XXXXXX.tsv)
plan=$(mktemp /tmp/memory-v1-v5-local-visibility-plan.XXXXXX.json)
other_plan=$(mktemp /tmp/memory-v1-v5-local-visibility-other.XXXXXX.json)
chmod 0600 "$timer_state" "$table_list" "$before" "$after" "$plan" "$other_plan"

run_sql() {
  docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
    -U sage -d "$database" "$@"
}

scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | tr -d '[:space:]'
}

query_rows() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
}

restore_timers() {
  [[ "$timers_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    if [[ "$enabled" == enabled ]]; then
      sudo -n systemctl enable "$unit" >/dev/null
    else
      [[ "$enabled" == disabled ]]
      sudo -n systemctl disable "$unit" >/dev/null
    fi
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      [[ "$active" == inactive ]]
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$timer_state"
  timers_quiesced=0
}

record_exit() {
  exit_code=$?
  if [[ "$migration_installed" -eq 1 && "$installation_committed" -eq 0 ]]; then
    run_sql <"$rollback" >/dev/null 2>&1 || exit_code=1
  fi
  restore_timers || exit_code=1
  rm -f "$timer_state" "$table_list" "$before" "$after" "$plan" "$other_plan"
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

capture_state() {
  local output=$1 schema table state
  : >"$output"
  while IFS=$'\t' read -r schema table; do
    state=$(scalar "
      SELECT count(*)::text || E'\\t' || encode(public.digest(convert_to(
        coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
        'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM \"$schema\".\"$table\" AS value
      ) AS rows
    ")
    printf '%s\t%s\t%s\n' "$schema" "$table" "$state" >>"$output"
  done <"$table_list"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

for required in "${!expected_sha256[@]}"; do
  [[ -f "$required" ]]
  [[ "$(sha256sum "$required" | awk '{print $1}')" \
      == "${expected_sha256[$required]}" ]]
done
[[ -z "$(git status --porcelain)" ]]
[[ "$(systemctl is-active brains.service)" == active ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_local_visibility_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_local_visibility_${run_tag}.json"

phase=quiesce_timers
: >"$timer_state"
while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ "$(wc -l <"$timer_state")" -eq 7 ]]
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  sudo -n systemctl stop "$unit"
done <"$timer_state"
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
done <"$timer_state"

phase=backup
backup_partial="$snapshot_dir/.memory_pre_v5_local_visibility_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_local_visibility_${run_tag}.dump"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup"
backup_sha=$(sha256sum "$backup" | awk '{print $1}')
printf '%s  %s\n' "$backup_sha" "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline
query_rows "
  SELECT table_schema || E'\\t' || table_name
  FROM information_schema.tables
  WHERE table_type='BASE TABLE' AND table_schema IN ('memory','public')
  ORDER BY table_schema,table_name
" >"$table_list"
capture_state "$before"
qdrant_before=$(qdrant_signature)

phase=install_visibility
run_sql <"$migration" >/dev/null
migration_installed=1
phase=security_test
run_sql \
  -v owner_user_id="$owner" \
  -v other_owner_user_id="$other" \
  -v staged_packet_id="$staged_packet" \
  <"$sql_test" >/dev/null

phase=selector_test
set -a
source .env
set +a
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$owner" >"$plan"
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$other" >"$other_plan"
PLAN="$plan" OTHER_PLAN="$other_plan" python3 - <<'PY'
import json
import os
from pathlib import Path

plan=json.loads(Path(os.environ['PLAN']).read_text())
other=json.loads(Path(os.environ['OTHER_PLAN']).read_text())
assert plan['apply'] is False and plan['plans'][0]['route']=='no_work'
assert all(row['route']=='no_work' for row in other['plans'])
assert 'source_text' not in json.dumps([plan,other])
PY

phase=postflight
capture_state "$after"
cmp -s "$before" "$after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]
phase=restore_timers
restore_timers
installation_committed=1

phase=report
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  '{contract_version:"memory_v1_v5_local_disposition_visibility_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    backup:{path:$backup,sha256:$backup_sha256},
    checks:{owner_filtered_stage_visibility:true,read_only_acl:true,
      staged_packet_excluded:true,cross_owner_excluded:true,
      all_table_rows_unchanged:true,qdrant_unchanged:true,
      timers_restored:true}}' >"$report"
chmod 0600 "$report"
jq -e '.checks|to_entries|map(.value)|all' "$report" >/dev/null
printf '%s\n' "$report"
