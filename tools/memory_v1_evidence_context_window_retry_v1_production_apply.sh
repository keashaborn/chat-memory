#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Deploys the tested evidence-context runtime and
# transactionally requeues exactly five owner-scoped validation failures.
# This tool makes no model, packet, claim, Qdrant, retrieval, or prompt writes.

if [[ "${MEMORY_V1_EVIDENCE_CONTEXT_RETRY_V1_APPLY:-}" != authorized ]]; then
  echo 'MEMORY_V1_EVIDENCE_CONTEXT_RETRY_V1_APPLY=authorized is required' >&2
  exit 1
fi
[[ "$(id -u)" -eq 0 ]] || {
  echo 'run as root on seebx' >&2
  exit 1
}

target_commit=${1:?target commit required}
repo_root=$(git rev-parse --show-toplevel)
live=/opt/chat-memory
manifest=ops/manifests/memory_v1_evidence_context_window_retry_v1_20260726.json
expected_manifest_sha=4834f7959e503efcff519ac1db10aae40213b43145165dbdc67a5e6d4855e8e9
expected_live=f8d313a02f0460ddadca4b95687ae3dba7f5d2d0
implementation_commit=cf98a2b876cb5662827d65378c9b46fed2a2d0e7
migration=ops/sql/20260726_memory_v1_zero_call_validation_retry.sql
rollback=ops/sql/20260726_memory_v1_zero_call_validation_retry_rollback.sql
security_test=tests/memory_v1_zero_call_validation_retry.sql
expected_migration_sha=d174e2b2ecc9263e833eac48b99643ee65af18e029e7d6bf7b2770bd763371f2
expected_rollback_sha=c7e3035aa19d9175f9e305eafaa27c82c11b85ed94da3a17ddda4ac69f94ee9f
expected_security_test_sha=e917f50d233bf32170031e6a8160b2feac769e174f686c011f0e9b5f9d1c6431
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_evidence_context_window_retry_v1.lock
phase=initialization
run_tag=
status_file=
units_quiesced=0
unit_state=$(mktemp /tmp/memory-v1-context-retry-units.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-context-retry-tables.XXXXXX)
cross_owner_output=$(mktemp /tmp/memory-v1-context-retry-cross-owner.XXXXXX)

git_live=(git -c safe.directory="$live" -C "$live")

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
      systemctl start "$unit"
    else
      systemctl stop "$unit"
    fi
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$unit_state"
  units_quiesced=0
}

record_exit() {
  exit_code=$?
  restore_timers || exit_code=1
  rm -f "$unit_state" "$table_list" "$cross_owner_output"
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
    [[ "$has_owner" == true || "$has_owner" == false ]]
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
    [[ "$has_owner" == true || "$has_owner" == false ]]
    predicate=true
    if [[ "$has_owner" == true ]]; then
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
          '8b1b36c4-2a7b-43a3-bdbb-97d17749e1f6'::uuid,
          '1349d208-d1b4-476d-8379-f49c84870543'::uuid,
          'a4d6a327-372c-4776-9411-bcb1b58ac121'::uuid,
          '079e5f54-e0c3-4051-976c-a47593a3ab6b'::uuid,
          '13ac1f7e-a09e-426e-98e8-d963d81f02a8'::uuid
        )
      UNION ALL
      SELECT to_jsonb(event)::text
      FROM memory.evidence_extraction_event AS event
      WHERE event.owner_user_id='$owner'::uuid
        AND event.operation_id IN (
          'f56c62cc-e28d-5ea6-8b1d-06ce36509e3d'::uuid,
          'a0be1723-a7aa-5fcd-b34c-5daab2c35eb9'::uuid,
          '68822bc5-fe01-5887-83f6-7026699c1e40'::uuid,
          'd07068f1-17a7-50cc-a568-b4cee046efff'::uuid,
          '3be3f335-b30e-57ad-a33e-ab095437b6cb'::uuid
        )
    ) AS rows
  "
}

