#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Requeues exactly two reviewed compiler-class failures for
# one synthetic owner. This tool makes no model, packet, claim, Qdrant, or
# prompt-influence writes.

if [[ "${MEMORY_V1_V5_LOCAL_COMPILER_RETRY_V2_APPLY:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_LOCAL_COMPILER_RETRY_V2_APPLY=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
manifest=ops/manifests/memory_v1_v5_local_compiler_retry_v2_20260719.json
expected_manifest_sha=b164ffb786385c4f64df566889f832015c5a464b254206aea5f4e23baf038fcd
required_ancestor=479dd92634336e5dcf28e3c911cc32bcff092fc9
owner=557ea042-cb82-48f8-9429-472e96c957ef
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_local_compiler_retry_v2_apply.lock
phase=initialization
run_tag=
status_file=
units_quiesced=0
unit_state=$(mktemp /tmp/memory-v1-v5-local-compiler-retry-v2-apply-units.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-local-compiler-retry-v2-apply-tables.XXXXXX)

psql_scalar() {
  docker exec "$container" psql -X -A -t -F $'\t' -v ON_ERROR_STOP=1 \
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
  while IFS=$'\t' read -r table has_owner; do
    if [[ "$table" == evidence_extraction_job ||
          "$table" == evidence_extraction_event ]]; then
      continue
    fi
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    [[ "$has_owner" == t || "$has_owner" == f ]]
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

capture_non_target_state() {
  local output=$1 table has_owner predicate state
  : >"$output"
  while IFS=$'\t' read -r table has_owner; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    [[ "$has_owner" == t || "$has_owner" == f ]]
    predicate=true
    if [[ "$has_owner" == t ]]; then
      predicate="owner_user_id <> '$owner'::uuid"
    fi
    state=$(psql_scalar "
      SELECT count(*)::text || E'\\t' || encode(public.digest(convert_to(
        coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
        'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value
        WHERE $predicate
      ) AS rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

target_state() {
  psql_scalar "
    SELECT encode(public.digest(convert_to(coalesce(string_agg(row_json,
      E'\\n' ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
    FROM (
      SELECT to_jsonb(job)::text AS row_json
      FROM memory.evidence_extraction_job AS job
      WHERE job.owner_user_id='$owner'::uuid
        AND job.job_id IN (
          '2a1d1519-b0f4-4a43-962d-12686d85f89e'::uuid,
          'cb7e0e52-bbf9-4ecc-9433-000e9fd9ae54'::uuid
        )
      UNION ALL
      SELECT to_jsonb(event)::text
      FROM memory.evidence_extraction_event AS event
      WHERE event.owner_user_id='$owner'::uuid
        AND event.operation_id IN (
          'd99b1bbf-0fa1-4b1b-a4bb-75096bbd224a'::uuid,
          '42517f47-f48b-4aee-b31e-0c39845e24ee'::uuid
        )
    ) AS rows
  "
}

run_retry() {
  local output=$1
  POSTGRES_DSN="$POSTGRES_DSN" psql "$POSTGRES_DSN" -X -q -A -t -F '|' \
    -v ON_ERROR_STOP=1 >"$output" <<'SQL'
BEGIN;
SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
) \gset actor_
SELECT * FROM memory.requeue_owner_local_compiler_failure_v2(
  'd99b1bbf-0fa1-4b1b-a4bb-75096bbd224a',
  '2a1d1519-b0f4-4a43-962d-12686d85f89e',
  '2de0989137c018f67b3386726d5222c604dba2ffae4a302bc57f2e55fece4681',
  'e4c21d74-b5f6-5f4c-bac6-d76b7043b0a0',
  '8dac68b2-8488-4faa-a917-e62feb5abb2a',1,
  'invalid_structured_output',
  '5cb83e837e38174af4cfda016c20009168ff62974e7d9137b30228d9973256a2',
  'memory_v1_local_policy_compiler_v4'
);
SELECT * FROM memory.requeue_owner_local_compiler_failure_v2(
  '42517f47-f48b-4aee-b31e-0c39845e24ee',
  'cb7e0e52-bbf9-4ecc-9433-000e9fd9ae54',
  '2bb5304cb478a28bf754b5aeb7c8380dc158e2b8882113deef7acc8e16a3ac40',
  '223f18cb-b763-5f1a-a519-bfc7a7f2d614',
  'b06adf49-cbba-4433-872e-d4bfaf194d99',1,
  'local_validation_rejected',
  '5cb83e837e38174af4cfda016c20009168ff62974e7d9137b30228d9973256a2',
  'memory_v1_local_policy_compiler_v4'
);
COMMIT;
SQL
  chmod 0600 "$output"
}

[[ -f "$repo_root/$manifest" ]]
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(sha256sum "$repo_root/$manifest" | awk '{print $1}')" == \
  "$expected_manifest_sha" ]]
[[ "$(jq -r '.contract_version' "$repo_root/$manifest")" == \
  memory_v1_v5_local_compiler_retry_manifest_v2 ]]
[[ "$(jq -r '.owner_user_id' "$repo_root/$manifest")" == "$owner" ]]
[[ "$(jq -r '.retry_contract_version' "$repo_root/$manifest")" == \
  memory_v1_local_policy_compiler_v4 ]]
[[ "$(jq '.rows | length' "$repo_root/$manifest")" == 2 ]]
[[ "$(jq -r '[.rows[].rejection_code] | sort | join("|")' \
  "$repo_root/$manifest")" == \
  'invalid_structured_output|local_validation_rejected' ]]
[[ "$(jq -r '.expected_effects | [.jobs_requeued,.audit_events_inserted,
  .model_calls,.packet_writes,.claim_writes,.qdrant_writes,
  .prompt_influence_changes] | join("|")' "$repo_root/$manifest")" == \
  '2|2|0|0|0|0|0' ]]

exec 9>"$lock_file"
flock -n 9
umask 077
set -a
source "$repo_root/.env"
set +a
[[ -n "${POSTGRES_DSN:-}" ]]

run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_local_compiler_retry_v2_apply_${run_tag}.status"
protected_before="$snapshot_dir/memory_v1_v5_local_compiler_retry_v2_apply_protected_before_${run_tag}.tsv"
protected_after="$snapshot_dir/memory_v1_v5_local_compiler_retry_v2_apply_protected_after_${run_tag}.tsv"
other_before="$snapshot_dir/memory_v1_v5_local_compiler_retry_v2_apply_other_before_${run_tag}.tsv"
other_after="$snapshot_dir/memory_v1_v5_local_compiler_retry_v2_apply_other_after_${run_tag}.tsv"
apply_output="$snapshot_dir/memory_v1_v5_local_compiler_retry_v2_apply_${run_tag}.tsv"
replay_output="$snapshot_dir/memory_v1_v5_local_compiler_retry_v2_replay_${run_tag}.tsv"

phase=preflight
[[ "$(psql_scalar "SELECT (
  to_regprocedure(
    'memory.requeue_owner_local_compiler_failure_v2(uuid,uuid,text,uuid,uuid,integer,text,text,text)'
  ) IS NOT NULL
  AND (SELECT count(*)=2 FROM memory.evidence_extraction_job
    WHERE owner_user_id='$owner'::uuid
      AND job_id IN (
        '2a1d1519-b0f4-4a43-962d-12686d85f89e'::uuid,
        'cb7e0e52-bbf9-4ecc-9433-000e9fd9ae54'::uuid
      ) AND status='skipped' AND attempts=1)
  AND (SELECT count(*)=0 FROM memory.evidence_extraction_event
    WHERE owner_user_id='$owner'::uuid
      AND operation_id IN (
        'd99b1bbf-0fa1-4b1b-a4bb-75096bbd224a'::uuid,
        '42517f47-f48b-4aee-b31e-0c39845e24ee'::uuid
      ))
)::integer")" == 1 ]]

: >"$unit_state"
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$unit_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ "$(wc -l <"$unit_state")" -eq 13 ]]
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
backup_partial="$snapshot_dir/.memory_pre_v5_local_compiler_retry_v2_apply_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_local_compiler_retry_v2_apply_${run_tag}.dump"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$backup.catalog"
[[ -s "$backup.catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$backup.catalog"
backup_sha=$(sha256sum "$backup" | awk '{print $1}')
printf '%s  %s\n' "$backup_sha" "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline
psql_scalar "
  SELECT table_name || E'\\t' || EXISTS (
    SELECT 1 FROM information_schema.columns AS column_value
    WHERE column_value.table_schema='memory'
      AND column_value.table_name=table_value.table_name
      AND column_value.column_name='owner_user_id'
  )::text
  FROM information_schema.tables AS table_value
  WHERE table_schema='memory' AND table_type='BASE TABLE'
  ORDER BY table_name
" >"$table_list"
capture_protected_state "$protected_before"
capture_non_target_state "$other_before"
qdrant_before=$(qdrant_signature)
job_count_before=$(psql_scalar 'SELECT count(*) FROM memory.evidence_extraction_job')
event_count_before=$(psql_scalar 'SELECT count(*) FROM memory.evidence_extraction_event')
local_event_count_before=$(psql_scalar 'SELECT count(*) FROM memory.v5_local_inference_event')

phase=apply
run_retry "$apply_output"
[[ "$(grep -c '|pending|1|applied$' "$apply_output")" -eq 2 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid
    AND job_id IN (
      '2a1d1519-b0f4-4a43-962d-12686d85f89e'::uuid,
      'cb7e0e52-bbf9-4ecc-9433-000e9fd9ae54'::uuid
    ) AND status='pending' AND attempts=1 AND last_error IS NULL
      AND lease_token IS NULL AND lease_expires_at IS NULL")" == 2 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_event
  WHERE owner_user_id='$owner'::uuid
    AND operation_id IN (
      'd99b1bbf-0fa1-4b1b-a4bb-75096bbd224a'::uuid,
      '42517f47-f48b-4aee-b31e-0c39845e24ee'::uuid
    ) AND event_type='queued' AND from_status='skipped'
      AND to_status='pending' AND actor_ref='local_compiler_retry_v2'")" == 2 ]]
[[ "$(psql_scalar 'SELECT count(*) FROM memory.evidence_extraction_job')" == \
  "$job_count_before" ]]
[[ "$(psql_scalar 'SELECT count(*) FROM memory.evidence_extraction_event')" == \
  "$((event_count_before + 2))" ]]
[[ "$(psql_scalar 'SELECT count(*) FROM memory.v5_local_inference_event')" == \
  "$local_event_count_before" ]]
capture_protected_state "$protected_after"
capture_non_target_state "$other_after"
cmp -s "$protected_before" "$protected_after"
cmp -s "$other_before" "$other_after"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

phase=replay
target_before_replay=$(target_state)
run_retry "$replay_output"
[[ "$(grep -c '|pending|1|replayed$' "$replay_output")" -eq 2 ]]
[[ "$(target_state)" == "$target_before_replay" ]]
[[ "$(psql_scalar 'SELECT count(*) FROM memory.evidence_extraction_event')" == \
  "$((event_count_before + 2))" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

restore_timers

phase=report
report="$snapshot_dir/memory_v1_v5_local_compiler_retry_v2_apply_${run_tag}.json"
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git -C "$repo_root" rev-parse HEAD)" \
  --arg manifest "$manifest" --arg manifest_sha256 "$expected_manifest_sha" \
  --arg owner_user_id "$owner" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg apply_output "$apply_output" --arg replay_output "$replay_output" \
  --arg qdrant_sha256 "$qdrant_before" \
  '{contract_version:"memory_v1_v5_local_compiler_retry_v2_apply_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    manifest:{path:$manifest,sha256:$manifest_sha256},
    owner_user_id:$owner_user_id,
    backup:{path:$backup,sha256:$backup_sha256},
    evidence:{apply_output:$apply_output,replay_output:$replay_output,
      qdrant_sha256:$qdrant_sha256},
    checks:{fresh_backup:true,jobs_requeued:2,audit_events_inserted:2,
      exact_owner_scope:true,non_target_rows_unchanged:true,
      protected_tables_unchanged:true,zero_write_replay:true,
      qdrant_unchanged:true,timers_restored:true,model_calls:0,
      packet_writes:0,claim_writes:0,prompt_influence_changes:0},
    hard_stop:"before_private_inference_retry_or_any_retrieval_influence"}' \
  >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf 'memory_v1_v5_local_compiler_retry_v2_production_apply: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
