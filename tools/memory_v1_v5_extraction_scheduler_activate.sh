#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the bounded V5 extraction units, executes one
# canary, verifies the write boundary, then enables the timer.

if [[ "${MEMORY_V1_V5_EXTRACTION_SCHEDULER_ACTIVATE:-}" != "authorized" ]]; then
  echo 'MEMORY_V1_V5_EXTRACTION_SCHEDULER_ACTIVATE=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
service_source=ops/systemd/memory-v1-v5-bounded-extraction.service
timer_source=ops/systemd/memory-v1-v5-bounded-extraction.timer
service_unit=memory-v1-v5-bounded-extraction.service
timer_unit=memory-v1-v5-bounded-extraction.timer
required_ancestor=7d0eac11ec75f3c179ca71564326883cd59ed152
expected_service_sha=932bb5c0888043773018ad92e023f1159f3d723d87d0362d2703c7b986b4be59
expected_timer_sha=4fd416d9eb18ff065b444be87beb5cfd92a2d3a624b8ae5822bcb2766d2c4289
expected_worker_sha=e73ffaf224d82a9fe2eedc8eed5c3069d3f996c28ca540dcc855b428db4360a5
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_extraction_scheduler_activate.lock
phase=initialization
quiesced=0
units_installed=0
timer_enabled=0
run_id=
status_file=
table_list=$(mktemp /tmp/memory-v1-v5-extraction-activate-tables.XXXXXX)
timer_state=$(mktemp /tmp/memory-v1-v5-extraction-activate-timers.XXXXXX)
baseline=
post=
existing_units=(
  memory-v1-consolidation.timer
  memory-v1-deferred-reconciliation-scan.timer
  memory-v1-evidence-intake-dispatcher.timer
  memory-v1-governance.timer
  memory-v1-projection.timer
  memory-v1-v5-chat-capture.timer
)

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
}

