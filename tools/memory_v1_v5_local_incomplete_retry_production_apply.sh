#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs one additive restricted retry function, proves it
# with rollback-only fixtures, and requeues one hash-bound owner-scoped job.
# No model, packet, claim, Qdrant, retrieval, or prompt write occurs here.

if [[ "${MEMORY_V1_V5_LOCAL_INCOMPLETE_RETRY_APPLY:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_LOCAL_INCOMPLETE_RETRY_APPLY=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
migration=ops/sql/20260722_memory_v1_v5_local_incomplete_retry.sql
test_sql=tests/memory_v1_v5_local_incomplete_retry.sql
manifest=ops/manifests/memory_v1_v5_2_incomplete_retry_20260722.json
expected_migration_sha=92644619ff8337dfe9d8b9607878f5d4a38c20dfa51ccc2bf0ac5d1f048ed104
expected_test_sha=b0cb9b454af318bfb93f4d067ac82c3d51d840b95cd38655c30f18d74290b4b1
expected_manifest_sha=29d65a8e8b897b5322ede22da0dc404c9b38b98075b05a8fe0297b06b0b106ed
required_ancestor=6d0e60a530c8b703633332e2864e5b45c43360d5
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
job=20ba21a9-855b-4606-93fb-28065e2e13d1
content_sha=70a0cef5245b571bc8fceaa4c744cc273b488bbacac08acb2718f831a2b920a1
prior_failure=032ac5c6-d969-5211-ac7c-bd20ca00f7e0
local_completion=0014b4cf-ea78-4db3-a79a-291995bb2237
operation=eeb053e5-7737-46d8-8349-b5f6ce7909e9
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_local_incomplete_retry_apply.lock
phase=initialization
run_tag=
status_file=
units_quiesced=0
unit_state=$(mktemp /tmp/memory-v1-v5-local-incomplete-retry-units.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-local-incomplete-retry-tables.XXXXXX)

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
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

capture_protected_state() {
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

isolation_signature() {
  psql_scalar "
    WITH rows AS (
      SELECT 'other_owner_job' AS lane,to_jsonb(value)::text AS row_json
      FROM memory.evidence_extraction_job AS value
      WHERE owner_user_id<>'$owner'::uuid
      UNION ALL
      SELECT 'other_owner_event',to_jsonb(value)::text
      FROM memory.evidence_extraction_event AS value
      WHERE owner_user_id<>'$owner'::uuid
      UNION ALL
      SELECT 'same_owner_other_job',to_jsonb(value)::text
      FROM memory.evidence_extraction_job AS value
      WHERE owner_user_id='$owner'::uuid AND job_id<>'$job'::uuid
      UNION ALL
      SELECT 'same_owner_other_event',to_jsonb(value)::text
      FROM memory.evidence_extraction_event AS value
      WHERE owner_user_id='$owner'::uuid AND job_id<>'$job'::uuid
    )
    SELECT encode(public.digest(convert_to(coalesce(string_agg(
      lane||':'||row_json,E'\\n' ORDER BY lane,row_json),''),'UTF8'),
      'sha256'),'hex') FROM rows
  "
}

run_retry() {
  local output=$1
  psql "$POSTGRES_DSN" -X -q -A -t -F '|' -v ON_ERROR_STOP=1 \
    >"$output" <<SQL
BEGIN;
SELECT set_config('app.user_id','$owner',true);
SELECT * FROM memory.requeue_owner_local_incomplete_failure_v1(
  '$operation','$job','$content_sha','$prior_failure','$local_completion',1,
  'bounded_output_ceiling_increase'
);
COMMIT;
SQL
  chmod 0600 "$output"
}

for required in "$migration" "$test_sql" "$manifest"; do
  [[ -f "$repo_root/$required" ]]
done
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(sha256sum "$repo_root/$migration" | cut -d' ' -f1)" == "$expected_migration_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | cut -d' ' -f1)" == "$expected_test_sha" ]]
[[ "$(sha256sum "$repo_root/$manifest" | cut -d' ' -f1)" == "$expected_manifest_sha" ]]
[[ "$(jq -r '.owner_user_id' "$repo_root/$manifest")" == "$owner" ]]
[[ "$(jq -r '.job_id' "$repo_root/$manifest")" == "$job" ]]
[[ "$(jq -r '.requeue_operation_id' "$repo_root/$manifest")" == "$operation" ]]
[[ "$(jq -r '.canary_max_output_tokens' "$repo_root/$manifest")" == 8192 ]]

exec 9>"$lock_file"
flock -n 9
umask 077
set -a
source "$repo_root/.env"
set +a
[[ -n "${POSTGRES_DSN:-}" ]]
[[ "$(psql "$POSTGRES_DSN" -X -A -t -v ON_ERROR_STOP=1 \
  -c 'SELECT session_user')" == brains_app ]]

run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_local_incomplete_retry_${run_tag}.status"
protected_before="$snapshot_dir/memory_v1_v5_local_incomplete_retry_protected_before_${run_tag}.tsv"
protected_after="$snapshot_dir/memory_v1_v5_local_incomplete_retry_protected_after_${run_tag}.tsv"
apply_output="$snapshot_dir/memory_v1_v5_local_incomplete_retry_apply_${run_tag}.tsv"
replay_output="$snapshot_dir/memory_v1_v5_local_incomplete_retry_replay_${run_tag}.tsv"

phase=preflight
[[ "$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid
    AND evidence_content_sha256='$content_sha' AND status='skipped'
    AND attempts=1 AND lease_token IS NULL AND lease_expires_at IS NULL
    AND last_error='local_inference_rejected: local_incomplete_response'")" == 1 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_event
  WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid
    AND operation_id='$prior_failure'::uuid AND event_type='skipped'")" == 1 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.v5_local_inference_event
  WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid
    AND event_id='$local_completion'::uuid AND action='completed'
    AND outcome='rejected' AND rejection_code='local_incomplete_response'
    AND local_model_calls=1 AND external_model_calls=0")" == 1 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_event
  WHERE owner_user_id='$owner'::uuid AND operation_id='$operation'::uuid")" == 0 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid")" == 0 ]]

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
backup_partial="$snapshot_dir/.memory_pre_v5_local_incomplete_retry_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_local_incomplete_retry_${run_tag}.dump"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$backup.catalog"
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$backup.catalog"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline
psql_scalar "SELECT table_name FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
    AND table_name NOT IN ('evidence_extraction_job','evidence_extraction_event')
  ORDER BY table_name" >"$table_list"
capture_protected_state "$protected_before"
isolation_before=$(isolation_signature)
qdrant_before=$(qdrant_signature)
jobs_before=$(psql_scalar 'SELECT count(*) FROM memory.evidence_extraction_job')
events_before=$(psql_scalar 'SELECT count(*) FROM memory.evidence_extraction_event')
local_events_before=$(psql_scalar 'SELECT count(*) FROM memory.v5_local_inference_event')

phase=install_and_rollback_tests
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$database" <"$repo_root/$migration" >/dev/null
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$database" <"$repo_root/$test_sql" >/dev/null

phase=requeue
run_retry "$apply_output"
[[ "$(grep -c '|pending|1|applied$' "$apply_output")" -eq 1 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid
    AND status='pending' AND attempts=1 AND last_error IS NULL
    AND lease_token IS NULL AND lease_expires_at IS NULL")" == 1 ]]
[[ "$(psql_scalar 'SELECT count(*) FROM memory.evidence_extraction_job')" == "$jobs_before" ]]
[[ "$(psql_scalar 'SELECT count(*) FROM memory.evidence_extraction_event')" == "$((events_before+1))" ]]
[[ "$(psql_scalar 'SELECT count(*) FROM memory.v5_local_inference_event')" == "$local_events_before" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_event
  WHERE owner_user_id='$owner'::uuid AND operation_id='$operation'::uuid
    AND event_type='queued' AND from_status='skipped' AND to_status='pending'
    AND actor_ref='local_incomplete_retry'")" == 1 ]]

phase=replay
run_retry "$replay_output"
[[ "$(grep -c '|pending|1|replayed$' "$replay_output")" -eq 1 ]]
[[ "$(psql_scalar 'SELECT count(*) FROM memory.evidence_extraction_event')" == "$((events_before+1))" ]]

phase=postflight
capture_protected_state "$protected_after"
cmp -s "$protected_before" "$protected_after"
[[ "$(isolation_signature)" == "$isolation_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid")" == 0 ]]
restore_timers
curl --fail --silent --show-error --max-time 10 \
  http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null

phase=report
report="$snapshot_dir/memory_v1_v5_local_incomplete_retry_${run_tag}.json"
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git -C "$repo_root" rev-parse HEAD)" \
  --arg manifest_sha "$expected_manifest_sha" \
  --arg backup "$backup" \
  --arg qdrant_sha "$qdrant_before" \
  '{contract_version:"memory_v1_v5_local_incomplete_retry_apply_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    manifest_sha256:$manifest_sha,backup:$backup,qdrant_sha256:$qdrant_sha,
    checks:{fresh_backup:true,rollback_only_security_tests:true,
      owner_scoped_requeue:true,zero_write_replay:true,
      other_accounts_unchanged:true,non_target_records_unchanged:true,
      qdrant_unchanged:true,model_calls:0,packet_writes:0,claim_writes:0,
      retrieval_changes:0,prompt_influence_changes:0,timers_restored:true},
    hard_stop:"before_private_local_retry"}' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf 'memory_v1_v5_local_incomplete_retry_production_apply: PASS\n'
printf 'report=%s\n' "$report"
