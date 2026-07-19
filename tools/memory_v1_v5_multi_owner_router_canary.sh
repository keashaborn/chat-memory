#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Routes exactly one accepted local packet for one of the
# two user-owned test accounts to a terminal deferral. No stage, claim, vector,
# prompt, review-file, or external-model write is permitted.

if [[ "${MEMORY_V1_V5_MULTI_OWNER_ROUTER_CANARY:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_MULTI_OWNER_ROUTER_CANARY=authorized is required' >&2
  exit 1
fi
if [[ "$#" -ne 1 ]]; then
  echo 'usage: memory_v1_v5_multi_owner_router_canary.sh OWNER_UUID' >&2
  exit 1
fi

target=$1
case "$target" in
  557ea042-cb82-48f8-9429-472e96c957ef|d839b4bc-0bd2-4f2d-aafe-0f3f75883db8) ;;
  *) echo 'target is not an approved test owner' >&2; exit 1 ;;
esac

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
review_root=/home/ubuntu/memory-v1-reviews
router=scripts/memory_v1_v5_local_packet_router.py
disposition=scripts/memory_v1_v5_local_packet_disposition.py
service=ops/systemd/memory-v1-v5-local-packet-router.service
lock_file=/home/ubuntu/brains/.memory_v1_v5_multi_owner_router_canary.lock

declare -A expected_sha256=(
  ["$router"]="08f7434514805a17897447b89235efa6e3495a59795016ce66ce5c5855afa9a5"
  ["$disposition"]="b63a63a5e352484b3ad010e1a849de2ba77043e249adf3cad5c5e4ba4dd09cb6"
  ["$service"]="29440f8cf7cbb93d556441b5b564d6acbff17dcdbf362b245a2115350ea5e48c"
)

phase=initialization
run_tag=
status_file=
timers_quiesced=0
timer_state=$(mktemp /tmp/memory-v1-v5-multi-router-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-multi-router-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-multi-router-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-v5-multi-router-after.XXXXXX.tsv)
output=$(mktemp /tmp/memory-v1-v5-multi-router-output.XXXXXX.json)
chmod 0600 "$timer_state" "$table_list" "$before" "$after" "$output"

scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | tr -d '[:space:]'
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
  restore_timers || exit_code=1
  rm -f "$timer_state" "$table_list" "$before" "$after" "$output"
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

