#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Replaces the already-installed local-inference functions
# with the deterministic legacy-source compatibility rule. No table data is
# intentionally changed; the security suite runs inside rollback transactions.

if [[ "${MEMORY_V1_V5_LOCAL_SOURCE_ID_COMPAT_APPLY:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_LOCAL_SOURCE_ID_COMPAT_APPLY=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
migration=ops/sql/20260718_memory_v1_v5_local_inference.sql
test_sql=tests/memory_v1_v5_local_inference.sql
expected_migration_sha=3db6e453f3becf76bc84b28e9908c17cdc2d1a8327b0bc165e663f4652863ab5
expected_test_sha=cbd64ecaeb593e84ec4aacf150b5de4aba9e87f6819659ebe9425c81e75061d9
target_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
target_job=16c36c01-f052-4a49-abbb-cfb2a47b50d4
target_evidence=00614247-c79d-5c63-9c37-d53419118b06
target_content_sha=6886231333bed756f3643f81acecf3644c69b5e3f6ffc07f6b8edbaadf83f3c7
target_run=bd4a7d2a-bb65-4cb6-88b9-08b37e47b184
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_local_source_id_compat.lock
phase=initialization
run_tag=
status_file=
units_quiesced=0
unit_state=$(mktemp /tmp/memory-v1-v5-local-source-compat-units.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-local-source-compat-tables.XXXXXX)

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
}

run_sql_file() {
  docker exec -e PGOPTIONS='-c lock_timeout=5s -c statement_timeout=180s' \
    -i "$container" psql -X -v ON_ERROR_STOP=1 -U sage -d "$database" \
    <"$repo_root/$1"
}

restore_timers() {
  [[ "$units_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$unit_state"
  units_quiesced=0
}

record_exit() {
  exit_code=$?
  restore_timers || exit_code=1
  rm -f "$unit_state" "$table_list"
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

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

capture_state() {
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
  done <"$table_list"
  chmod 0600 "$output"
}

for file in "$migration" "$test_sql"; do
  [[ -f "$repo_root/$file" ]]
done
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
[[ "$(sha256sum "$repo_root/$migration" | awk '{print $1}')" == "$expected_migration_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | awk '{print $1}')" == "$expected_test_sha" ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_local_source_id_compat_${run_tag}.status"
before="$snapshot_dir/memory_v1_v5_local_source_id_compat_before_${run_tag}.tsv"
after="$snapshot_dir/memory_v1_v5_local_source_id_compat_after_${run_tag}.tsv"
log="$snapshot_dir/memory_v1_v5_local_source_id_compat_${run_tag}.log"

phase=preflight
[[ "$(psql_scalar "SELECT CASE WHEN EXISTS (
  SELECT 1
  FROM memory.evidence_extraction_job AS job
  JOIN memory.evidence AS evidence USING(owner_user_id,evidence_id)
  JOIN memory.v5_local_inference_event AS event
    ON event.owner_user_id=job.owner_user_id AND event.job_id=job.job_id
  WHERE job.owner_user_id='$target_owner'::uuid
    AND job.job_id='$target_job'::uuid
    AND job.evidence_id='$target_evidence'::uuid
    AND job.evidence_content_sha256='$target_content_sha'
    AND job.status='skipped' AND job.attempts=1
    AND job.lease_token IS NULL AND job.lease_expires_at IS NULL
    AND job.last_error='local_inference_rejected: local_transport_timeout'
    AND evidence.status='active'
    AND event.action='completed' AND event.run_id='$target_run'::uuid
    AND event.outcome='rejected'
    AND event.rejection_code='local_transport_timeout'
    AND event.local_model_calls=1 AND event.external_model_calls=0
) AND NOT EXISTS (
  SELECT 1 FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$target_owner'::uuid AND job_id='$target_job'::uuid
) THEN 1 ELSE 0 END")" == 1 ]]

: >"$unit_state"
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$unit_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ -s "$unit_state" ]]
units_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  sudo -n systemctl stop "$unit"
done <"$unit_state"
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
done <"$unit_state"

phase=backup
backup_partial="$snapshot_dir/.memory_pre_v5_local_source_id_compat_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_local_source_id_compat_${run_tag}.dump"
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
psql_scalar "SELECT table_name FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
  ORDER BY table_name" >"$table_list"
capture_state "$before"
qdrant_before=$(qdrant_signature)

phase=function_compatibility_install
run_sql_file "$migration" >"$log" 2>&1

phase=rollback_only_security_test
run_sql_file "$test_sql" >>"$log" 2>&1
chmod 0600 "$log"

phase=postflight
capture_state "$after"
cmp -s "$before" "$after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]
[[ "$(psql_scalar "SELECT (pg_get_functiondef(
  'memory.persist_owner_v5_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure
) LIKE '%ELSE evidence_record.evidence_id::text%')::integer")" == 1 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$target_owner'::uuid AND job_id='$target_job'::uuid")" == 0 ]]

restore_timers

phase=report
report="$snapshot_dir/memory_v1_v5_local_source_id_compat_${run_tag}.json"
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git -C "$repo_root" rev-parse HEAD)" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg before "$before" --arg after "$after" --arg log "$log" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{contract_version:"memory_v1_v5_local_source_id_compat_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    backup:{path:$backup,sha256:$backup_sha256},
    evidence:{before:$before,after:$after,log:$log,qdrant_sha256:$qdrant_sha256},
    checks:{fresh_backup:true,functions_only:true,legacy_source_binding:true,
      rollback_only_security_test:true,memory_rows_unchanged:true,
      qdrant_unchanged:true,timers_restored:true,external_model_calls:0},
    hard_stop:"before_exact_canary_resume_or_any_promotion_or_live_retrieval"}' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf 'memory_v1_v5_local_source_id_compat_production_apply: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