restore_existing_timers() {
  [[ "$quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$timer_state"
  quiesced=0
}

record_exit() {
  exit_code=$?
  if [[ "$exit_code" -ne 0 && "$units_installed" -eq 1 ]]; then
    sudo -n systemctl disable --now "$timer_unit" >/dev/null 2>&1 || true
    sudo -n systemctl stop "$service_unit" >/dev/null 2>&1 || true
  fi
  restore_existing_timers || exit_code=1
  rm -f "$table_list" "$timer_state"
  if [[ -n "$status_file" ]]; then
    {
      printf 'run_id=%s\n' "$run_id"
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$exit_code"
      printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$exit_code"
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

capture_protected_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    state=$(psql_scalar "
      SELECT count(*)::text || E'\\t' ||
             encode(public.digest(convert_to(coalesce(string_agg(
               row_json,E'\\n' ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value
      ) AS rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(sha256sum "$repo_root/$service_source" | awk '{print $1}')" == "$expected_service_sha" ]]
[[ "$(sha256sum "$repo_root/$timer_source" | awk '{print $1}')" == "$expected_timer_sha" ]]
[[ "$(sha256sum "$repo_root/scripts/memory_v1_v5_bounded_extraction_worker.py" | awk '{print $1}')" == "$expected_worker_sha" ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_extraction_activate_${run_id}.status"
baseline="$snapshot_dir/memory_v1_v5_extraction_activate_baseline_${run_id}.tsv"
post="$snapshot_dir/memory_v1_v5_extraction_activate_post_${run_id}.tsv"

phase=preflight
[[ "$(psql_scalar "SELECT (
  to_regclass('memory.v5_extraction_call_event') IS NOT NULL
  AND to_regprocedure(
    'memory.claim_owner_v5_bounded_extraction_job_v1(uuid,uuid,text,text,integer,integer,text,text,text,integer,integer,integer)'
  ) IS NOT NULL
  AND to_regprocedure(
    'memory.complete_owner_v5_extraction_call_v1(uuid,uuid,uuid,uuid,text,integer,text,text,text,text)'
  ) IS NOT NULL
)::int")" == 1 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.v5_extraction_call_event")" == 0 ]]
[[ ! -e "/etc/systemd/system/$service_unit" ]]
[[ ! -e "/etc/systemd/system/$timer_unit" ]]

: >"$timer_state"
for unit in "${existing_units[@]}"; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done
quiesced=1
for unit in "${existing_units[@]}"; do
  sudo -n systemctl stop "$unit"
  [[ "$(systemctl is-active "$unit")" == inactive ]]
done

phase=backup
backup_partial="$snapshot_dir/.memory_pre_v5_extraction_activate_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_extraction_activate_${run_id}.dump"
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

phase=baseline_capture
psql_scalar "
  SELECT table_name FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
    AND table_name NOT IN (
      'evidence_extraction_job','evidence_extraction_event',
      'evidence_extraction_packet_v5','v5_extraction_call_event'
    )
  ORDER BY table_name
" >"$table_list"
capture_protected_state "$baseline"
qdrant_before=$(qdrant_signature)
jobs_before=$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_job")
events_before=$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_event")
packets_before=$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_packet_v5")

phase=unit_install
sudo -n install -o root -g root -m 0644 \
  "$repo_root/$service_source" "/etc/systemd/system/$service_unit"
sudo -n install -o root -g root -m 0644 \
  "$repo_root/$timer_source" "/etc/systemd/system/$timer_unit"
sudo -n systemctl daemon-reload
units_installed=1
[[ "$(systemctl is-enabled "$timer_unit")" == disabled ]]
[[ "$(systemctl is-active "$timer_unit")" == inactive ]]
[[ "$(sha256sum "/etc/systemd/system/$service_unit" | awk '{print $1}')" == "$expected_service_sha" ]]
[[ "$(sha256sum "/etc/systemd/system/$timer_unit" | awk '{print $1}')" == "$expected_timer_sha" ]]

phase=manual_canary
sudo -n systemctl start "$service_unit"
[[ "$(systemctl show "$service_unit" -p Result --value)" == success ]]
[[ "$(systemctl is-active "$service_unit")" == inactive ]]

phase=canary_verification
jobs_after=$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_job")
events_after=$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_event")
packets_after=$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_packet_v5")
[[ "$jobs_after" == "$jobs_before" ]]
[[ $((events_after-events_before)) -eq 2 ]]
[[ $((packets_after-packets_before)) -ge 0 ]]
[[ $((packets_after-packets_before)) -le 1 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.v5_extraction_call_event
  WHERE owner_user_id='$owner'::uuid AND action='reserved'")" == 1 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.v5_extraction_call_event
  WHERE owner_user_id='$owner'::uuid AND action='completed'")" == 1 ]]
[[ "$(psql_scalar "SELECT external_model_calls FROM memory.v5_extraction_call_event
  WHERE owner_user_id='$owner'::uuid AND action='completed'")" == 1 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.v5_extraction_call_event
  WHERE owner_user_id<>'$owner'::uuid")" == 0 ]]
completion_outcome=$(psql_scalar "SELECT outcome FROM memory.v5_extraction_call_event
  WHERE owner_user_id='$owner'::uuid AND action='completed'")
[[ "$completion_outcome" == accepted || "$completion_outcome" == rejected ]]
if [[ "$completion_outcome" == accepted ]]; then
  [[ $((packets_after-packets_before)) -eq 1 ]]
else
  [[ $((packets_after-packets_before)) -eq 0 ]]
fi
capture_protected_state "$post"
cmp -s "$baseline" "$post" || {
  diff -u "$baseline" "$post" >&2 || true
  echo 'protected Memory V1 state changed during extraction canary' >&2
  exit 1
}
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=timer_enable
restore_existing_timers
sudo -n systemctl enable --now "$timer_unit"
timer_enabled=1
[[ "$(systemctl is-enabled "$timer_unit")" == enabled ]]
[[ "$(systemctl is-active "$timer_unit")" == active ]]

phase=report
report="$snapshot_dir/memory_v1_v5_extraction_activate_${run_id}.json"
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git -C "$repo_root" rev-parse HEAD)" \
  --arg backup_path "$backup" \
  --arg backup_sha256 "$backup_sha" \
  --arg baseline "$baseline" \
  --arg post "$post" \
  --arg qdrant_sha256 "$qdrant_after" \
  --arg canary_outcome "$completion_outcome" \
  --argjson packet_rows_created "$((packets_after-packets_before))" \
  '{
    contract_version:"memory_v1_v5_extraction_scheduler_activation_report_v1",
    completed_at:$completed_at,
    head_commit:$head_commit,
    backup:{path:$backup_path,sha256:$backup_sha256},
    canary:{outcome:$canary_outcome,external_model_calls:1,
      packet_rows_created:$packet_rows_created},
    evidence:{baseline:$baseline,post:$post,qdrant_sha256:$qdrant_sha256},
    checks:{
      owner_allowlist_count:1,
      max_jobs_per_cycle:1,
      rolling_call_limit:12,
      rolling_window_seconds:86400,
      failure_threshold:3,
      store_false:true,
      http_retries:0,
      queue_rows_constant:true,
      queue_events_created:2,
      other_owners_unchanged:true,
      protected_memory_tables_unchanged:true,
      qdrant_unchanged:true,
      prompt_influence:false,
      service_installed:true,
      timer_enabled:true,
      existing_timers_restored:true
    },
    hard_stop:"before_candidate_claim_or_projection_automation"
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf 'memory_v1_v5_extraction_scheduler_activate: PASS\n'
printf 'report=%s\nbackup=%s\ncanary_outcome=%s\n' \
  "$report" "$backup" "$completion_outcome"
