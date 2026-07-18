#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the blocker-free local auto-stage admission gate
# and bounded timer. The installation canary must be zero-work and cannot write
# claims, projections, Qdrant, files, retrieval state, or prompts.

if [[ "${MEMORY_V1_V5_LOCAL_AUTO_STAGE_INSTALL:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_LOCAL_AUTO_STAGE_INSTALL=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
review_root=/home/ubuntu/memory-v1-reviews
service=memory-v1-v5-local-auto-stage.service
timer=memory-v1-v5-local-auto-stage.timer
service_source=ops/systemd/$service
timer_source=ops/systemd/$timer
migration=ops/sql/20260718_memory_v1_v5_local_auto_stage_admission.sql
rollback=ops/sql/20260718_memory_v1_v5_local_auto_stage_admission_rollback.sql
production_test=tests/memory_v1_v5_local_auto_stage_production.sql
worker=scripts/memory_v1_v5_local_auto_stage.py
worker_test=scripts/memory_v1_v5_local_auto_stage_test.py
lock_file=/home/ubuntu/brains/.memory_v1_v5_local_auto_stage_install.lock

declare -A expected_sha256=(
  ["$migration"]="c33463cd68972a22e62514126db363d02294aa0937073ef321f02394c23f9ad8"
  ["$rollback"]="96b25afe673d8fdefb6f9a9dc9fdf7dfc79a436f3822055159e62e01007461af"
  ["$production_test"]="70405f8be1eeb0bf9852e2d4ccaa6e0010b90bcd3ae85e77137ed9652aec5165"
  ["$worker"]="704aba13305c7ad0d9636b3757c4823f5ac98ecaa82df4e984fa05c38eb3c145"
  ["$worker_test"]="95d80242145c49fcbaf358fba9abfffa4a77cc75066b9806492456bcb712f1f0"
  ["$service_source"]="4d48e096812f9fc7b8a97bfb12d4ecac5dae3f0c9ea2714389b7f5ef5974da9b"
  ["$timer_source"]="66d8605f7f0c2f91eeec444339f7db216256dff876e603a00b87a164fb16df9e"
)

phase=initialization
run_tag=
status_file=
timers_quiesced=0
migration_installed=0
units_installed=0
installation_committed=0
timer_state=$(mktemp /tmp/memory-v1-v5-local-auto-stage-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-local-auto-stage-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-local-auto-stage-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-v5-local-auto-stage-after.XXXXXX.tsv)
service_output=$(mktemp /tmp/memory-v1-v5-local-auto-stage-service.XXXXXX.json)
other_plan=$(mktemp /tmp/memory-v1-v5-local-auto-stage-other.XXXXXX.json)
chmod 0600 "$timer_state" "$table_list" "$before" "$after" \
  "$service_output" "$other_plan"

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

record_exit() {
  exit_code=$?
  if [[ "$units_installed" -eq 1 && "$installation_committed" -eq 0 ]]; then
    sudo -n systemctl disable --now "$timer" >/dev/null 2>&1 || true
    sudo -n rm -f "/etc/systemd/system/$service" "/etc/systemd/system/$timer"
    sudo -n systemctl daemon-reload
  fi
  if [[ "$migration_installed" -eq 1 && "$installation_committed" -eq 0 ]]; then
    run_sql <"$rollback" >/dev/null 2>&1 || exit_code=1
  fi
  restore_timers || exit_code=1
  rm -f "$timer_state" "$table_list" "$before" "$after" \
    "$service_output" "$other_plan"
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

review_root_signature() {
  (
    cd "$review_root"
    find . -type f -print0 | sort -z | xargs -0r sha256sum
  ) | sha256sum | awk '{print $1}'
}

for required in "${!expected_sha256[@]}"; do
  [[ -f "$required" ]]
  [[ "$(sha256sum "$required" | awk '{print $1}')" \
      == "${expected_sha256[$required]}" ]]
done
[[ -z "$(git status --porcelain)" ]]
git merge-base --is-ancestor 68c7b16 HEAD
[[ "$(systemctl is-active brains.service)" == active ]]
[[ ! -e "/etc/systemd/system/$service" ]]
[[ ! -e "/etc/systemd/system/$timer" ]]
[[ "$(scalar "SELECT to_regclass('memory.v5_local_packet_stage_admission') IS NULL")" == t ]]
[[ "$(stat -c '%a:%U:%G' "$review_root")" == 700:ubuntu:ubuntu ]]
PYTHONPATH="$repo_root" python3 "$worker_test" >/dev/null
systemd-analyze verify "$service_source" "$timer_source"

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_local_auto_stage_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_local_auto_stage_${run_tag}.json"
preserved_service_output="$snapshot_dir/memory_v1_v5_local_auto_stage_${run_tag}.service.json"

phase=quiesce_timers
: >"$timer_state"
while IFS= read -r unit; do
  [[ "$unit" != "$timer" ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ "$(wc -l <"$timer_state")" -eq 8 ]]
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  sudo -n systemctl stop "$unit"
done <"$timer_state"
while IFS=$'\t' read -r unit _enabled _active; do
  existing_service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$existing_service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$existing_service"
done <"$timer_state"

phase=backup
backup_partial="$snapshot_dir/.memory_pre_v5_local_auto_stage_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_local_auto_stage_${run_tag}.dump"
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
query_rows "SELECT table_schema || E'\\t' || table_name
  FROM information_schema.tables
  WHERE table_type='BASE TABLE' AND table_schema IN ('memory','public')
  ORDER BY table_schema,table_name" >"$table_list"
capture_state "$before"
qdrant_before=$(qdrant_signature)
review_root_before=$(review_root_signature)

phase=install_schema
run_sql <"$migration" >/dev/null
migration_installed=1
run_sql -v other_owner_user_id="$other" <"$production_test" >/dev/null
[[ "$(scalar 'SELECT count(*) FROM memory.v5_local_packet_stage_admission')" == 0 ]]

phase=install_disabled_units
sudo -n install -o root -g root -m 0644 "$service_source" \
  "/etc/systemd/system/$service"
sudo -n install -o root -g root -m 0644 "$timer_source" \
  "/etc/systemd/system/$timer"
sudo -n systemctl daemon-reload
units_installed=1
[[ "$(systemctl is-enabled "$timer")" == disabled ]]
[[ "$(systemctl is-active "$timer")" == inactive ]]

phase=zero_work_canary
started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
sudo -n systemctl start "$service"
[[ "$(systemctl show "$service" -p Result --value)" == success ]]
sudo -n journalctl -u "$service" --since "$started_at" --no-pager -o cat \
  | grep '^{' | tail -n 1 >"$service_output"
jq -e '
  .worker_version=="memory_v1_v5_local_auto_stage_v1" and
  .apply==true and .outcome=="no_work" and .database_rows_created==0 and
  .zero_write_replay_proved==true and .filesystem_writes==0 and
  .external_model_calls==0 and .write_counts.admission==0 and
  .write_counts.stage_and_request==0 and .write_counts.claims==0 and
  .write_counts.projections==0 and .write_counts.qdrant==0 and
  .write_counts.prompt_influence==0
' "$service_output" >/dev/null
cp "$service_output" "$preserved_service_output"
chmod 0600 "$preserved_service_output"

set -a
source .env
set +a
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$other" --review-root "$review_root" >"$other_plan"
jq -e '.apply==false and .database_writes==0 and .filesystem_writes==0 and
  .qdrant_writes==0 and .external_model_calls==0 and .prompt_influence==0' \
  "$other_plan" >/dev/null

phase=postflight
capture_state "$after"
cmp -s "$before" "$after"
[[ "$(scalar 'SELECT count(*) FROM memory.v5_local_packet_stage_admission')" == 0 ]]
review_root_after=$(review_root_signature)
[[ "$review_root_before" == "$review_root_after" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]

phase=restore_timers
restore_timers

phase=enable_timer
sudo -n systemctl enable --now "$timer" >/dev/null
[[ "$(systemctl is-enabled "$timer")" == enabled ]]
[[ "$(systemctl is-active "$timer")" == active ]]
[[ "$(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u | wc -l)" -eq 9 ]]

phase=report
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg owner_sha256 "$(printf %s "$owner" | sha256sum | awk '{print $1}')" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg service_output "$preserved_service_output" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{contract_version:"memory_v1_v5_local_auto_stage_install_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    owner_user_id_sha256:$owner_sha256,
    backup:{path:$backup,sha256:$backup_sha256},
    canary:{sanitized_output:$service_output,outcome:"no_work",
      local_model_calls:0,external_model_calls:0},
    policy:{version:"memory_v1_v5_local_auto_stage_policy_v1",
      requires_zero_manual_deferred_rejected_and_blocking:true,
      max_packets_per_cycle:1},
    checks:{fresh_backup:true,hash_locked_runtime:true,forced_rls:true,
      append_only:true,function_only_write_access:true,
      cross_owner_rejected:true,all_preexisting_rows_unchanged:true,
      admission_rows_zero:true,existing_review_files_unchanged:true,
      qdrant_unchanged:true,claims_written_zero:true,
      projections_written_zero:true,prompt_influence_zero:true,
      existing_timers_restored:true,auto_stage_timer_enabled:true},
    qdrant_sha256:$qdrant_sha256,
    hard_stop:"before_entity_review_apply_projection_claim_qdrant_or_prompt_influence"}' \
  >"$report"
chmod 0600 "$report"
jq -e '.checks|to_entries|map(.value==true)|all' "$report" >/dev/null
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
installation_committed=1

phase=complete
printf 'memory_v1_v5_local_auto_stage_production_install: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
