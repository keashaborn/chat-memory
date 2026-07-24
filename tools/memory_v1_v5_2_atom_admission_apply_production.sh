#!/usr/bin/env bash
set -euo pipefail

# Server: seebx backend only.
# Installs the hash-locked atom-review replay compatibility function and
# transactionally persists exactly two reviewed atom proposals, two reviews,
# one authorization, and five operation rows. It never stages relational data,
# creates governed claims, writes Qdrant, or influences retrieval/prompts.

if [[ "${MEMORY_V1_V5_2_ATOM_ADMISSION_APPLY:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_ATOM_ADMISSION_APPLY=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
env_file=/opt/chat-memory/.env
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_atom_apply.lock
compat_migration=ops/sql/20260724_memory_v1_v5_2_atom_review_replay_compat.sql
compat_rollback=ops/sql/20260724_memory_v1_v5_2_atom_review_replay_compat_rollback.sql
manifest=ops/manifests/memory_v1_v5_2_atom_admission_apply_manifest_20260724.json
runner=scripts/memory_v1_v5_2_atom_admission_apply.py
clone_test=tools/memory_v1_v5_2_atom_admission_apply_production_clone.sh
required_base=f10b8c8a9745aa7729f74d5a54f4dc9794c17585
required_production_ancestor=6b7d01fee0ff31bbaad5e7b74ba75875c2ea3e46
expected_compat_sha=ac5a1e306d06ca6d30a4cfb89fa309073ebc78161d0f5b267cfc13fce7983396
expected_rollback_sha=c1fef042ff2a5cb5483cd9c5ef028b9ba63666e1adf98d917b59acfcd3c71ae9
expected_manifest_file_sha=f090948929ef30eb91cfffb7363531cacaac00c32d1c78f36468986c5d75f621
expected_manifest_sha=75ca06aa6a541c175baab6c047c5e064c353353962bd17406f4b2d78daaa212d
expected_runner_sha=f878b212a6ad5395490747ebc12b2560bf7aa38616ac6250919c554d6158c64f
expected_clone_sha=b4b60359352c811dda1da4f8707f3ad1d46aae040c408a84b2ea1168d3fbf726
expected_review_preflight_before=9655c8b28f544dee55ee411fdd2965b2347cd061fbfe426089389cb1a5bc815a
expected_review_apply_before=afc3d4fab7378b35bed8966e330bb26a65e4c77e12b0d530717e7669fa71cd7e
target_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
stance_packet=78ca7a3e-e136-535a-9fe6-1d83aefff806
preference_packet=a5624f05-8d75-5b96-bfd7-9f56145f7ad9

phase=initialization
run_tag=
status_file=
timer_state=
protected_tables=
protected_before=
protected_after=
timers_quiesced=0
compat_installed=0
durable_rows_present=0

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
}

run_sql() {
  docker exec \
    -e PGOPTIONS='-c lock_timeout=5s -c statement_timeout=240s' \
    -i "$container" psql -X -v ON_ERROR_STOP=1 \
    -U sage -d "$database" "$@"
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
  local output=$1
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
  done <"$protected_tables"
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
  if [[ "$compat_installed" -eq 1 && "$durable_rows_present" -eq 0 ]]; then
    atom_total=$(psql_scalar "
      SELECT
        (SELECT count(*) FROM memory.v5_2_atom_admission_proposal)
        + (SELECT count(*) FROM memory.v5_2_atom_admission_review)
        + (SELECT count(*) FROM memory.v5_2_atom_admission_apply)
        + (SELECT count(*) FROM memory.v5_2_atom_admission_operation)
    " 2>/dev/null)
    if [[ "$atom_total" =~ ^[0-9]+$ && "$atom_total" -gt 0 ]]; then
      durable_rows_present=1
    fi
  fi
  if [[ "$compat_installed" -eq 1 && "$durable_rows_present" -eq 0 ]]; then
    failed_phase=$phase
    phase=rollback_compatibility_after_failure
    run_sql <"$compat_rollback" >/dev/null 2>&1 || exit_code=1
    phase=$failed_phase
  fi
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
      printf 'durable_rows_present=%s\n' "$durable_rows_present"
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
git merge-base --is-ancestor "$required_production_ancestor" HEAD
for pair in \
  "$compat_migration:$expected_compat_sha" \
  "$compat_rollback:$expected_rollback_sha" \
  "$manifest:$expected_manifest_file_sha" \
  "$runner:$expected_runner_sha" \
  "$clone_test:$expected_clone_sha"; do
  file=${pair%%:*}
  expected=${pair##*:}
  [[ -f "$file" ]]
  [[ "$(sha256sum "$file" | awk '{print $1}')" == "$expected" ]]
done
[[ "$(jq -r '.manifest_sha256' "$manifest")" == "$expected_manifest_sha" ]]
[[ "$(psql_scalar "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.v5_2_atom_admission_proposal),
    (SELECT count(*) FROM memory.v5_2_atom_admission_review),
    (SELECT count(*) FROM memory.v5_2_atom_admission_apply),
    (SELECT count(*) FROM memory.v5_2_atom_admission_operation)
  )
")" == '0,0,0,0' ]]
review_preflight_before=$(function_sha \
  'memory.preflight_owner_v5_2_atom_review_v1(uuid,memory.v5_2_atom_review_decision,text,text,jsonb)')
review_apply_before=$(function_sha \
  'memory.review_owner_v5_2_atom_proposal_v1(uuid,uuid,uuid,memory.v5_2_atom_review_decision,text,text,jsonb,text)')
[[ "$review_preflight_before" == "$expected_review_preflight_before" ]]
[[ "$review_apply_before" == "$expected_review_apply_before" ]]
authenticated_health

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_2_atom_apply_${run_tag}.status"
timer_state="$snapshot_dir/memory_v1_v5_2_atom_apply_timers_${run_tag}.tsv"
protected_tables="$snapshot_dir/memory_v1_v5_2_atom_apply_tables_${run_tag}.txt"
protected_before="$snapshot_dir/memory_v1_v5_2_atom_apply_before_${run_tag}.tsv"
protected_after="$snapshot_dir/memory_v1_v5_2_atom_apply_after_${run_tag}.tsv"
install_log="$snapshot_dir/memory_v1_v5_2_atom_apply_${run_tag}.log"
preflight_output="$snapshot_dir/memory_v1_v5_2_atom_apply_preflight_${run_tag}.json"
apply_output="$snapshot_dir/memory_v1_v5_2_atom_apply_commit_${run_tag}.json"
replay_output="$snapshot_dir/memory_v1_v5_2_atom_apply_replay_${run_tag}.json"
report="$snapshot_dir/memory_v1_v5_2_atom_apply_${run_tag}.json"

phase=capture_timer_state
mapfile -t timers < <(
  systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
    | awk '{print $1}' | sort -u
)
[[ ${#timers[@]} -gt 0 ]]
: >"$timer_state"
for unit in "${timers[@]}"; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done
chmod 0600 "$timer_state"

phase=quiesce_timers
for unit in "${timers[@]}"; do
  sudo -n systemctl stop "$unit"
done
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

phase=capture_baseline
psql_scalar "
  SELECT table_name
  FROM information_schema.tables
  WHERE table_schema='memory'
    AND table_type='BASE TABLE'
    AND table_name NOT IN (
      'v5_2_atom_admission_proposal',
      'v5_2_atom_admission_review',
      'v5_2_atom_admission_apply',
      'v5_2_atom_admission_operation'
    )
  ORDER BY table_name
" >"$protected_tables"
[[ -s "$protected_tables" ]]
chmod 0600 "$protected_tables"
capture_protected "$protected_before"
qdrant_before=$(qdrant_signature)

phase=fresh_backup
database_size=$(psql_scalar 'SELECT pg_database_size(current_database())')
free_bytes=$(df --output=avail -B1 "$snapshot_dir" | tail -1 | tr -d '[:space:]')
(( free_bytes >= database_size * 2 ))
backup_partial="$snapshot_dir/.memory_pre_v5_2_atom_apply_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_2_atom_apply_${run_tag}.dump"
catalog="$backup.catalog"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$catalog"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=install_replay_compatibility
: >"$install_log"
run_sql <"$compat_migration" >>"$install_log" 2>&1
compat_installed=1
review_preflight_after=$(function_sha \
  'memory.preflight_owner_v5_2_atom_review_v1(uuid,memory.v5_2_atom_review_decision,text,text,jsonb)')
review_apply_after=$(function_sha \
  'memory.review_owner_v5_2_atom_proposal_v1(uuid,uuid,uuid,memory.v5_2_atom_review_decision,text,text,jsonb,text)')
[[ "$review_preflight_after" == "$review_preflight_before" ]]
[[ "$review_apply_after" != "$review_apply_before" ]]
run_sql <"$compat_migration" >>"$install_log" 2>&1
[[ "$(function_sha \
  'memory.preflight_owner_v5_2_atom_review_v1(uuid,memory.v5_2_atom_review_decision,text,text,jsonb)')" \
  == "$review_preflight_after" ]]
[[ "$(function_sha \
  'memory.review_owner_v5_2_atom_proposal_v1(uuid,uuid,uuid,memory.v5_2_atom_review_decision,text,text,jsonb,text)')" \
  == "$review_apply_after" ]]
chmod 0600 "$install_log"

phase=zero_write_preflight
set -a
source "$env_file"
set +a
python3 "$runner" \
  --mode preflight \
  --manifest "$manifest" \
  --output "$preflight_output"
[[ "$(jq -r '.mode' "$preflight_output")" == preflight ]]
[[ "$(jq -r '.manifest_sha256' "$preflight_output")" == "$expected_manifest_sha" ]]
[[ "$(jq -r '.persistent_writes' "$preflight_output")" == 0 ]]
[[ "$(jq '[.results[].proposal_outcome] | unique == ["applied"]' "$preflight_output")" == true ]]
[[ "$(jq '[.same_transaction_replay[].proposal_outcome] | unique == ["replayed"]' "$preflight_output")" == true ]]
[[ "$(psql_scalar "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.v5_2_atom_admission_proposal),
    (SELECT count(*) FROM memory.v5_2_atom_admission_review),
    (SELECT count(*) FROM memory.v5_2_atom_admission_apply),
    (SELECT count(*) FROM memory.v5_2_atom_admission_operation)
  )
")" == '0,0,0,0' ]]

phase=durable_owner_scoped_apply
MEMORY_V1_V5_2_ATOM_ADMISSION_APPLY=authorized \
  python3 "$runner" \
    --mode apply \
    --manifest "$manifest" \
    --output "$apply_output"
[[ "$(jq -r '.mode' "$apply_output")" == apply ]]
[[ "$(jq -r '.manifest_sha256' "$apply_output")" == "$expected_manifest_sha" ]]
[[ "$(jq -r '.persistent_writes' "$apply_output")" == 10 ]]
[[ "$(jq '[.results[].proposal_outcome] | unique == ["applied"]' "$apply_output")" == true ]]
[[ "$(jq '[.same_transaction_replay[].proposal_outcome] | unique == ["replayed"]' "$apply_output")" == true ]]
durable_rows_present=1

phase=zero_write_replay
python3 "$runner" \
  --mode replay \
  --manifest "$manifest" \
  --output "$replay_output"
[[ "$(jq -r '.mode' "$replay_output")" == replay ]]
[[ "$(jq -r '.manifest_sha256' "$replay_output")" == "$expected_manifest_sha" ]]
[[ "$(jq -r '.persistent_writes' "$replay_output")" == 0 ]]
[[ "$(jq '[.results[].proposal_outcome] | unique == ["replayed"]' "$replay_output")" == true ]]
[[ "$(jq '.same_transaction_replay | length' "$replay_output")" == 0 ]]

phase=postflight
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
  SELECT concat_ws(',',
    (
      SELECT count(*)
      FROM memory.v5_2_atom_admission_apply AS applied
      JOIN memory.v5_2_atom_admission_review AS review
        ON review.review_id=applied.review_id
      JOIN memory.v5_2_atom_admission_proposal AS proposal
        ON proposal.proposal_id=review.proposal_id
      WHERE proposal.source_packet_id='$stance_packet'::uuid
    ),
    (
      SELECT count(*)
      FROM memory.v5_2_atom_admission_apply AS applied
      JOIN memory.v5_2_atom_admission_review AS review
        ON review.review_id=applied.review_id
      JOIN memory.v5_2_atom_admission_proposal AS proposal
        ON proposal.proposal_id=review.proposal_id
      WHERE proposal.source_packet_id='$preference_packet'::uuid
    )
  )
")" == '1,0' ]]
[[ "$(psql_scalar "
  SELECT concat_ws(',',
    count(*) FILTER (WHERE operation='record_proposal'),
    count(*) FILTER (WHERE operation='record_review'),
    count(*) FILTER (WHERE operation='apply_review')
  )
  FROM memory.v5_2_atom_admission_operation
")" == '2,2,1' ]]
capture_protected "$protected_after"
cmp -s "$protected_before" "$protected_after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]
authenticated_health

phase=restore_timers
restore_timers
authenticated_health

phase=write_report
jq -n \
  --arg contract_version memory_v1_v5_2_atom_admission_apply_report_v1 \
  --arg run_tag "$run_tag" \
  --arg head "$(git rev-parse HEAD)" \
  --arg compat_migration_sha256 "$expected_compat_sha" \
  --arg compat_rollback_sha256 "$expected_rollback_sha" \
  --arg manifest_file_sha256 "$expected_manifest_file_sha" \
  --arg manifest_sha256 "$expected_manifest_sha" \
  --arg runner_sha256 "$expected_runner_sha" \
  --arg backup "$backup" \
  --arg backup_sha256 "$(sha256sum "$backup" | awk '{print $1}')" \
  --arg preflight_output "$preflight_output" \
  --arg apply_output "$apply_output" \
  --arg replay_output "$replay_output" \
  --arg qdrant_signature "$qdrant_after" \
  --arg review_preflight_before "$review_preflight_before" \
  --arg review_preflight_after "$review_preflight_after" \
  --arg review_apply_before "$review_apply_before" \
  --arg review_apply_after "$review_apply_after" \
  '{
    contract_version:$contract_version,
    run_tag:$run_tag,
    head:$head,
    artifacts:{
      compatibility_migration_sha256:$compat_migration_sha256,
      compatibility_rollback_sha256:$compat_rollback_sha256,
      manifest_file_sha256:$manifest_file_sha256,
      manifest_sha256:$manifest_sha256,
      runner_sha256:$runner_sha256
    },
    backup:{path:$backup,sha256:$backup_sha256},
    outputs:{
      preflight:$preflight_output,
      apply:$apply_output,
      replay:$replay_output
    },
    results:{
      proposals_created:2,
      reviews_created:2,
      authorizations_created:1,
      operation_rows_created:5,
      replay:"passed_zero_write",
      account_isolation:"passed",
      non_target_memory_unchanged:true,
      relational_staging_rows_created:0,
      entity_rows_created:0,
      observation_rows_created:0,
      claim_rows_created:0,
      qdrant_unchanged:true,
      timers_restored:true,
      retrieval_or_prompt_influence:false
    },
    qdrant_signature:$qdrant_signature,
    function_hashes:{
      review_preflight_before:$review_preflight_before,
      review_preflight_after:$review_preflight_after,
      review_apply_before:$review_apply_before,
      review_apply_after:$review_apply_after
    }
  }' >"$report"
chmod 0600 "$report"

phase=complete
printf '%s\n' \
  "MEMORY_V1_V5_2_ATOM_ADMISSION_APPLY=PASS" \
  "head=$(git rev-parse HEAD)" \
  "backup=$backup" \
  "report=$report" \
  "manifest_sha256=$expected_manifest_sha" \
  "rows=2,2,1,5" \
  "qdrant_signature=$qdrant_after"
