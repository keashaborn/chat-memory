#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the tested continuous, sequential V5.2 local
# extraction drain. It changes no schema and performs no external model calls.

if [[ ${EUID} -ne 0 ]]; then
  echo 'run through sudo' >&2
  exit 2
fi
if [[ "${MEMORY_V1_V5_2_CONTINUOUS_DRAIN_ACTIVATE:-}" != authorized ]]; then
  echo 'continuous drain activation authorization is required' >&2
  exit 2
fi

repo=/opt/chat-memory
container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
snapshot_dir=/home/ubuntu/brains/snapshots
service=memory-v1-v5-local-inference-scheduler.service
timer=memory-v1-v5-local-inference-scheduler.timer
service_source=ops/systemd/$service
timer_source=ops/systemd/$timer
benchmark_report=${MEMORY_V1_V5_2_CONTINUOUS_DRAIN_BENCHMARK_REPORT:-}
benchmark_sha=${MEMORY_V1_V5_2_CONTINUOUS_DRAIN_BENCHMARK_SHA256:-}
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_continuous_drain_activate.lock

phase=initialization
run_tag=
status_file=
timers_quiesced=0
units_installed=0
activation_committed=0
timer_state=$(mktemp /tmp/memory-v1-v5-2-continuous-drain-timers.XXXXXX)
unit_backup=$(mktemp -d /tmp/memory-v1-v5-2-continuous-drain-units.XXXXXX)
all_tables=$(mktemp /tmp/memory-v1-v5-2-continuous-drain-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-2-continuous-drain-before.XXXXXX)
after=$(mktemp /tmp/memory-v1-v5-2-continuous-drain-after.XXXXXX)
chmod 0600 "$timer_state" "$all_tables" "$before" "$after"
chmod 0700 "$unit_backup"

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
}

capture_memory() {
  local output=$1 table state
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
      ) AS rows")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$all_tables"
  chmod 0600 "$output"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

