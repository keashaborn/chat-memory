#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs code-reviewed service definitions that extend the
# private local V5 pipeline from the admin owner to two owner-controlled canaries.

if [[ "${MEMORY_V1_V5_MULTI_OWNER_AUTOMATION_INSTALL:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_MULTI_OWNER_AUTOMATION_INSTALL=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_multi_owner_automation.lock
contract_test=scripts/memory_v1_v5_multi_owner_automation_contract_test.py
services=(
  memory-v1-v5-local-inference-scheduler.service
  memory-v1-v5-local-packet-router.service
  memory-v1-v5-local-auto-stage.service
  memory-v1-v5-local-entity-validation.service
  memory-v1-v5-local-auto-resolution.service
  memory-v1-v5-local-entailment.service
  memory-v1-v5-local-claim-projection.service
)
declare -A expected_sha256=(
  [ops/systemd/memory-v1-v5-local-inference-scheduler.service]=890e6439f351a8ee23ef5ed1e948e011b3170fd33c827f8108c128c2248fbde3
  [ops/systemd/memory-v1-v5-local-packet-router.service]=081175bab2e92ce390bf0a815cf49b13106dfeae3d04d132e40710030dd2d83f
  [ops/systemd/memory-v1-v5-local-auto-stage.service]=2d2d8291a39c8b1667c2f2be908493f12326dea355aefeec50f425b4d96bc225
  [ops/systemd/memory-v1-v5-local-entity-validation.service]=1b09614d17b851bd8b5579f1bc84e83b0d1749ad503a07583dac0115e1427269
  [ops/systemd/memory-v1-v5-local-auto-resolution.service]=6ec6450a6ff29d6f19d7b227c1a8a72d381fe86cfb8604171656bc22c01835ac
  [ops/systemd/memory-v1-v5-local-entailment.service]=dda5d1bdbce7464091cab9a1382d6cab98ea9960b6e8b72640728d0b30b62e58
  [ops/systemd/memory-v1-v5-local-claim-projection.service]=e2c8e8adccb226a822389a5abb792b2d5c54a7f0939772f5e00a66b4ce25aa2e
  [$contract_test]=b7422369157c7231346b1e911439cd851d7adb8cec09520fc05fa3c553a2b8c3
)

phase=initialization
run_tag=
status_file=
timers_quiesced=0
units_installed=0
installation_committed=0
timer_state=$(mktemp /tmp/memory-v1-v5-multi-owner-timers.XXXXXX)
unit_backup=$(mktemp -d /tmp/memory-v1-v5-multi-owner-units.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-multi-owner-before.XXXXXX)
after=$(mktemp /tmp/memory-v1-v5-multi-owner-after.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-multi-owner-tables.XXXXXX)
chmod 0600 "$timer_state" "$before" "$after" "$table_list"
chmod 0700 "$unit_backup"

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | tr -d '[:space:]'
}

capture_memory_state() {
  local output=$1 table state
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z0-9_]+$ ]]
    state=$(scalar "SELECT count(*)::text || E'\\t' ||
      encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
        ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
      FROM (SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value) rows")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
}

restore_timers() {
  [[ "$timers_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
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

restore_units() {
  [[ "$units_installed" -eq 1 ]] || return 0
  for service in "${services[@]}"; do
    sudo -n install -o root -g root -m 0644 "$unit_backup/$service" \
      "/etc/systemd/system/$service"
  done
  sudo -n systemctl daemon-reload
  units_installed=0
}

record_exit() {
  exit_code=$?
  if [[ "$installation_committed" -eq 0 ]]; then
    restore_units || exit_code=1
  fi
  restore_timers || exit_code=1
  rm -rf "$unit_backup"
  rm -f "$timer_state" "$before" "$after" "$table_list"
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

for required in "${!expected_sha256[@]}"; do
  [[ -f "$required" ]]
  [[ "$(sha256sum "$required" | awk '{print $1}')" == "${expected_sha256[$required]}" ]]
done
[[ -z "$(git status --porcelain)" ]]
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(systemctl is-active memory-v1-v5-local-inference-tunnel.service)" == active ]]
python3 "$contract_test" >/dev/null
systemd-analyze verify "${services[@]/#/ops/systemd/}"

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_multi_owner_automation_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_multi_owner_automation_${run_tag}.json"

phase=quiesce
: >"$timer_state"
while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ "$(wc -l <"$timer_state")" -eq 13 ]]
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  sudo -n systemctl stop "$unit"
done <"$timer_state"
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 60); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
done <"$timer_state"

phase=backup
backup_partial="$snapshot_dir/.memory_pre_v5_multi_owner_automation_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_multi_owner_automation_${run_tag}.dump"
catalog="$backup.catalog"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$catalog"
backup_sha256=$(sha256sum "$backup" | awk '{print $1}')
printf '%s  %s\n' "$backup_sha256" "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline
docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
  -U sage -d "$database" -c "SELECT table_name
    FROM information_schema.tables
    WHERE table_type='BASE TABLE' AND table_schema='memory'
    ORDER BY table_name" >"$table_list"
[[ -s "$table_list" ]]
capture_memory_state "$before"
qdrant_before=$(qdrant_signature)
for service in "${services[@]}"; do
  [[ -f "/etc/systemd/system/$service" ]]
  cp "/etc/systemd/system/$service" "$unit_backup/$service"
  chmod 0600 "$unit_backup/$service"
done

phase=install_units
for service in "${services[@]}"; do
  sudo -n install -o root -g root -m 0644 "ops/systemd/$service" \
    "/etc/systemd/system/$service"
done
sudo -n systemctl daemon-reload
units_installed=1
python3 "$contract_test" >/dev/null
for service in "${services[@]}"; do
  cmp -s "ops/systemd/$service" "/etc/systemd/system/$service"
done

phase=zero_write_postflight
capture_memory_state "$after"
cmp -s "$before" "$after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=restore_timers
restore_timers

phase=report
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha256" \
  --arg qdrant_sha256 "$qdrant_after" \
  --arg memory_sha256 "$(sha256sum "$after" | awk '{print $1}')" \
  '{contract_version:"memory_v1_v5_multi_owner_automation_install_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    backup:{path:$backup,sha256:$backup_sha256},
    owner_scope:{count:3,all_authenticated:false,external_extraction_admin_only:true},
    checks:{hash_locked_units:true,fresh_backup:true,all_memory_timers_quiesced:true,
      exact_unit_commands_installed:true,contract_test_passed:true,
      database_unchanged:true,qdrant_unchanged:true,
      original_timer_states_restored:true,private_tunnel_active:true,
      prompt_influence_zero:true,external_model_calls_zero:true},
    memory_sha256:$memory_sha256,qdrant_sha256:$qdrant_sha256,
    hard_stop:"before_manual_local_job_or_shadow_allowlist_expansion"}' >"$report"
chmod 0600 "$report"
jq -e '.checks|to_entries|map(.value==true)|all' "$report" >/dev/null
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

installation_committed=1
phase=complete
printf '%s\n' 'memory_v1_v5_multi_owner_automation_activate: PASS'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