run_retry() {
  local output=$1
  psql "$POSTGRES_DSN" -X -q -A -t -F '|' -v ON_ERROR_STOP=1 \
    >"$output" <<'SQL'
BEGIN;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);
SELECT * FROM memory.requeue_owner_local_validation_failure_v1(
  'f56c62cc-e28d-5ea6-8b1d-06ce36509e3d',
  '8b1b36c4-2a7b-43a3-bdbb-97d17749e1f6',
  '6df993f62a9d749c100dc481fa661700cc48bfca50d63c2aba8cde1fe7b79082',
  '574b1caf-c34d-5221-bbff-bc8008b0c6e1',
  'ec8a1d95-8778-4686-9fa6-26bba1176885',1,
  'local_validation_rejected','registry_enum_compiler_repair'
);
SELECT * FROM memory.requeue_owner_local_validation_failure_v1(
  'a0be1723-a7aa-5fcd-b34c-5daab2c35eb9',
  '1349d208-d1b4-476d-8379-f49c84870543',
  '9c6993e8aff11eae0b6d2235346055f33b72117c0f89d6e256f2ca8633235912',
  '1684cff7-a916-5470-87f6-62637efbdfeb',
  '63c944dc-63bc-41f6-997e-fcb02c4218e0',1,
  'local_validation_rejected','registry_enum_compiler_repair'
);
SELECT * FROM memory.requeue_owner_local_validation_failure_v1(
  '68822bc5-fe01-5887-83f6-7026699c1e40',
  'a4d6a327-372c-4776-9411-bcb1b58ac121',
  'e669f3377fb0f3936199de063d04375deaa519202b69c20de863f68e6ef9956d',
  '32b029c5-df39-545b-9d72-02a01db8b363',
  '20cdd10f-a2d6-4631-8a25-a04e1efc6f5f',1,
  'local_validation_rejected','registry_enum_compiler_repair'
);
SELECT * FROM memory.requeue_owner_local_validation_failure_v1(
  'd07068f1-17a7-50cc-a568-b4cee046efff',
  '079e5f54-e0c3-4051-976c-a47593a3ab6b',
  'e56a7f11175bf0c0db2335d93581fbbc77c86402a341e51baf112e95743de7ec',
  '19a4ebda-23ee-5f84-82d1-a2aaa6f4acfc',
  'b5f9d2d4-9a0b-4f19-b40d-462e27b52e20',1,
  'local_validation_rejected','registry_enum_compiler_repair'
);
SELECT *
FROM memory.requeue_owner_local_zero_call_validation_failure_v1(
  '3be3f335-b30e-57ad-a33e-ab095437b6cb',
  '13ac1f7e-a09e-426e-98e8-d963d81f02a8',
  '8e4cfbd41156ccc92fd4392372a220d3e5802597c6d5756f8d48748503d0166b',
  '2d74f3ca-0eda-516c-9520-222ae97cf6c1',
  '1fee556a-0da0-4d63-b7c2-f1237742b504',1,
  'evidence_context_window_compiler_repair'
);
COMMIT;
SQL
  chmod 0600 "$output"
}

run_cross_owner_rejection() {
  if psql "$POSTGRES_DSN" -X -q -A -t -v ON_ERROR_STOP=1 \
      >"$cross_owner_output" 2>&1 <<'SQL'
BEGIN;
SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
SELECT *
FROM memory.requeue_owner_local_zero_call_validation_failure_v1(
  '3be3f335-b30e-57ad-a33e-ab095437b6cb',
  '13ac1f7e-a09e-426e-98e8-d963d81f02a8',
  '8e4cfbd41156ccc92fd4392372a220d3e5802597c6d5756f8d48748503d0166b',
  '2d74f3ca-0eda-516c-9520-222ae97cf6c1',
  '1fee556a-0da0-4d63-b7c2-f1237742b504',1,
  'evidence_context_window_compiler_repair'
);
ROLLBACK;
SQL
  then
    echo 'cross-owner retry unexpectedly succeeded' >&2
    exit 1
  fi
  grep -q 'zero-call validation retry job binding is invalid' \
    "$cross_owner_output"
}

