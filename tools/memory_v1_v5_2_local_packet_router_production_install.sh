#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the append-only V5.2 route ledger and one
# owner-allowlisted router. The router cannot stage, promote, retrieve, write
# Qdrant, or influence prompts.

if [[ "${MEMORY_V1_V5_2_ROUTER_INSTALL:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_ROUTER_INSTALL=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
review_root=/home/ubuntu/memory-v1-reviews
service=memory-v1-v5-2-local-packet-router.service
timer=memory-v1-v5-2-local-packet-router.timer
migration=ops/sql/20260724_memory_v1_v5_2_local_packet_router.sql
rollback=ops/sql/20260724_memory_v1_v5_2_local_packet_router_rollback.sql
security_test=tests/memory_v1_v5_2_local_packet_router_security.sql
unit_test=tests/test_memory_v1_v5_2_local_packet_router.py
worker=scripts/memory_v1_v5_2_local_packet_router.py
service_source=ops/systemd/$service
timer_source=ops/systemd/$timer
expected_migration_sha=cb3d5a4ab780d7af9faf725cd2809616f749d82187423ef6e363c790f71a0375
expected_rollback_sha=5d79cb3342284e4c9b432c893587ebca37f9e9ae4d2a03cda976078d4f076555
expected_security_test_sha=c36f6aaf8ea9aa28173d177b60c9651dcbc594c591d20abd828a67cc5a3a432f
expected_unit_test_sha=e8a5847dc99dd45a0b127ef3949469f5c0bcc99a1d5d7549f5901b838ff3be8e
expected_worker_sha=9e65406593cf1badbd065fdde3415d6b54b1b1487e406926fd95b4d2ba5efb93
expected_service_sha=ef45b11cd3066f7d1ecd7f57b125f18041e0cd8543cbaff3c2704f13f2ee859e
expected_timer_sha=aed751a6f305eb624b14dff6b6688e27c2b596e8165d052f8dd5835a34db6b94
required_base=b28ab2534c05ffe9b002168b9827a9599204d675
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_router_install.lock

phase=initialization
run_tag=
status_file=
units_quiesced=0
schema_installed=0
units_installed=0
installation_committed=0
unit_state=$(mktemp /tmp/memory-v1-v5-2-router-units.XXXXXX)
protected_tables=$(mktemp /tmp/memory-v1-v5-2-router-tables.XXXXXX)
protected_before=$(mktemp /tmp/memory-v1-v5-2-router-protected-before.XXXXXX)
protected_after=$(mktemp /tmp/memory-v1-v5-2-router-protected-after.XXXXXX)
review_before=$(mktemp /tmp/memory-v1-v5-2-router-reviews-before.XXXXXX)
review_after=$(mktemp /tmp/memory-v1-v5-2-router-reviews-after.XXXXXX)

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | tr -d '[:space:]'
}

run_sql() {
  docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
    -U sage -d "$database" "$@"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

capture_protected() {
  local output=$1 table state
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    state=$(psql_scalar "
      SELECT count(*)::text || E'\\t' || encode(public.digest(convert_to(
        coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
        'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value
      ) AS rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$protected_tables"
  chmod 0600 "$output"
}

capture_reviews() {
  local output=$1
  find "$review_root" -maxdepth 1 -type f \
    -name 'v5-2-router-*.json' -print0 \
    | sort -z \
    | xargs -0 -r sha256sum >"$output"
  chmod 0600 "$output"
}

restore_units() {
  [[ "$units_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
    if [[ "$enabled" == enabled ]]; then
      sudo -n systemctl enable "$unit" >/dev/null
    else
      sudo -n systemctl disable "$unit" >/dev/null
    fi
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$unit_state"
  units_quiesced=0
}

remove_new_review_files() {
  capture_reviews "$review_after"
  comm -13 <(sort "$review_before") <(sort "$review_after") \
    | awk '{print $2}' \
    | while IFS= read -r path; do
        [[ "$path" == "$review_root"/v5-2-router-*.json ]]
        rm -f -- "$path"
      done
}

record_exit() {
  exit_code=$?
  if [[ "$installation_committed" -eq 0 ]]; then
    if [[ "$units_installed" -eq 1 ]]; then
      sudo -n systemctl disable --now "$timer" >/dev/null 2>&1 || true
      sudo -n rm -f "/etc/systemd/system/$service" \
        "/etc/systemd/system/$timer"
      sudo -n systemctl daemon-reload
    fi
    if [[ "$schema_installed" -eq 1 ]]; then
      run_sql <"$rollback" >/dev/null 2>&1 || exit_code=1
      remove_new_review_files || exit_code=1
    fi
  fi
  restore_units || exit_code=1
  rm -f "$unit_state" "$protected_tables" "$protected_before" \
    "$protected_after" "$review_before" "$review_after"
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

for pair in \
  "$migration:$expected_migration_sha" \
  "$rollback:$expected_rollback_sha" \
  "$security_test:$expected_security_test_sha" \
  "$unit_test:$expected_unit_test_sha" \
  "$worker:$expected_worker_sha" \
  "$service_source:$expected_service_sha" \
  "$timer_source:$expected_timer_sha"; do
  file=${pair%%:*}
  expected=${pair##*:}
  [[ -f "$file" ]]
  [[ "$expected" =~ ^[0-9a-f]{64}$ ]]
  [[ "$(sha256sum "$file" | awk '{print $1}')" == "$expected" ]]
done

[[ -z "$(git status --porcelain)" ]]
git merge-base --is-ancestor "$required_base" HEAD
[[ "$(systemctl is-active brains.service)" == active ]]
[[ ! -e "/etc/systemd/system/$service" ]]
[[ ! -e "/etc/systemd/system/$timer" ]]
[[ "$(psql_scalar \
  "SELECT (to_regclass('memory.v5_2_local_packet_route_event') IS NULL)::int")" \
  == 1 ]]
[[ "$(psql_scalar \
  "SELECT (to_regrole('memory_v5_2_local_router_maintainer') IS NULL)::int")" \
  == 1 ]]
systemd-analyze verify "$service_source" "$timer_source"
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$unit_test"
[[ "$(stat -c '%a:%U:%G' "$review_root")" == 700:ubuntu:ubuntu ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_2_router_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_2_router_${run_tag}.json"
service_output="$snapshot_dir/memory_v1_v5_2_router_${run_tag}.service.json"

phase=quiesce_existing_timers
: >"$unit_state"
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  [[ "$unit" != "$timer" ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$unit_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' \
  --no-legend --no-pager | awk '{print $1}' | sort -u)
[[ -s "$unit_state" ]]
units_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  sudo -n systemctl stop "$unit"
done <"$unit_state"
while IFS=$'\t' read -r unit _enabled _active; do
  existing_service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$existing_service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$existing_service"
done <"$unit_state"

phase=backup
backup_partial="$snapshot_dir/.memory_pre_v5_2_router_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_2_router_${run_tag}.dump"
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
  SELECT table_name
  FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
  ORDER BY table_name
" >"$protected_tables"
[[ -s "$protected_tables" ]]
capture_protected "$protected_before"
capture_reviews "$review_before"
qdrant_before=$(qdrant_signature)

phase=install_schema
run_sql <"$migration" >/dev/null
schema_installed=1
[[ "$(psql_scalar \
  'SELECT count(*) FROM memory.v5_2_local_packet_route_event')" == 0 ]]

target_packet=$(psql_scalar "
  SELECT packet_id
  FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid
    AND normalized_packet->>'contract_version'
          ='memory_v1_relational_extraction_v5_2'
  ORDER BY created_at,packet_id
  LIMIT 1
")
[[ "$target_packet" =~ ^[0-9a-f-]{36}$ ]]

set -a
source /opt/chat-memory/.env
set +a
psql "$POSTGRES_DSN" -X -v ON_ERROR_STOP=1 \
  -v target_packet_id="$target_packet" <"$security_test" >/dev/null
[[ "$(psql_scalar \
  'SELECT count(*) FROM memory.v5_2_local_packet_route_event')" == 0 ]]

phase=install_disabled_units
sudo -n install -o root -g root -m 0644 "$service_source" \
  "/etc/systemd/system/$service"
sudo -n install -o root -g root -m 0644 "$timer_source" \
  "/etc/systemd/system/$timer"
sudo -n systemctl daemon-reload
units_installed=1
[[ "$(systemctl is-enabled "$timer")" == disabled ]]
[[ "$(systemctl is-active "$timer")" == inactive ]]

phase=one_owner_route_canary
started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
sudo -n systemctl start "$service"
[[ "$(systemctl show "$service" -p Result --value)" == success ]]
sudo -n journalctl -u "$service" --since "$started_at" \
  --no-pager -o cat | grep '^{' | tail -n 1 >"$service_output"
chmod 0600 "$service_output"
jq -e '
  .worker_version=="memory_v1_v5_2_local_packet_router_v1" and
  .apply==true and
  (.outcome=="terminal_no_stage" or
   .outcome=="manual_review_artifact_ready") and
  .write_counts.route_events==1 and
  (.write_counts.restricted_review_artifacts==0 or
   .write_counts.restricted_review_artifacts==2) and
  .write_counts.stage==0 and .write_counts.claims==0 and
  .write_counts.qdrant==0 and .write_counts.prompt_influence==0 and
  .zero_write_replay_proved==true and .external_model_calls==0
' "$service_output" >/dev/null
outcome=$(jq -r '.outcome' "$service_output")

phase=postflight
capture_protected "$protected_after"
cmp -s "$protected_before" "$protected_after"
[[ "$(psql_scalar "
  SELECT count(*)
  FROM memory.v5_2_local_packet_route_event
  WHERE owner_user_id='$owner'::uuid
")" == 1 ]]
[[ "$(psql_scalar "
  SELECT count(*)
  FROM memory.v5_2_local_packet_route_event
  WHERE owner_user_id<>'$owner'::uuid
")" == 0 ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]

phase=restore_existing_timers
restore_units

phase=enable_v5_2_router
sudo -n systemctl enable --now "$timer" >/dev/null
[[ "$(systemctl is-enabled "$timer")" == enabled ]]
[[ "$(systemctl is-active "$timer")" == active ]]
installation_committed=1

phase=report
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg owner_sha256 "$(printf %s "$owner" | sha256sum | awk '{print $1}')" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg service_output "$service_output" --arg outcome "$outcome" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{contract_version:"memory_v1_v5_2_router_install_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    owner_user_id_sha256:$owner_sha256,
    backup:{path:$backup,sha256:$backup_sha256},
    canary:{sanitized_output:$service_output,outcome:$outcome,
      external_model_calls:0},
    checks:{fresh_backup:true,hash_locked_runtime:true,
      one_owner_scoped_route:true,zero_write_replay:true,
      protected_memory_tables_unchanged:true,
      other_owner_route_rows:0,qdrant_unchanged:true,
      stage_writes:0,claim_writes:0,prompt_influence:0,
      existing_timers_restored:true,v5_2_router_timer_enabled:true},
    qdrant_sha256:$qdrant_sha256,
    schedule:{max_packets_per_cycle:1,interval:"1m",
      owner_allowlist_size:1},
    hard_stop:"before_v5_2_stage_entity_resolution_claim_projection_retrieval_or_prompt_influence"}' \
  >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf 'memory_v1_v5_2_local_packet_router_production_install: PASS\n'
printf 'report=%s\nbackup=%s\noutcome=%s\n' \
  "$report" "$backup" "$outcome"