restore_timers() {
  [[ "$timers_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
    if [[ "$enabled" == enabled ]]; then
      systemctl enable "$unit" >/dev/null
    else
      [[ "$enabled" == disabled ]]
      systemctl disable "$unit" >/dev/null 2>&1 || true
    fi
    if [[ "$active" == active ]]; then
      systemctl start "$unit"
    else
      [[ "$active" == inactive ]]
      systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$timer_state"
  timers_quiesced=0
}

restore_units() {
  [[ "$units_installed" -eq 1 ]] || return 0
  install -o root -g root -m 0644 "$unit_backup/$service" \
    "/etc/systemd/system/$service"
  install -o root -g root -m 0644 "$unit_backup/$timer" \
    "/etc/systemd/system/$timer"
  systemctl daemon-reload
  units_installed=0
}

cleanup() {
  rc=$?
  trap - EXIT
  if [[ "$activation_committed" -eq 0 ]]; then
    restore_units || rc=1
  fi
  restore_timers || rc=1
  rm -rf "$unit_backup"
  rm -f "$timer_state" "$all_tables" "$before" "$after"
  if [[ -n "$status_file" ]]; then
    {
      printf 'run_tag=%s\n' "$run_tag"
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$rc"
      printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$rc"
}
trap cleanup EXIT

phase=source_preflight
cd "$repo"
[[ -z "$(git status --short)" ]]
sha256sum -c <<'HASHES'
9a703362087f5fef6111c6d7515fa7ad12a6bdc058a253c64a76e6d88b854fb8  ops/systemd/memory-v1-v5-local-inference-scheduler.service
49c00faacf1f890bf9abc04fac50e56eb9a7a64c05ff620ddcad837d433c6e0d  ops/systemd/memory-v1-v5-local-inference-scheduler.timer
0da89022b24453a3617a7d633cb019bc42de9de69c44f0c71011ae29b92e148e  scripts/memory_v1_v5_local_inference_scheduler.py
b0d0d727f3bcd0e0183e470aa2866c313bd951256ffc16f671bf91345e8da874  scripts/memory_v1_v5_local_inference_canary.py
cb70ae71926be29284593dbb67c8fcb8537a38bdf3e71dec330591494aec317f  scripts/memory_v1_v5_local_inference_scheduler_test.py
51aa730043eeaa46f54af173ff807519590791c2c26cd1e890bcc7e477787cb3  scripts/memory_v1_v5_local_inference_continuous_drain_contract_test.py
a6ca947fc8898f8ab3fda67fafa6628f4ee94a2cb14484bc6d0b62aa69051bf5  scripts/memory_v1_v5_2_continuous_drain_activation_contract_test.py
af28ef6f5e1a4e98bd236fcaaade3e4591b0bf96329d265a0462fe2daabafb50  tools/memory_v1_v5_local_inference_continuous_drain_clone_benchmark.sh
HASHES
PYTHONPATH="$repo" /opt/chat-memory/venv/bin/python \
  scripts/memory_v1_v5_local_inference_scheduler_test.py >/dev/null
PYTHONPATH="$repo" /opt/chat-memory/venv/bin/python \
  scripts/memory_v1_v5_local_inference_continuous_drain_contract_test.py \
  >/dev/null
PYTHONPATH="$repo" /opt/chat-memory/venv/bin/python \
  scripts/memory_v1_v5_2_continuous_drain_activation_contract_test.py \
  >/dev/null
systemd-analyze verify "$service_source" "$timer_source"
[[ -f "$benchmark_report" && "$benchmark_report" == "$snapshot_dir/"* ]]
[[ "$benchmark_sha" =~ ^[0-9a-f]{64}$ ]]
[[ "$(sha256sum "$benchmark_report" | awk '{print $1}')" == "$benchmark_sha" ]]
jq -e '
  .contract_version=="memory_v1_v5_continuous_drain_clone_benchmark_v1" and
  .processed==10 and .accepted==9 and .rejected==1 and
  .local_model_calls==10 and .external_model_calls==0 and
  .retry_jobs_repaired==4 and
  .checks.sequential_execution==true and
  .checks.max_attempts_two==true and
  .checks.retry_provenance_preserved==true and
  .checks.exact_ten_jobs_changed==true and
  .checks.other_owners_unchanged==true and
  .checks.production_memory_unchanged==true and
  .checks.qdrant_unchanged==true and
  .checks.claims_unchanged==true and
  .checks.answer_bindings_unchanged==true and
  .checks.external_model_calls_zero==true and
  .checks.private_gpu_health_ok==true
' "$benchmark_report" >/dev/null
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(systemctl is-active memory-v1-v5-local-inference-tunnel.service)" == active ]]
[[ "$(systemctl is-enabled "$timer")" == enabled ]]
[[ "$(systemctl is-active "$timer")" == active ]]
grep -q -- '--max-jobs 1' "/etc/systemd/system/$service"
grep -q -- '--max-attempts 1' "/etc/systemd/system/$service"
test -r /etc/memory-v1-local-inference/api-key

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_2_continuous_drain_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_2_continuous_drain_${run_tag}.json"

phase=quiesce_timers
: >"$timer_state"
while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" \
    "$(systemctl is-active "$unit")" >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ -s "$timer_state" ]]
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  systemctl stop "$unit"
done <"$timer_state"
while IFS=$'\t' read -r unit _enabled _active; do
  related_service=${unit%.timer}.service
  for _attempt in $(seq 1 60); do
    systemctl is-active --quiet "$related_service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$related_service"
done <"$timer_state"

phase=fresh_backup
backup_partial="$snapshot_dir/.memory_pre_v5_2_continuous_drain_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_2_continuous_drain_${run_tag}.dump"
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

phase=baseline
psql_scalar "
  SELECT table_name FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
  ORDER BY table_name" >"$all_tables"
[[ -s "$all_tables" ]]
capture_memory "$before"
qdrant_before=$(qdrant_signature)
status_counts_before=$(psql_scalar "
  SELECT coalesce(jsonb_object_agg(status,count ORDER BY status),'{}'::jsonb)
  FROM (
    SELECT status::text AS status,count(*)::integer AS count
    FROM memory.evidence_extraction_job
    WHERE owner_user_id='$owner'::uuid
    GROUP BY status
  ) AS counts")
eligible_before=$(psql_scalar "
  SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid
    AND route='relational_extraction'
    AND status IN ('pending','error')
    AND attempts<2
    AND available_at<=clock_timestamp()")
retry_jobs_repaired=$(psql_scalar "
  SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid
    AND route='relational_extraction'
    AND status IN ('pending','error')
    AND attempts=1
    AND available_at<=clock_timestamp()")
[[ "$retry_jobs_repaired" -eq 4 ]]
cp "/etc/systemd/system/$service" "$unit_backup/$service"
cp "/etc/systemd/system/$timer" "$unit_backup/$timer"
chmod 0600 "$unit_backup/$service" "$unit_backup/$timer"

phase=install_units
install -o root -g root -m 0644 "$service_source" \
  "/etc/systemd/system/$service"
install -o root -g root -m 0644 "$timer_source" \
  "/etc/systemd/system/$timer"
systemctl daemon-reload
units_installed=1
cmp -s "$service_source" "/etc/systemd/system/$service"
cmp -s "$timer_source" "/etc/systemd/system/$timer"
grep -q -- '--max-jobs 100' "/etc/systemd/system/$service"
grep -q -- '--max-attempts 2' "/etc/systemd/system/$service"
grep -q -- '--rolling-window-seconds 3600' \
  "/etc/systemd/system/$service"
grep -q -- '--max-reserved-jobs 100' "/etc/systemd/system/$service"
grep -q -- '--failure-threshold 10' "/etc/systemd/system/$service"
grep -q -- 'OnUnitInactiveSec=5min' "/etc/systemd/system/$timer"

phase=zero_data_change_verification
capture_memory "$after"
cmp -s "$before" "$after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=restore_timers_and_start
restore_timers
[[ "$(systemctl is-enabled "$timer")" == enabled ]]
[[ "$(systemctl is-active "$timer")" == active ]]
systemctl start --no-block "$service"
sleep 2
service_state=$(systemctl show "$service" -p ActiveState --value)
service_result=$(systemctl show "$service" -p Result --value)
[[ "$service_state" =~ ^(activating|active|inactive)$ ]]
if [[ "$service_state" == inactive ]]; then
  [[ "$service_result" == success ]]
fi

phase=report
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg backup "$backup" \
  --arg backup_sha256 "$backup_sha" \
  --arg benchmark_report "$benchmark_report" \
  --arg benchmark_sha256 "$benchmark_sha" \
  --arg qdrant_sha256 "$qdrant_after" \
  --arg service_state "$service_state" \
  --arg service_result "$service_result" \
  --argjson status_counts_before "$status_counts_before" \
  --argjson eligible_before "$eligible_before" \
  --argjson retry_jobs_repaired "$retry_jobs_repaired" \
  '{
    contract_version:"memory_v1_v5_2_continuous_drain_activation_v1",
    completed_at:$completed_at,
    head_commit:$head_commit,
    backup:{path:$backup,sha256:$backup_sha256},
    benchmark:{path:$benchmark_report,sha256:$benchmark_sha256},
    scheduler:{
      owner_count:1,
      profile:"v5_2",
      sequential:true,
      max_jobs_per_cycle:100,
      max_runtime_seconds:21600,
      max_attempts:2,
      rolling_window_seconds:3600,
      max_reserved_jobs:100,
      failure_threshold:10,
      timer_interval_seconds:300,
      active_state:$service_state,
      result:$service_result
    },
    baseline:{
      status_counts:$status_counts_before,
      eligible:$eligible_before,
      retry_jobs_repaired:$retry_jobs_repaired
    },
    checks:{
      fresh_backup:true,
      hash_locked_sources:true,
      clone_benchmark_passed:true,
      zero_data_change_before_start:true,
      qdrant_unchanged_before_start:true,
      timers_restored:true,
      account_scope_single_owner:true,
      external_model_calls:0,
      legacy_memory_enabled:false,
      prompt_policy_changed:false
    },
    qdrant_sha256:$qdrant_sha256
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

activation_committed=1
units_installed=0
phase=complete
printf '%s\n' \
  'memory_v1_v5_2_continuous_drain_activate: PASS' \
  "report=$report" \
  "backup=$backup" \
  "eligible_before=$eligible_before" \
  "retry_jobs_repaired=$retry_jobs_repaired" \
  "service_state=$service_state" \
  'timers_restored=true' \
  'qdrant_unchanged_before_start=true'