capture_non_target() {
  local destination=$1 schema table scoped state
  : >"$destination"
  while IFS=$'\t' read -r schema table scoped; do
    if [[ "$scoped" == t ]]; then
      state=$(scalar "SELECT count(*)::text || ':' ||
        encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
          ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
        FROM (SELECT to_jsonb(value)::text AS row_json
          FROM \"$schema\".\"$table\" AS value
          WHERE owner_user_id::text IS DISTINCT FROM '$target') rows")
    else
      state=$(scalar "SELECT count(*)::text || ':' ||
        encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
          ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
        FROM (SELECT to_jsonb(value)::text AS row_json
          FROM \"$schema\".\"$table\" AS value) rows")
    fi
    printf '%s\t%s\t%s\n' "$schema" "$table" "$state" >>"$destination"
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
  (cd "$review_root" && find . -type f -print0 | sort -z \
    | xargs -0r sha256sum) | sha256sum | awk '{print $1}'
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
target_sha=$(printf %s "$target" | sha256sum | awk '{print $1}')
base="$snapshot_dir/memory_v1_v5_multi_owner_router_canary_${run_tag}_${target_sha:0:12}"
status_file="$base.status"
report="$base.json"
preserved_output="$base.output.json"

phase=quiesce_timers
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
  existing_service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$existing_service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$existing_service"
done <"$timer_state"

phase=backup
backup_partial="$snapshot_dir/.memory_pre_v5_multi_owner_router_${run_tag}_${target_sha:0:12}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_multi_owner_router_${run_tag}_${target_sha:0:12}.dump"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup"
backup_sha=$(sha256sum "$backup" | awk '{print $1}')
printf '%s  %s\n' "$backup_sha" "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline
docker exec "$container" psql -X -A -t -U sage -d "$database" -c \
  "SELECT t.table_schema || E'\\t' || t.table_name || E'\\t' ||
     CASE WHEN EXISTS (SELECT 1 FROM information_schema.columns c
       WHERE c.table_schema=t.table_schema AND c.table_name=t.table_name
         AND c.column_name='owner_user_id') THEN 't' ELSE 'f' END
   FROM information_schema.tables t
   WHERE t.table_type='BASE TABLE' AND t.table_schema IN ('memory','public')
   ORDER BY t.table_schema,t.table_name" >"$table_list"
capture_non_target "$before"
qdrant_before=$(qdrant_signature)
review_before=$(review_root_signature)
claims_before=$(scalar 'SELECT count(*) FROM memory.claim')
dispositions_before=$(scalar "SELECT count(*) FROM memory.v5_local_packet_disposition WHERE owner_user_id='$target'::uuid")
artifacts_before=$(scalar "SELECT count(*) FROM memory.v5_local_packet_review_artifact WHERE owner_user_id='$target'::uuid")
stage_before=$(scalar "SELECT count(*) FROM memory.v5_local_packet_stage_admission WHERE owner_user_id='$target'::uuid")

phase=route
MEMORY_V1_V5_LOCAL_PACKET_ROUTER_APPLY=memory_v1_v5_local_packet_router_apply_v1 \
PYTHONPATH=/opt/chat-memory \
  venv/bin/python "$router" --owner-user-id "$target" --apply >"$output"
jq -e --arg owner_sha "$target_sha" '
  .worker_version=="memory_v1_v5_local_packet_router_v1" and
  .apply==true and .outcome=="terminal_no_stage" and
  (.plans|length)==1 and .plans[0].owner_user_id_sha256==$owner_sha and
  .plans[0].route=="terminal_deferral" and
  .write_counts.packet_route_events==1 and
  .write_counts.restricted_review_artifacts==0 and
  .write_counts.stage==0 and .write_counts.claims==0 and
  .write_counts.qdrant==0 and .write_counts.prompt_influence==0 and
  .zero_write_replay_proved==true and .external_model_calls==0
' "$output" >/dev/null
cp "$output" "$preserved_output"
chmod 0600 "$preserved_output"

phase=postflight
[[ "$(scalar "SELECT count(*) FROM memory.v5_local_packet_disposition WHERE owner_user_id='$target'::uuid")" -eq $((dispositions_before+1)) ]]
[[ "$(scalar "SELECT count(*) FROM memory.v5_local_packet_review_artifact WHERE owner_user_id='$target'::uuid")" == "$artifacts_before" ]]
[[ "$(scalar "SELECT count(*) FROM memory.v5_local_packet_stage_admission WHERE owner_user_id='$target'::uuid")" == "$stage_before" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.claim')" == "$claims_before" ]]
capture_non_target "$after"
cmp -s "$before" "$after"
[[ "$(review_root_signature)" == "$review_before" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=restore_timers
restore_timers

phase=report
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg target_owner_sha256 "$target_sha" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg output "$preserved_output" --arg qdrant_sha256 "$qdrant_after" \
  '{contract_version:"memory_v1_v5_multi_owner_router_canary_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    target_owner_user_id_sha256:$target_owner_sha256,
    backup:{path:$backup,sha256:$backup_sha256},sanitized_output:$output,
    checks:{fresh_backup:true,exactly_one_terminal_disposition:true,
      zero_write_replay:true,non_target_rows_unchanged:true,
      claims_unchanged:true,qdrant_unchanged:true,prompt_influence_zero:true,
      no_stage_or_review_artifact:true,review_files_unchanged:true,
      external_model_calls_zero:true,existing_timers_restored:true},
    qdrant_sha256:$qdrant_sha256,
    hard_stop:"before_staging_entity_resolution_claims_qdrant_or_prompt_influence"}' \
  >"$report"
chmod 0600 "$report"
jq -e '.checks|to_entries|map(.value==true)|all' "$report" >/dev/null
sha256sum "$report" >"$report.sha256"

phase=complete
printf '%s\n' 'memory_v1_v5_multi_owner_router_canary: PASS'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
