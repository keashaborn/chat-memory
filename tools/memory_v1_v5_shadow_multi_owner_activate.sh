#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Expands sanitized V5 zero-influence shadow tracing from
# the admin owner to two owner-controlled canaries. It does not enable governed
# prompt influence, specialized prompt influence, or all-authenticated access.

if [[ "${MEMORY_V1_V5_SHADOW_MULTI_OWNER_INSTALL:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_SHADOW_MULTI_OWNER_INSTALL=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
admin=1240822d-ac9a-4096-95aa-e2b24d36ef50
lifeswitch=557ea042-cb82-48f8-9429-472e96c957ef
doctor=d839b4bc-0bd2-4f2d-aafe-0f3f75883db8
old_allowlist=$admin
new_allowlist=$admin,$lifeswitch,$doctor
container=brains-postgres-1
database=memory
env_file=/opt/chat-memory/.env
snapshot_dir=/home/ubuntu/brains/snapshots
required_ancestor=5586dabe5f43e5aed1173c793f0de3fedc29afc2
lock_file=/home/ubuntu/brains/.memory_v1_v5_shadow_multi_owner.lock
health_test=scripts/memory_v1_v5_shadow_trace_test.py
store_test=scripts/memory_v1_v5_shadow_trace_store_test.py
probe=scripts/memory_v1_v5_secondary_owner_shadow_probe.py

phase=initialization
run_tag=
status_file=
timers_quiesced=0
env_changed=0
installation_committed=0
timer_state=$(mktemp /tmp/memory-v1-v5-shadow-multi-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-shadow-multi-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-shadow-multi-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-v5-shadow-multi-after.XXXXXX.tsv)
restored=$(mktemp /tmp/memory-v1-v5-shadow-multi-restored.XXXXXX.tsv)
life_probe=$(mktemp /tmp/memory-v1-v5-shadow-multi-life.XXXXXX.json)
doctor_probe=$(mktemp /tmp/memory-v1-v5-shadow-multi-doctor.XXXXXX.json)
rm -f "$life_probe" "$doctor_probe"
chmod 0600 "$timer_state" "$table_list" "$before" "$after" "$restored"

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

authenticated_health() {
  set -a
  source "$env_file"
  set +a
  test -n "$VS_SERVICE_TOKEN"
  curl --fail --silent --show-error --max-time 10 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null
  curl --fail --silent --show-error --max-time 10 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/readyz \
    | jq -e '.ok==true and .postgres==true' >/dev/null
}

restart_brains() {
  sudo -n systemctl restart brains.service
  for _attempt in $(seq 1 30); do
    if [[ "$(systemctl is-active brains.service)" == active ]] \
       && authenticated_health; then
      return 0
    fi
    sleep 1
  done
  return 1
}

record_exit() {
  exit_code=$?
  if [[ "$env_changed" -eq 1 && "$installation_committed" -eq 0 ]]; then
    sudo -n install -o ubuntu -g ubuntu -m 0600 "$env_backup" "$env_file" \
      || exit_code=1
    restart_brains || exit_code=1
  fi
  restore_timers || exit_code=1
  rm -f "$timer_state" "$table_list" "$before" "$after" "$restored" \
    "$life_probe" "$doctor_probe"
  if [[ -n "$status_file" ]]; then
    {
      printf 'run_tag=%s\n' "$run_tag"
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$exit_code"
      printf 'env_changed=%s\n' "$env_changed"
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

[[ -z "$(git status --porcelain)" ]]
git merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(stat -c '%a:%U:%G' "$env_file")" == 600:ubuntu:ubuntu ]]
[[ "$(grep -c '^MEMORY_V1_V5_SHADOW_USER_IDS=' "$env_file")" == 1 ]]
grep -Fx "MEMORY_V1_V5_SHADOW_USER_IDS=$old_allowlist" "$env_file" >/dev/null
grep -Fx 'MEMORY_V1_V5_SHADOW=1' "$env_file" >/dev/null
grep -Fx 'MEMORY_V1_V5_SHADOW_TRACE_PERSISTENCE=1' "$env_file" >/dev/null
grep -Fx 'MEMORY_V1_V5_SHADOW_ALL_AUTHENTICATED=0' "$env_file" >/dev/null
! grep -Fx 'MEMORY_V1_GOVERNED_ACTIVE=1' "$env_file" >/dev/null
! grep -Fx 'MEMORY_V1_SPECIALIZED_ACTIVE=1' "$env_file" >/dev/null
[[ "$(systemctl is-active brains.service)" == active ]]
authenticated_health
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$health_test" >/dev/null
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$store_test" >/dev/null

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_shadow_multi_owner_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_shadow_multi_owner_${run_tag}.json"
env_backup="$snapshot_dir/memory_pre_v5_shadow_multi_owner_${run_tag}.env"

phase=quiesce_timers
: >"$timer_state"
while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ "$(wc -l <"$timer_state")" -ge 1 ]]
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

phase=backup_and_baseline
cp -p "$env_file" "$env_backup"
chmod 0600 "$env_backup"
env_backup_sha=$(sha256sum "$env_backup" | awk '{print $1}')
printf '%s  %s\n' "$env_backup_sha" "$env_backup" >"$env_backup.sha256"
chmod 0600 "$env_backup.sha256"
query_rows "
  SELECT table_schema || E'\\t' || table_name
  FROM information_schema.tables
  WHERE table_type='BASE TABLE' AND table_schema IN ('memory','public')
  ORDER BY table_schema,table_name
" >"$table_list"
capture_state "$before"
qdrant_before=$(qdrant_signature)

phase=update_allowlist
sed -i \
  "s|^MEMORY_V1_V5_SHADOW_USER_IDS=$old_allowlist$|MEMORY_V1_V5_SHADOW_USER_IDS=$new_allowlist|" \
  "$env_file"
env_changed=1
[[ "$(stat -c '%a:%U:%G' "$env_file")" == 600:ubuntu:ubuntu ]]
grep -Fx "MEMORY_V1_V5_SHADOW_USER_IDS=$new_allowlist" "$env_file" >/dev/null
[[ "$(grep -c '^MEMORY_V1_V5_SHADOW_USER_IDS=' "$env_file")" == 1 ]]
normalized_env_sha=$(sed \
  "s|^MEMORY_V1_V5_SHADOW_USER_IDS=$new_allowlist$|MEMORY_V1_V5_SHADOW_USER_IDS=$old_allowlist|" \
  "$env_file" | sha256sum | awk '{print $1}')
[[ "$normalized_env_sha" == "$env_backup_sha" ]]

phase=restart_and_probe
restart_brains
main_pid=$(systemctl show brains.service -p MainPID --value)
[[ "$main_pid" =~ ^[1-9][0-9]*$ ]]
sudo -n tr '\0' '\n' <"/proc/$main_pid/environ" \
  | grep -Fx "MEMORY_V1_V5_SHADOW_USER_IDS=$new_allowlist" >/dev/null
sudo -n tr '\0' '\n' <"/proc/$main_pid/environ" \
  | grep -Fx 'MEMORY_V1_V5_SHADOW_ALL_AUTHENTICATED=0' >/dev/null
! sudo -n tr '\0' '\n' <"/proc/$main_pid/environ" \
  | grep -Fx 'MEMORY_V1_GOVERNED_ACTIVE=1' >/dev/null
! sudo -n tr '\0' '\n' <"/proc/$main_pid/environ" \
  | grep -Fx 'MEMORY_V1_SPECIALIZED_ACTIVE=1' >/dev/null

set -a
source "$env_file"
set +a
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$probe" \
  --owner-user-id "$lifeswitch" \
  --seed-claim-id 00d3f592-05a9-49a4-9bab-d2d7f4a09919 \
  --query 'What do you remember about me?' \
  --request-classification SPECIFIC_RECALL --expected-selected 0 \
  --output "$life_probe" >/dev/null
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$probe" \
  --owner-user-id "$doctor" \
  --seed-claim-id fc4b1c5b-40f6-4e8e-8c78-8b3429930506 \
  --query 'Tell me about my pets.' \
  --request-classification SPECIFIC_RECALL --expected-selected 1 \
  --output "$doctor_probe" >/dev/null

phase=zero_write_postflight
capture_state "$after"
cmp -s "$before" "$after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]

phase=restore_timers
restore_timers
capture_state "$restored"
cmp -s "$before" "$restored"
qdrant_restored=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_restored" ]]
installation_committed=1

phase=report
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg env_backup "$env_backup" --arg env_backup_sha256 "$env_backup_sha" \
  --arg qdrant_sha256 "$qdrant_restored" \
  --arg life_probe_sha256 "$(sha256sum "$life_probe" | awk '{print $1}')" \
  --arg doctor_probe_sha256 "$(sha256sum "$doctor_probe" | awk '{print $1}')" \
  '{contract_version:"memory_v1_v5_shadow_multi_owner_activation_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    owner_scope:{count:3,all_authenticated:false},
    backup:{path:$env_backup,sha256:$env_backup_sha256},
    probes:{lifeswitch:{selected_count:0,sha256:$life_probe_sha256},
      doctor:{selected_count:1,sha256:$doctor_probe_sha256}},
    checks:{exact_config_change:true,brains_health_ready:true,
      live_process_config_verified:true,owner_filtered_probes:true,
      database_unchanged:true,qdrant_unchanged:true,
      timers_restored_exactly:true,prompt_influence_zero:true,
      all_authenticated_disabled:true,external_model_calls_zero:true},
    qdrant_sha256:$qdrant_sha256,
    hard_stop:"before_prompt_influence_or_general_account_activation"}' \
  >"$report"
chmod 0600 "$report"
jq -e '.checks|to_entries|all(.value==true)' "$report" >/dev/null
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
printf '%s\n' "$report"
