#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Switches the already-installed bounded private local
# extraction scheduler from V5.1 to V5.2 after a hash-locked employment canary
# report proves the compiler-v6 behavior. This performs no model call.

if [[ "${MEMORY_V1_V5_2_SCHEDULER_ACTIVATE:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_SCHEDULER_ACTIVATE=authorized is required' >&2
  exit 1
fi

repo=/opt/chat-memory
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
service=memory-v1-v5-local-inference-scheduler.service
timer=memory-v1-v5-local-inference-scheduler.timer
service_source=ops/systemd/$service
timer_source=ops/systemd/$timer
canary_report=${MEMORY_V1_V5_2_EMPLOYMENT_CANARY_REPORT:-}
canary_report_sha=${MEMORY_V1_V5_2_EMPLOYMENT_CANARY_REPORT_SHA256:-}
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_scheduler_activate.lock

phase=initialization
run_id=
status_file=
timers_quiesced=0
units_installed=0
activation_committed=0
timer_state=$(mktemp /tmp/memory-v1-v5-2-scheduler-timers.XXXXXX)
unit_backup=$(mktemp -d /tmp/memory-v1-v5-2-scheduler-units.XXXXXX)
all_tables=$(mktemp /tmp/memory-v1-v5-2-scheduler-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-2-scheduler-before.XXXXXX)
after=$(mktemp /tmp/memory-v1-v5-2-scheduler-after.XXXXXX)
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

authenticated_health() {
  set -a
  source "$repo/.env"
  set +a
  test -n "${VS_SERVICE_TOKEN:-}"
  [[ "$(systemctl is-active brains.service)" == active ]]
  [[ "$(systemctl is-active memory-v1-v5-local-inference-tunnel.service)" == active ]]
  curl --fail --silent --show-error --max-time 10 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null
  curl --fail --silent --show-error --max-time 10 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/readyz \
    | jq -e '.ok==true and .postgres==true' >/dev/null
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
  sudo -n install -o root -g root -m 0644 "$unit_backup/$service" \
    "/etc/systemd/system/$service"
  sudo -n install -o root -g root -m 0644 "$unit_backup/$timer" \
    "/etc/systemd/system/$timer"
  sudo -n systemctl daemon-reload
  units_installed=0
}

record_exit() {
  exit_code=$?
  if [[ "$activation_committed" -eq 0 ]]; then
    restore_units || exit_code=1
  fi
  restore_timers || exit_code=1
  rm -rf "$unit_backup"
  rm -f "$timer_state" "$all_tables" "$before" "$after"
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

phase=source_preflight
cd "$repo"
[[ -z "$(git status --porcelain -- \
  "$service_source" "$timer_source" \
  scripts/memory_v1_v5_multi_owner_automation_contract_test.py \
  scripts/memory_v1_v5_local_inference_scheduler.py \
  scripts/memory_v1_v5_local_inference_canary.py \
  scripts/memory_v1_predicate_runtime_profile_v2.py \
  scripts/memory_v1_relational_extraction_v5_local_provider.py \
  scripts/memory_v1_relational_extraction_v5_provider.py \
  specs/memory_v1_predicate_runtime_profiles_v2.json \
  tests/test_memory_v1_v5_2_explicit_employment_guard.py \
  tests/test_memory_v1_v5_2_employment_normalization.py \
  tools/memory_v1_v5_local_inference_scheduler_production_clone.sh \
  tools/memory_v1_v5_2_employment_canary_production_clone.sh \
  tools/memory_v1_v5_2_scheduler_activate.sh)" ]]
sha256sum -c <<'HASHES'
f807a72cb851e66e3a4bb7dd108a1f25775b4c458bd9f563dd0a70cf3601ca6f  ops/systemd/memory-v1-v5-local-inference-scheduler.service
dc618a248d7103956ff2e671a2b08fa775f6e030bf383d4a68354382b38dc9a8  ops/systemd/memory-v1-v5-local-inference-scheduler.timer
da034629b1cc58ce942b34310731e04f70089dce829ea9b18908465849dd4440  scripts/memory_v1_v5_multi_owner_automation_contract_test.py
0da89022b24453a3617a7d633cb019bc42de9de69c44f0c71011ae29b92e148e  scripts/memory_v1_v5_local_inference_scheduler.py
05787c4e1bfc8a8f2107eef1ede4a07c6e105ef15ca58dbf036a85edeaa8b428  scripts/memory_v1_v5_local_inference_canary.py
a211620badb08eb9aee2e30f9cf0219a368cb37f2c487a0cdbd52ad77caa2b80  scripts/memory_v1_predicate_runtime_profile_v2.py
7dd13a2b7b9ac019324f70d68b085f07feb77b7f3432eea6e3dc1d4375ff7e37  scripts/memory_v1_relational_extraction_v5_local_provider.py
2b07b0503eb1d4d206697fe6a32607a89398f93498099e3af3276af3cd9d79f6  scripts/memory_v1_relational_extraction_v5_provider.py
5786269a2cda01045cc0f729ed2f7239da95da03e0dab2d074df761de80b24a4  specs/memory_v1_predicate_runtime_profiles_v2.json
64ebf4a4d019f1a9c114c4716c89a02d736e0ce251a6960a10ac35fbcfdb8903  tests/test_memory_v1_v5_2_explicit_employment_guard.py
b518132a9c322435dc51b71fac0eac322d4bc33ed1fdccfde3395de868293599  tests/test_memory_v1_v5_2_employment_normalization.py
062478e3e1a5506549bb03fe366c0dd1628ac45bdf838017e309cc3e4a86a06f  tools/memory_v1_v5_local_inference_scheduler_production_clone.sh
f62352b75f08b3b837639f25e8ee6956fe6ddbe77497dd38cd18024bdc6a347d  tools/memory_v1_v5_2_employment_canary_production_clone.sh
HASHES
PYTHONPATH="$repo" /opt/chat-memory/venv/bin/python \
  scripts/memory_v1_v5_multi_owner_automation_contract_test.py >/dev/null
PYTHONPATH="$repo" /opt/chat-memory/venv/bin/python \
  scripts/memory_v1_v5_local_inference_scheduler_test.py >/dev/null
PYTHONPATH="$repo" /opt/chat-memory/venv/bin/python -m unittest -q \
  tests.test_memory_v1_v5_2_explicit_employment_guard \
  tests.test_memory_v1_v5_2_employment_normalization
systemd-analyze verify "$service_source" "$timer_source"
[[ "$(grep -o -- '--contract-profile v5_2' "$service_source" | wc -l)" == 1 ]]
[[ "$(grep -o -- '--owner-user-id 1240822d-ac9a-4096-95aa-e2b24d36ef50' "$service_source" | wc -l)" == 1 ]]
[[ "$(grep -o -- '--max-reserved-jobs 12' "$service_source" | wc -l)" == 1 ]]
[[ "$(grep -o -- '--max-jobs 1' "$service_source" | wc -l)" == 1 ]]
[[ "$(grep -o -- '--failure-threshold 3' "$service_source" | wc -l)" == 1 ]]
[[ -f "$canary_report" && "$canary_report" == "$snapshot_dir/"* ]]
[[ "$canary_report_sha" =~ ^[0-9a-f]{64}$ ]]
[[ "$(sha256sum "$canary_report" | awk '{print $1}')" == "$canary_report_sha" ]]
jq -e '
  .contract_version=="memory_v1_v5_2_employment_canary_report_v1" and
  .outcome=="pass" and
  .owner_user_id_sha256=="9f5d6523a63c8ff7391ecf514fab572af530874832cddc9f56eb3db93cf45b15" and
  .evidence_id_sha256=="ef829ea1e297118abdae2e3474a6c7db9276b06070a7afcb35ce09b73fcf1c41" and
  .evidence_content_sha256=="48c3ea92b1c624634f20a54eb462c6953a11d700eeaa1a2b2cffa3c31f67929d" and
  .predicate_contract_profile=="v5_2" and
  .extraction_contract_version=="memory_v1_relational_extraction_v5_2" and
  .predicate_registry_version=="memory_predicate_registry_v5_2" and
  .policy_compiler_version=="memory_v1_semantic_policy_compiler_v6" and
  .policy_guard_code=="explicit_started_employment" and
  .local_model_calls==0 and .external_model_calls==0 and
  .counts.entity_mentions==2 and .counts.observations==1 and
  .observation_predicates==["employment.worked_for"] and
  .checks.self_entity==true and .checks.organization_entity==true and
  .checks.organization_name_exact==true and
  .checks.no_current_employment_invention==true and
  .checks.no_date_invention==true and
  .checks.account_isolation==true and .checks.qdrant_unchanged==true and
  (.production_write_counts|to_entries|map(.value==0)|all)
' "$canary_report" >/dev/null
authenticated_health
[[ "$(systemctl is-active "$timer")" == active ]]
[[ "$(systemctl is-enabled "$timer")" == enabled ]]
[[ "$(systemctl is-active "$service")" == inactive ]]
grep -q -- '--contract-profile v5_1' "/etc/systemd/system/$service"

exec 9>"$lock_file"
flock -n 9
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_2_scheduler_activate_${run_id}.status"
report="$snapshot_dir/memory_v1_v5_2_scheduler_activate_${run_id}.json"

phase=quiesce_timers
: >"$timer_state"
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ -s "$timer_state" ]]
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  sudo -n systemctl stop "$unit"
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
backup_partial="$snapshot_dir/.memory_pre_v5_2_scheduler_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_2_scheduler_${run_id}.dump"
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
" >"$all_tables"
[[ -s "$all_tables" ]]
capture_memory "$before"
qdrant_before=$(qdrant_signature)
cp "/etc/systemd/system/$service" "$unit_backup/$service"
cp "/etc/systemd/system/$timer" "$unit_backup/$timer"
chmod 0600 "$unit_backup/$service" "$unit_backup/$timer"

phase=install_v5_2_units
sudo -n install -o root -g root -m 0644 "$service_source" \
  "/etc/systemd/system/$service"
sudo -n install -o root -g root -m 0644 "$timer_source" \
  "/etc/systemd/system/$timer"
sudo -n systemctl daemon-reload
units_installed=1
cmp -s "$service_source" "/etc/systemd/system/$service"
cmp -s "$timer_source" "/etc/systemd/system/$timer"
grep -q -- '--contract-profile v5_2' "/etc/systemd/system/$service"
[[ "$(systemctl is-active "$timer")" == inactive ]]
[[ "$(systemctl is-active "$service")" == inactive ]]

phase=zero_data_change_verification
capture_memory "$after"
cmp -s "$before" "$after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=restore_timers_and_health
restore_timers
authenticated_health
[[ "$(systemctl is-enabled "$timer")" == enabled ]]
[[ "$(systemctl is-active "$timer")" == active ]]
grep -q -- '--contract-profile v5_2' "/etc/systemd/system/$service"

phase=report
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg canary_report "$canary_report" \
  --arg canary_report_sha256 "$canary_report_sha" \
  --arg service_sha256 "$(sha256sum "$service_source" | awk '{print $1}')" \
  --arg timer_sha256 "$(sha256sum "$timer_source" | awk '{print $1}')" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{contract_version:"memory_v1_v5_2_scheduler_activation_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    backup:{path:$backup,sha256:$backup_sha256},
    employment_canary:{path:$canary_report,sha256:$canary_report_sha256},
    scheduler:{profile:"v5_2",owner_count:1,max_jobs_per_cycle:1,
      rolling_24h_quota:12,failure_threshold:3,
      service_sha256:$service_sha256,timer_sha256:$timer_sha256},
    checks:{fresh_backup:true,hash_locked_sources:true,
      employment_canary_passed:true,private_local_inference_only:true,
      memory_rows_changed:0,qdrant_unchanged:true,claims_written:0,
      retrieval_changes:0,prompt_influence:0,other_owner_processing:0,
      timer_states_restored:true,service_healthy:true},
    qdrant_sha256:$qdrant_sha256,
    hard_stop:"before_v5_2_packet_review_stage_claim_projection_retrieval_or_prompt_influence"}' \
  >"$report"
chmod 0600 "$report"
jq -e '.checks|to_entries|map(.value==true or .value==0)|all' \
  "$report" >/dev/null
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

activation_committed=1
units_installed=0
phase=complete
printf '%s\n' 'memory_v1_v5_2_scheduler_activate: PASS'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