[[ -f "$repo_root/$manifest" ]]
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
[[ "$(git -C "$repo_root" rev-parse HEAD)" == "$target_commit" ]]
git -C "$repo_root" merge-base --is-ancestor "$implementation_commit" HEAD
[[ "$("${git_live[@]}" rev-parse HEAD)" == "$expected_live" ]]
[[ -z "$("${git_live[@]}" status --porcelain)" ]]
"${git_live[@]}" merge-base --is-ancestor "$expected_live" "$target_commit"
[[ "$(sha256sum "$repo_root/$manifest" | awk '{print $1}')" == \
  "$expected_manifest_sha" ]]
[[ "$(sha256sum "$repo_root/$migration" | awk '{print $1}')" == \
  "$expected_migration_sha" ]]
[[ "$(sha256sum "$repo_root/$rollback" | awk '{print $1}')" == \
  "$expected_rollback_sha" ]]
[[ "$(sha256sum "$repo_root/$security_test" | awk '{print $1}')" == \
  "$expected_security_test_sha" ]]
[[ "$(sha256sum "$repo_root/rag_engine/memory_v1_evidence_context_loader_v1.py" |
  awk '{print $1}')" == 15b207437cfdbe6708910929b67fc5be4befab24b1cdfad3aaf17d00fcc4ec22 ]]
[[ "$(sha256sum "$repo_root/scripts/memory_v1_relational_extraction_v5_local_provider.py" |
  awk '{print $1}')" == 23d43795ec186377383a2f44c4e9297c12fc020e1ce8311d2e32aaf281776be5 ]]
[[ "$(jq -r '.contract_version' "$repo_root/$manifest")" == \
  memory_v1_evidence_context_window_retry_manifest_v1 ]]
[[ "$(jq -r '.owner_user_id' "$repo_root/$manifest")" == "$owner" ]]
[[ "$(jq '.rows | length' "$repo_root/$manifest")" == 5 ]]
[[ "$(jq -r '[.rows[].rejection_code] | unique | join("|")' \
  "$repo_root/$manifest")" == local_validation_rejected ]]
[[ "$(jq -r '[.rows[].retry_function] | group_by(.) |
  map({key:.[0],value:length}) | from_entries |
  [.["memory.requeue_owner_local_validation_failure_v1"],
   .["memory.requeue_owner_local_zero_call_validation_failure_v1"]] |
  join("|")' "$repo_root/$manifest")" == '4|1' ]]
[[ "$(jq -r '.expected_effects | [.jobs_requeued,.audit_events_inserted,
  .additive_functions_installed,.model_calls,.packet_writes,
  .claim_writes,.qdrant_writes,
  .retrieval_changes,.prompt_influence_changes] | join("|")' \
  "$repo_root/$manifest")" == '5|5|1|0|0|0|0|0|0' ]]

exec 9>"$lock_file"
flock -n 9
umask 077
set -a
source "$live/.env"
source /etc/verbalsage/brains.env
set +a
[[ -n "${POSTGRES_DSN:-}" && -n "${VS_SERVICE_TOKEN:-}" ]]
[[ "$(psql "$POSTGRES_DSN" -X -A -t -v ON_ERROR_STOP=1 \
  -c 'SELECT session_user')" == brains_app ]]

run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_evidence_context_window_retry_v1_${run_tag}.status"
protected_before="$snapshot_dir/memory_v1_evidence_context_window_retry_v1_protected_before_${run_tag}.tsv"
protected_after="$snapshot_dir/memory_v1_evidence_context_window_retry_v1_protected_after_${run_tag}.tsv"
other_before="$snapshot_dir/memory_v1_evidence_context_window_retry_v1_other_before_${run_tag}.tsv"
other_after="$snapshot_dir/memory_v1_evidence_context_window_retry_v1_other_after_${run_tag}.tsv"
apply_output="$snapshot_dir/memory_v1_evidence_context_window_retry_v1_apply_${run_tag}.tsv"
replay_output="$snapshot_dir/memory_v1_evidence_context_window_retry_v1_replay_${run_tag}.tsv"

phase=preflight
[[ "$(psql_scalar "SELECT (
  to_regprocedure(
    'memory.requeue_owner_local_validation_failure_v1(uuid,uuid,text,uuid,uuid,integer,text,text)'
  ) IS NOT NULL
  AND (SELECT count(*)=5 FROM memory.evidence_extraction_job
    WHERE owner_user_id='$owner'::uuid
      AND job_id IN (
        '8b1b36c4-2a7b-43a3-bdbb-97d17749e1f6'::uuid,
        '1349d208-d1b4-476d-8379-f49c84870543'::uuid,
        'a4d6a327-372c-4776-9411-bcb1b58ac121'::uuid,
        '079e5f54-e0c3-4051-976c-a47593a3ab6b'::uuid,
        '13ac1f7e-a09e-426e-98e8-d963d81f02a8'::uuid
      ) AND status='skipped' AND attempts=1
        AND last_error='local_inference_rejected: local_validation_rejected')
  AND (SELECT count(*)=0 FROM memory.evidence_extraction_event
    WHERE owner_user_id='$owner'::uuid
      AND operation_id IN (
        'f56c62cc-e28d-5ea6-8b1d-06ce36509e3d'::uuid,
        'a0be1723-a7aa-5fcd-b34c-5daab2c35eb9'::uuid,
        '68822bc5-fe01-5887-83f6-7026699c1e40'::uuid,
        'd07068f1-17a7-50cc-a568-b4cee046efff'::uuid,
        '3be3f335-b30e-57ad-a33e-ab095437b6cb'::uuid
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
[[ "$(wc -l <"$unit_state")" -ge 13 ]]
units_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  systemctl stop "$unit"
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
backup_partial="$snapshot_dir/.memory_pre_context_window_retry_v1_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_context_window_retry_v1_${run_tag}.dump"
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

phase=deploy
"${git_live[@]}" merge --ff-only "$target_commit"
[[ "$("${git_live[@]}" rev-parse HEAD)" == "$target_commit" ]]
[[ "$(sha256sum "$live/rag_engine/memory_v1_evidence_context_loader_v1.py" |
  awk '{print $1}')" == 15b207437cfdbe6708910929b67fc5be4befab24b1cdfad3aaf17d00fcc4ec22 ]]
[[ "$(sha256sum "$live/scripts/memory_v1_relational_extraction_v5_local_provider.py" |
  awk '{print $1}')" == 23d43795ec186377383a2f44c4e9297c12fc020e1ce8311d2e32aaf281776be5 ]]
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$database" <"$live/$migration" >/dev/null
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$database" <"$live/$security_test" >/dev/null
[[ "$(psql_scalar "SELECT (
  to_regprocedure(
    'memory.requeue_owner_local_zero_call_validation_failure_v1(uuid,uuid,text,uuid,uuid,integer,text)'
  ) IS NOT NULL
)::integer")" == 1 ]]
systemctl restart brains.service
for _attempt in $(seq 1 30); do
  if curl -fsS -H "x-vs-service-token: ${VS_SERVICE_TOKEN}" \
      http://127.0.0.1:8088/healthz >/dev/null 2>&1 \
    && curl -fsS -H "x-vs-service-token: ${VS_SERVICE_TOKEN}" \
      http://127.0.0.1:8088/readyz >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl -fsS -H "x-vs-service-token: ${VS_SERVICE_TOKEN}" \
  http://127.0.0.1:8088/healthz >/dev/null
curl -fsS -H "x-vs-service-token: ${VS_SERVICE_TOKEN}" \
  http://127.0.0.1:8088/readyz >/dev/null

phase=apply
run_retry "$apply_output"
[[ "$(grep -c '|pending|1|applied$' "$apply_output")" -eq 5 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid
    AND job_id IN (
      '8b1b36c4-2a7b-43a3-bdbb-97d17749e1f6'::uuid,
      '1349d208-d1b4-476d-8379-f49c84870543'::uuid,
      'a4d6a327-372c-4776-9411-bcb1b58ac121'::uuid,
      '079e5f54-e0c3-4051-976c-a47593a3ab6b'::uuid,
      '13ac1f7e-a09e-426e-98e8-d963d81f02a8'::uuid
    ) AND status='pending' AND attempts=1 AND last_error IS NULL
      AND lease_token IS NULL AND lease_expires_at IS NULL")" == 5 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_event
  WHERE owner_user_id='$owner'::uuid
    AND operation_id IN (
      'f56c62cc-e28d-5ea6-8b1d-06ce36509e3d'::uuid,
      'a0be1723-a7aa-5fcd-b34c-5daab2c35eb9'::uuid,
      '68822bc5-fe01-5887-83f6-7026699c1e40'::uuid,
      'd07068f1-17a7-50cc-a568-b4cee046efff'::uuid,
      '3be3f335-b30e-57ad-a33e-ab095437b6cb'::uuid
    ) AND event_type='queued' AND from_status='skipped'
      AND to_status='pending'
      AND actor_ref IN (
        'local_validation_retry','local_zero_call_validation_retry'
      )")" == 5 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_event
  WHERE owner_user_id='$owner'::uuid
    AND operation_id='3be3f335-b30e-57ad-a33e-ab095437b6cb'::uuid
    AND actor_ref='local_zero_call_validation_retry'")" == 1 ]]
[[ "$(psql_scalar 'SELECT count(*) FROM memory.evidence_extraction_job')" == \
  "$job_count_before" ]]
[[ "$(psql_scalar 'SELECT count(*) FROM memory.evidence_extraction_event')" == \
  "$((event_count_before + 5))" ]]
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
[[ "$(grep -c '|pending|1|replayed$' "$replay_output")" -eq 5 ]]
[[ "$(target_state)" == "$target_before_replay" ]]
[[ "$(psql_scalar 'SELECT count(*) FROM memory.evidence_extraction_event')" == \
  "$((event_count_before + 5))" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

phase=isolation
run_cross_owner_rejection
[[ "$(target_state)" == "$target_before_replay" ]]
[[ "$(psql_scalar 'SELECT count(*) FROM memory.evidence_extraction_event')" == \
  "$((event_count_before + 5))" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

restore_timers

phase=report
report="$snapshot_dir/memory_v1_evidence_context_window_retry_v1_${run_tag}.json"
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$("${git_live[@]}" rev-parse HEAD)" \
  --arg manifest "$manifest" --arg manifest_sha256 "$expected_manifest_sha" \
  --arg owner_user_id "$owner" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg apply_output "$apply_output" --arg replay_output "$replay_output" \
  --arg qdrant_sha256 "$qdrant_before" \
  '{contract_version:"memory_v1_evidence_context_window_retry_apply_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    manifest:{path:$manifest,sha256:$manifest_sha256},
    owner_user_id:$owner_user_id,
    backup:{path:$backup,sha256:$backup_sha256},
    evidence:{apply_output:$apply_output,replay_output:$replay_output,
      qdrant_sha256:$qdrant_sha256},
    checks:{fresh_backup:true,runtime_deployed:true,
      additive_functions_installed:1,jobs_requeued:5,
      audit_events_inserted:5,exact_owner_scope:true,
      non_target_rows_unchanged:true,protected_tables_unchanged:true,
      zero_write_replay:true,cross_owner_rejected:true,qdrant_unchanged:true,
      timers_restored:true,service_health:true,model_calls:0,
      packet_writes:0,claim_writes:0,retrieval_changes:0,
      prompt_influence_changes:0},
    hard_stop:"after_exact_five_requeue_before_private_inference_results"}' \
  >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf 'memory_v1_evidence_context_window_retry_v1_production_apply: PASS\n'
printf 'head=%s\nreport=%s\nbackup=%s\n' \
  "$("${git_live[@]}" rev-parse HEAD)" "$report" "$backup"
