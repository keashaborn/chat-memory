#!/usr/bin/env bash
set -euo pipefail

# Server: seebx backend only.
# Completes read-only verification after the reviewed atom apply committed but
# the original installer stopped on a postflight column-name assertion. It
# performs an exact zero-write replay and writes an immutable recovery report.

if [[ "${MEMORY_V1_V5_2_ATOM_ADMISSION_POSTFLIGHT_RECOVERY:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_ATOM_ADMISSION_POSTFLIGHT_RECOVERY=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
env_file=/opt/chat-memory/.env
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_atom_apply.lock
manifest=ops/manifests/memory_v1_v5_2_atom_admission_apply_manifest_20260724.json
runner=scripts/memory_v1_v5_2_atom_admission_apply.py
required_base=75b8c2fc44ae22460d583f1f37d4fd2d1dac2cdd
expected_manifest_file_sha=f090948929ef30eb91cfffb7363531cacaac00c32d1c78f36468986c5d75f621
expected_manifest_sha=75ca06aa6a541c175baab6c047c5e064c353353962bd17406f4b2d78daaa212d
expected_runner_sha=f878b212a6ad5395490747ebc12b2560bf7aa38616ac6250919c554d6158c64f
expected_review_preflight_sha=9655c8b28f544dee55ee411fdd2965b2347cd061fbfe426089389cb1a5bc815a
expected_review_apply_sha=1cf8a14414d109bca45895f9e5eadae666b078a8f9c733e263de62f42a6e5708
expected_qdrant_sha=1b3e3b6b38ba90ee039b21bff10cf6a1cddb0e0ce63cc446b6305c584bf583b2
target_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
stance_packet=78ca7a3e-e136-535a-9fe6-1d83aefff806
preference_packet=a5624f05-8d75-5b96-bfd7-9f56145f7ad9
stance_proposal=e7ee3e4b-6056-512a-86fa-8fce608e0e3f
preference_proposal=f7410e6d-9765-54b9-9a46-af9d5a875600
stance_review=7d945d31-2a13-5a38-95d5-0dc7e12517b3
preference_review=3352eb53-47dc-5503-a34a-0418f0994e0c
stance_apply=a94e6005-7142-581c-b4db-df314a358c66
original_run_tag=20260724T122334Z_75b8c2fc44ae

phase=initialization
run_tag=
status_file=
timer_state=
timers_quiesced=0

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
}

function_sha() {
  local signature=$1
  psql_scalar "
    SELECT encode(public.digest(
      convert_to(pg_get_functiondef('$signature'::regprocedure),'UTF8'),
      'sha256'
    ),'hex')
  "
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
  source "$env_file"
  set +a
  [[ -n "${VS_SERVICE_TOKEN:-}" ]]
  [[ "$(systemctl is-active brains.service)" == active ]]
  docker exec "$container" pg_isready -U sage -d "$database" >/dev/null
  curl --fail --silent --show-error --max-time 10 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null
  curl --fail --silent --show-error --max-time 10 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/readyz \
    | jq -e '.ok==true and .postgres==true' >/dev/null
}

capture_protected() {
  local table_list=$1
  local output=$2
  local table state
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    state=$(psql_scalar "
      SELECT count(*)::text || E'\\t' ||
        encode(public.digest(convert_to(coalesce(string_agg(
          row_json,E'\\n' ORDER BY row_json
        ),''),'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value
      ) AS rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

restore_timers() {
  [[ "$timers_quiesced" -eq 1 && -s "$timer_state" ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
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
  local exit_code=$?
  local failed_phase
  set +e
  if [[ "$timers_quiesced" -eq 1 ]]; then
    failed_phase=$phase
    phase=restore_timers_after_failure
    restore_timers || exit_code=1
    phase=$failed_phase
  fi
  if [[ -n "$status_file" ]]; then
    {
      printf 'run_tag=%s\n' "$run_tag"
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$exit_code"
      printf 'head=%s\n' "$(git rev-parse HEAD)"
      printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$exit_code"
}
trap record_exit EXIT

phase=source_preflight
[[ -z "$(git status --porcelain)" ]]
git merge-base --is-ancestor "$required_base" HEAD
[[ "$(sha256sum "$manifest" | awk '{print $1}')" == "$expected_manifest_file_sha" ]]
[[ "$(jq -r '.manifest_sha256' "$manifest")" == "$expected_manifest_sha" ]]
[[ "$(sha256sum "$runner" | awk '{print $1}')" == "$expected_runner_sha" ]]
[[ "$(function_sha \
  'memory.preflight_owner_v5_2_atom_review_v1(uuid,memory.v5_2_atom_review_decision,text,text,jsonb)')" \
  == "$expected_review_preflight_sha" ]]
[[ "$(function_sha \
  'memory.review_owner_v5_2_atom_proposal_v1(uuid,uuid,uuid,memory.v5_2_atom_review_decision,text,text,jsonb,text)')" \
  == "$expected_review_apply_sha" ]]
authenticated_health

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_2_atom_apply_recovery_${run_tag}.status"
timer_state="$snapshot_dir/memory_v1_v5_2_atom_apply_recovery_timers_${run_tag}.tsv"
current_protected="$snapshot_dir/memory_v1_v5_2_atom_apply_recovery_protected_${run_tag}.tsv"
replay_output="$snapshot_dir/memory_v1_v5_2_atom_apply_recovery_replay_${run_tag}.json"
report="$snapshot_dir/memory_v1_v5_2_atom_apply_recovery_${run_tag}.json"
original_status="$snapshot_dir/memory_v1_v5_2_atom_apply_${original_run_tag}.status"
original_timer_state="$snapshot_dir/memory_v1_v5_2_atom_apply_timers_${original_run_tag}.tsv"
original_tables="$snapshot_dir/memory_v1_v5_2_atom_apply_tables_${original_run_tag}.txt"
original_protected="$snapshot_dir/memory_v1_v5_2_atom_apply_before_${original_run_tag}.tsv"
original_preflight="$snapshot_dir/memory_v1_v5_2_atom_apply_preflight_${original_run_tag}.json"
original_apply="$snapshot_dir/memory_v1_v5_2_atom_apply_commit_${original_run_tag}.json"
original_replay="$snapshot_dir/memory_v1_v5_2_atom_apply_replay_${original_run_tag}.json"
backup="$snapshot_dir/memory_pre_v5_2_atom_apply_${original_run_tag}.dump"

phase=validate_original_artifacts
for artifact in \
  "$original_status" \
  "$original_timer_state" \
  "$original_tables" \
  "$original_protected" \
  "$original_preflight" \
  "$original_apply" \
  "$original_replay" \
  "$backup" \
  "$backup.sha256" \
  "$backup.catalog"; do
  [[ -s "$artifact" ]]
done
[[ "$(stat -c '%a' "$backup")" == 600 ]]
sha256sum -c "$backup.sha256"
docker exec -i "$container" pg_restore -l <"$backup" >/dev/null
grep -qx 'phase=postflight' "$original_status"
grep -qx 'exit_code=1' "$original_status"
grep -qx 'durable_rows_present=1' "$original_status"
[[ "$(jq -r '.mode + "|" + (.persistent_writes|tostring)' "$original_preflight")" \
  == 'preflight|0' ]]
[[ "$(jq -r '.mode + "|" + (.persistent_writes|tostring)' "$original_apply")" \
  == 'apply|10' ]]
[[ "$(jq -r '.mode + "|" + (.persistent_writes|tostring)' "$original_replay")" \
  == 'replay|0' ]]
for output in "$original_preflight" "$original_apply" "$original_replay"; do
  [[ "$(jq -r '.manifest_sha256' "$output")" == "$expected_manifest_sha" ]]
done
[[ "$(jq '[.results[].proposal_outcome] | unique == ["applied"]' \
  "$original_apply")" == true ]]
[[ "$(jq '[.same_transaction_replay[].proposal_outcome] | unique == ["replayed"]' \
  "$original_apply")" == true ]]
[[ "$(jq '[.results[].proposal_outcome] | unique == ["replayed"]' \
  "$original_replay")" == true ]]

phase=verify_restored_timer_state
: >"$timer_state"
while IFS=$'\t' read -r unit expected_enabled _expected_active; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' \
    "$unit" "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
  [[ "$(systemctl is-enabled "$unit")" == "$expected_enabled" ]]
done <"$original_timer_state"
chmod 0600 "$timer_state"
cmp -s "$original_timer_state" "$timer_state"

phase=quiesce_timers
while IFS=$'\t' read -r unit _enabled _active; do
  sudo -n systemctl stop "$unit"
done <"$timer_state"
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
  [[ "$(systemctl is-active "$unit")" == inactive ]]
done <"$timer_state"

phase=verify_exact_rows
[[ "$(psql_scalar "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.v5_2_atom_admission_proposal),
    (SELECT count(*) FROM memory.v5_2_atom_admission_review),
    (SELECT count(*) FROM memory.v5_2_atom_admission_apply),
    (SELECT count(*) FROM memory.v5_2_atom_admission_operation)
  )
")" == '2,2,1,5' ]]
[[ "$(psql_scalar "
  SELECT count(*)
  FROM (
    SELECT owner_user_id FROM memory.v5_2_atom_admission_proposal
    UNION ALL
    SELECT owner_user_id FROM memory.v5_2_atom_admission_review
    UNION ALL
    SELECT owner_user_id FROM memory.v5_2_atom_admission_apply
    UNION ALL
    SELECT owner_user_id FROM memory.v5_2_atom_admission_operation
  ) AS scoped
  WHERE owner_user_id <> '$target_owner'::uuid
")" == 0 ]]
[[ "$(psql_scalar "
  SELECT string_agg(
    proposal_id::text || '|' || packet_id::text || '|' ||
    admitted_observation_count::text || '|' || deferred_atom_count::text,
    ',' ORDER BY proposal_id
  )
  FROM memory.v5_2_atom_admission_proposal
")" == \
  "$stance_proposal|$stance_packet|4|1,$preference_proposal|$preference_packet|0|2" ]]
[[ "$(psql_scalar "
  SELECT string_agg(
    review_id::text || '|' || proposal_id::text || '|' || decision::text,
    ',' ORDER BY review_id
  )
  FROM memory.v5_2_atom_admission_review
")" == \
  "$preference_review|$preference_proposal|deferred,$stance_review|$stance_proposal|authorized" ]]
[[ "$(psql_scalar "
  SELECT apply_id::text || '|' || review_id::text
  FROM memory.v5_2_atom_admission_apply
")" == "$stance_apply|$stance_review" ]]
[[ "$(psql_scalar "
  SELECT concat_ws(',',
    count(*) FILTER (WHERE operation='record_proposal'),
    count(*) FILTER (WHERE operation='record_review'),
    count(*) FILTER (WHERE operation='apply_review')
  )
  FROM memory.v5_2_atom_admission_operation
")" == '2,2,1' ]]

phase=zero_write_replay
set -a
source "$env_file"
set +a
python3 "$runner" \
  --mode replay \
  --manifest "$manifest" \
  --output "$replay_output"
[[ "$(jq -r '.mode + "|" + (.persistent_writes|tostring)' "$replay_output")" \
  == 'replay|0' ]]
[[ "$(jq -r '.manifest_sha256' "$replay_output")" == "$expected_manifest_sha" ]]
[[ "$(jq '[.results[].proposal_outcome] | unique == ["replayed"]' \
  "$replay_output")" == true ]]
[[ "$(jq '.same_transaction_replay | length' "$replay_output")" == 0 ]]
[[ "$(psql_scalar "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.v5_2_atom_admission_proposal),
    (SELECT count(*) FROM memory.v5_2_atom_admission_review),
    (SELECT count(*) FROM memory.v5_2_atom_admission_apply),
    (SELECT count(*) FROM memory.v5_2_atom_admission_operation)
  )
")" == '2,2,1,5' ]]

phase=verify_non_target_state
capture_protected "$original_tables" "$current_protected"
cmp -s "$original_protected" "$current_protected"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$expected_qdrant_sha" ]]
authenticated_health

phase=restore_timers
restore_timers
authenticated_health
: >"$timer_state.after"
while IFS=$'\t' read -r unit _enabled _active; do
  printf '%s\t%s\t%s\n' \
    "$unit" "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state.after"
done <"$original_timer_state"
chmod 0600 "$timer_state.after"
cmp -s "$original_timer_state" "$timer_state.after"

phase=write_report
jq -n \
  --arg contract_version memory_v1_v5_2_atom_admission_apply_recovery_report_v1 \
  --arg run_tag "$run_tag" \
  --arg head "$(git rev-parse HEAD)" \
  --arg original_run_tag "$original_run_tag" \
  --arg original_status "$original_status" \
  --arg manifest_sha256 "$expected_manifest_sha" \
  --arg backup "$backup" \
  --arg backup_sha256 "$(sha256sum "$backup" | awk '{print $1}')" \
  --arg replay_output "$replay_output" \
  --arg qdrant_signature "$qdrant_after" \
  '{
    contract_version:$contract_version,
    run_tag:$run_tag,
    head:$head,
    recovered_run:{run_tag:$original_run_tag,status:$original_status},
    manifest_sha256:$manifest_sha256,
    backup:{path:$backup,sha256:$backup_sha256,catalog_verified:true},
    results:{
      proposals:2,
      reviews:2,
      authorizations:1,
      operations:5,
      exact_row_bindings:"passed",
      replay:"passed_zero_write",
      account_isolation:"passed",
      non_target_memory_unchanged:true,
      relational_staging_rows_created:0,
      entity_rows_created:0,
      observation_rows_created:0,
      claim_rows_created:0,
      qdrant_unchanged:true,
      timers_restored:true,
      service_health:"passed",
      retrieval_or_prompt_influence:false
    },
    replay_output:$replay_output,
    qdrant_signature:$qdrant_signature
  }' >"$report"
chmod 0600 "$report"

phase=complete
printf '%s\n' \
  "MEMORY_V1_V5_2_ATOM_ADMISSION_POSTFLIGHT_RECOVERY=PASS" \
  "head=$(git rev-parse HEAD)" \
  "original_run_tag=$original_run_tag" \
  "report=$report" \
  "rows=2,2,1,5" \
  "replay_writes=0" \
  "qdrant_signature=$qdrant_after"
