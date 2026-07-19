#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the tested zero-call terminal-deferral
# compatibility patch. The live test is rollback-only and writes no rows.

if [[ "${MEMORY_V1_V5_ZERO_CALL_COMPAT_INSTALL:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_ZERO_CALL_COMPAT_INSTALL=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
review_root=/home/ubuntu/memory-v1-reviews
owner=557ea042-cb82-48f8-9429-472e96c957ef
other=d839b4bc-0bd2-4f2d-aafe-0f3f75883db8
packet=70dc2e9e-27fa-588d-a54b-966c466949af
migration=ops/sql/20260719_memory_v1_v5_local_packet_disposition_zero_call_compat.sql
rollback=ops/sql/20260719_memory_v1_v5_local_packet_disposition_zero_call_compat_rollback.sql
test_sql=tests/memory_v1_v5_local_packet_disposition_zero_call_compat.sql
clone_test=tools/memory_v1_v5_local_packet_disposition_zero_call_compat_clone.sh
lock_file=/home/ubuntu/brains/.memory_v1_v5_zero_call_compat_install.lock

declare -A expected_sha256=(
  ["$migration"]="decfc0da3006c17b85b06983191aaff83446b6b7244a2a7aaebe9d4731011dbd"
  ["$rollback"]="3ce35917851cc8abdeaf2c78a911e377802bf2a11a6804a420719b57f67ddfad"
  ["$test_sql"]="20c57a1a85c816027cb07c8be64b484c6eb8ae97cc3519aa390a28e71a613854"
  ["$clone_test"]="36ce98843799ee878f42b4e49ddaa6dd7c60d0ce9eca8d56e4c9db76b6e24b76"
)

phase=initialization
run_tag=
status_file=
timers_quiesced=0
schema_installed=0
installation_committed=0
timer_state=$(mktemp /tmp/memory-v1-v5-zero-call-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-zero-call-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-zero-call-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-v5-zero-call-after.XXXXXX.tsv)
chmod 0600 "$timer_state" "$table_list" "$before" "$after"

run_sql() {
  docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
    -U sage -d "$database" "$@"
}

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
  if [[ "$schema_installed" -eq 1 && "$installation_committed" -eq 0 ]]; then
    run_sql <"$rollback" >/dev/null 2>&1 || exit_code=1
  fi
  restore_timers || exit_code=1
  rm -f "$timer_state" "$table_list" "$before" "$after"
  if [[ -n "$status_file" ]]; then
    {
      printf 'run_tag=%s\n' "$run_tag"
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$exit_code"
      printf 'schema_installed=%s\n' "$schema_installed"
      printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$exit_code"
}
trap record_exit EXIT

capture_rows() {
  local output=$1 schema table state
  : >"$output"
  while IFS=$'\t' read -r schema table; do
    state=$(scalar "SELECT count(*)::text || ':' ||
      encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
        ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
      FROM (SELECT to_jsonb(value)::text AS row_json
        FROM \"$schema\".\"$table\" AS value) rows")
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
[[ "$(scalar "SELECT pg_get_constraintdef(oid)
  FROM pg_constraint
  WHERE conrelid='memory.v5_local_packet_disposition'::regclass
    AND conname='v5_local_packet_disposition_local_model_calls_check'")" \
  == 'CHECK((local_model_calls=1))' ]]

phase=clone_verification
"$clone_test" >/dev/null

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_zero_call_compat_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_zero_call_compat_${run_tag}.json"

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
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
done <"$timer_state"

phase=backup
backup_partial="$snapshot_dir/.memory_pre_v5_zero_call_compat_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_zero_call_compat_${run_tag}.dump"
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
  "SELECT table_schema || E'\\t' || table_name
   FROM information_schema.tables
   WHERE table_type='BASE TABLE' AND table_schema IN ('memory','public')
   ORDER BY table_schema,table_name" >"$table_list"
capture_rows "$before"
qdrant_before=$(qdrant_signature)
review_before=$(review_root_signature)
rows_before=$(scalar 'SELECT count(*) FROM memory.v5_local_packet_disposition')
claims_before=$(scalar 'SELECT count(*) FROM memory.claim')
packet_sha=$(scalar "SELECT packet_storage_sha256
  FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
    AND local_model_calls=0 AND external_model_calls=0")
[[ "$packet_sha" =~ ^[0-9a-f]{64}$ ]]

phase=install_schema
run_sql <"$migration" >/dev/null
schema_installed=1

phase=rollback_only_security_test
run_sql -v owner_user_id="$owner" -v other_owner_user_id="$other" \
  -v packet_id="$packet" -v packet_storage_sha256="$packet_sha" \
  <"$test_sql" >/dev/null
[[ "$(scalar 'SELECT count(*) FROM memory.v5_local_packet_disposition')" == "$rows_before" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.claim')" == "$claims_before" ]]

phase=postflight
[[ "$(scalar "SELECT pg_get_constraintdef(oid) LIKE '%local_model_calls >= 0%'
  AND pg_get_constraintdef(oid) LIKE '%local_model_calls <= 1%'
  FROM pg_constraint
  WHERE conrelid='memory.v5_local_packet_disposition'::regclass
    AND conname='v5_local_packet_disposition_local_model_calls_check'")" == t ]]
[[ "$(scalar "SELECT strpos(pg_get_functiondef(
  'memory.finalize_owner_v5_local_deferral_v1(uuid,uuid,uuid,text,text)'::regprocedure
  ),'packet.local_model_calls NOT BETWEEN 0 AND 1')>0")" == t ]]
[[ "$(scalar "SELECT pg_get_userbyid(proowner)='sage'
  FROM pg_proc WHERE oid=
  'memory.finalize_owner_v5_local_deferral_v1(uuid,uuid,uuid,text,text)'::regprocedure")" == t ]]
[[ "$(scalar "SELECT has_schema_privilege('brains_app','memory','USAGE')
  AND has_function_privilege('brains_app',
    'memory.plan_owner_v5_local_packet_disposition_v1(integer)','EXECUTE')
  AND has_function_privilege('brains_app',
    'memory.finalize_owner_v5_local_deferral_v1(uuid,uuid,uuid,text,text)','EXECUTE')
  AND NOT has_function_privilege('public',
    'memory.finalize_owner_v5_local_deferral_v1(uuid,uuid,uuid,text,text)','EXECUTE')")" == t ]]
capture_rows "$after"
cmp -s "$before" "$after"
[[ "$(review_root_signature)" == "$review_before" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=restore_timers
restore_timers
installation_committed=1

phase=report
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{contract_version:"memory_v1_v5_zero_call_disposition_compat_install_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    backup:{path:$backup,sha256:$backup_sha256},
    checks:{clone_verification:true,fresh_backup:true,hash_locked_inputs:true,
      exact_timer_restoration:true,rollback_only_live_security_test:true,
      zero_memory_row_changes:true,owner_isolation:true,restricted_acl:true,
      claims_unchanged:true,qdrant_unchanged:true,review_files_unchanged:true},
    qdrant_sha256:$qdrant_sha256,
    hard_stop:"before_terminal_disposition_apply_or_any_downstream_stage"}' \
  >"$report"
chmod 0600 "$report"
jq -e '.checks|to_entries|map(.value==true)|all' "$report" >/dev/null
sha256sum "$report" >"$report.sha256"

phase=complete
printf '%s\n' 'memory_v1_v5_local_packet_disposition_zero_call_compat_install: PASS'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
