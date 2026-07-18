#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Read-only attestation for a completed local-packet
# disposition run whose original final report validator rejected numeric zero.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
packet=50c8fb51-f847-5423-b9b6-285c79703270
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots

report=${1:-$(ls -1t \
  "$snapshot_dir"/memory_v1_v5_local_disposition_*.json | head -1)}
[[ -f "$report" ]]
status=${report%.json}.status
[[ -f "$status" ]]
attestation=${report%.json}.postverify.json
[[ ! -e "$attestation" ]]

scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | tr -d '[:space:]'
}

jq -e '
  .contract_version=="memory_v1_v5_local_packet_disposition_install_report_v1" and
  .result.dispositions_written==1 and
  .result.stage_rows_written==0 and .result.claims_written==0 and
  .result.qdrant_writes==0 and .result.prompt_influence==0 and
  .result.external_model_calls==0 and
  .checks.hash_locked_inputs==true and .checks.fresh_backup==true and
  .checks.rollback_only_security_test==true and
  .checks.owner_isolation==true and .checks.other_owner_rows_written==0 and
  .checks.zero_write_replay==true and
  .checks.existing_tables_unchanged==true and
  .checks.qdrant_unchanged==true and .checks.timers_restored_exactly==true
' "$report" >/dev/null
grep -qx 'phase=report' "$status"
grep -qx 'exit_code=1' "$status"
grep -qx 'schema_installed=1' "$status"
grep -qx 'data_written=1' "$status"

backup=$(jq -r '.backup.path' "$report")
backup_sha=$(jq -r '.backup.sha256' "$report")
[[ -f "$backup" && -f "$backup.sha256" && -f "$backup.catalog" ]]
[[ "$(sha256sum "$backup" | awk '{print $1}')" == "$backup_sha" ]]
sha256sum -c "$backup.sha256" >/dev/null

[[ "$(scalar "
  SELECT count(*) FROM memory.v5_local_packet_disposition
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
    AND disposition='terminal_no_stage'
    AND reason_code='deferral_only_no_stage'
")" == 1 ]]
[[ "$(scalar "
  SELECT count(*) FROM memory.v5_local_packet_disposition
  WHERE owner_user_id<>'$owner'::uuid
")" == 0 ]]
[[ "$(scalar "
  SELECT relrowsecurity::int || '|' || relforcerowsecurity::int
  FROM pg_class WHERE oid='memory.v5_local_packet_disposition'::regclass
")" == '1|1' ]]
[[ "$(scalar "
  SELECT has_table_privilege(
    'brains_app','memory.v5_local_packet_disposition','SELECT'
  )::int || '|' || has_table_privilege(
    'brains_app','memory.v5_local_packet_disposition','INSERT'
  )::int
")" == '0|0' ]]
[[ "$(scalar "
  SELECT has_function_privilege(
    'brains_app',
    'memory.finalize_owner_v5_local_deferral_v1(uuid,uuid,uuid,text,text)',
    'EXECUTE'
  )::int
")" == 1 ]]

timer_count=0
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  case "$unit" in
    memory-v1-consolidation.timer)
      [[ "$(systemctl is-enabled "$unit")" == disabled ]]
      [[ "$(systemctl is-active "$unit")" == inactive ]]
      ;;
    *)
      [[ "$(systemctl is-enabled "$unit")" == enabled ]]
      [[ "$(systemctl is-active "$unit")" == active ]]
      ;;
  esac
  timer_count=$((timer_count + 1))
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ "$timer_count" -eq 7 ]]

report_sha=$(sha256sum "$report" | awk '{print $1}')
status_sha=$(sha256sum "$status" | awk '{print $1}')
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg original_report "$report" --arg original_report_sha256 "$report_sha" \
  --arg original_status "$status" --arg original_status_sha256 "$status_sha" \
  '{contract_version:"memory_v1_v5_local_packet_disposition_postverify_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    original_report:{path:$original_report,sha256:$original_report_sha256},
    original_status:{path:$original_status,sha256:$original_status_sha256},
    interpretation:"original run completed; validator rejected numeric zero",
    checks:{backup_verified:true,one_target_disposition:true,
      zero_other_owner_dispositions:true,forced_rls:true,
      function_only_application_access:true,timers_restored:true,
      original_bounded_state_report_verified:true}}' >"$attestation"
chmod 0600 "$attestation"
jq -e '.checks|to_entries|map(.value)|all' "$attestation" >/dev/null
printf '%s\n' "$attestation"
