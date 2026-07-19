#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the accepted-relational review-artifact
# compatibility patch. Live security writes are contained by rollback.

if [[ "${MEMORY_V1_V5_RELATIONAL_REVIEW_ADMISSION_INSTALL:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_RELATIONAL_REVIEW_ADMISSION_INSTALL=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
review_root=/home/ubuntu/memory-v1-reviews
target_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
migration=ops/sql/20260719_memory_v1_v5_relational_review_admission_compat.sql
rollback=ops/sql/20260719_memory_v1_v5_relational_review_admission_compat_rollback.sql
test_sql=tests/memory_v1_v5_relational_review_admission_compat.sql
clone_test=tools/memory_v1_v5_relational_packet_review_clone.sh
lock_file=/home/ubuntu/brains/.memory_v1_v5_relational_review_admission.lock

declare -A expected_sha256=(
  ["$migration"]="b0e0ac678428a6398aff0d0f11bca8e650f999a360f0dd3240d14a8ca3736f3d"
  ["$rollback"]="b93f56a6125db677096904d7f780ff187380e64d16ee3457fca09805a335182b"
  ["$test_sql"]="40c8e4ce52b4db98fda545c29e699f3317d732afe99f6aeee20630cbc519120d"
  ["$clone_test"]="841f688704af53102937d1f40f14b936b34eedf125efed3fdce344c82b9de522"
)

phase=initialization
run_tag=
status_file=
timers_quiesced=0
schema_installed=0
installation_committed=0
timer_state=$(mktemp /tmp/memory-v1-review-admission-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-review-admission-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-review-admission-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-review-admission-after.XXXXXX.tsv)
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

phase=clone_verification
"$clone_test" >/dev/null

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_relational_review_admission_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_relational_review_admission_${run_tag}.json"

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
backup_partial="$snapshot_dir/.memory_pre_v5_relational_review_admission_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_relational_review_admission_${run_tag}.dump"
docker exec "$container" pg_dump -U sage -d "$database" -Fc >"$backup_partial"
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
target_packet=$(scalar "SELECT packet.packet_id
  FROM memory.evidence_extraction_packet_v5_local AS packet
  JOIN memory.evidence_extraction_job AS job
    ON job.owner_user_id=packet.owner_user_id AND job.job_id=packet.job_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=packet.owner_user_id
   AND evidence.evidence_id=packet.evidence_id
  WHERE packet.owner_user_id='$target_owner'::uuid
    AND packet.provider_id='local_llama_cpp'
    AND packet.local_model_calls=1 AND packet.external_model_calls=0
    AND NOT packet.manual_review_required
    AND (packet.entity_mention_count>0 OR packet.observation_count>0
      OR packet.comparison_hint_count>0)
    AND job.status='review_required' AND job.route='relational_extraction'
    AND job.lease_token IS NULL AND job.lease_expires_at IS NULL
    AND job.last_error IS NULL AND evidence.status='active'
    AND NOT EXISTS (SELECT 1 FROM memory.relational_stage_batch stage
      WHERE stage.owner_user_id=packet.owner_user_id
        AND stage.evidence_id=packet.evidence_id)
    AND NOT EXISTS (SELECT 1 FROM memory.v5_local_packet_review_artifact artifact
      WHERE artifact.owner_user_id=packet.owner_user_id
        AND artifact.packet_id=packet.packet_id)
  ORDER BY packet.created_at,packet.packet_id LIMIT 1")
[[ "$target_packet" =~ ^[0-9a-f-]{36}$ ]]

phase=install_compatibility
run_sql <"$migration" >/dev/null
schema_installed=1

phase=rollback_only_security_test
run_sql -v owner_user_id="$target_owner" -v packet_id="$target_packet" \
  <"$test_sql" >/dev/null

phase=postflight
[[ "$(scalar "SELECT strpos(pg_get_functiondef(
  'memory.record_owner_v5_local_review_artifact_v1(uuid,uuid,uuid,text,uuid,uuid,text,text,text,integer,integer,integer,integer,integer)'::regprocedure
  ),'NOT packet.manual_review_required')=0")" == t ]]
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
  '{contract_version:"memory_v1_v5_relational_review_admission_install_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    backup:{path:$backup,sha256:$backup_sha256},
    checks:{clone_verification:true,fresh_backup:true,hash_locked_inputs:true,
      exact_timer_restoration:true,rollback_only_live_security_test:true,
      zero_memory_row_changes:true,owner_isolation:true,
      controlled_writer_preserved:true,qdrant_unchanged:true,
      review_files_unchanged:true},
    qdrant_sha256:$qdrant_sha256,
    hard_stop:"before_live_relational_review_artifact_apply"}' \
  >"$report"
chmod 0600 "$report"
jq -e '.checks|to_entries|map(.value==true)|all' "$report" >/dev/null
sha256sum "$report" >"$report.sha256"

phase=complete
printf '%s\n' 'memory_v1_v5_relational_review_admission_production_install: PASS'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
