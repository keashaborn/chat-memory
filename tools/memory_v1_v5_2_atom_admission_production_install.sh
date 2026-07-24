#!/usr/bin/env bash
set -euo pipefail

# Server: seebx backend only.
# Installs the dormant V5.2 atom proposal/review/apply boundary and runs the
# complete rollback-only suite. It never generates proposals or stages data.

if [[ "${MEMORY_V1_V5_2_ATOM_ADMISSION_INSTALL:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_ATOM_ADMISSION_INSTALL=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
env_file=/opt/chat-memory/.env
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_atom_admission_install.lock
migration=ops/sql/20260724_memory_v1_v5_2_atom_admission.sql
rollback=ops/sql/20260724_memory_v1_v5_2_atom_admission_rollback.sql
security_test=tests/memory_v1_v5_2_atom_admission.sql
clone_test=tools/memory_v1_v5_2_atom_admission_production_clone.sh
required_base=786e8a4a6b0840d2ba2692eadf34408a24e474b9
expected_migration_sha=22df61f0751bf765f16e71f941b37e05a8f9aa4a92dc3a44fa4146ac727a2e1d
expected_rollback_sha=9768efcdfa5a803111e82142b7ad542c0a7471e7a5cdfb76c83b199eb5b9a814
expected_security_test_sha=04bff2a008e508dae13d6a8a7ad6d49c6cab3f1b5d42ac01d1c316f9d763143d
expected_clone_test_sha=28258051c117e8ec0a3c7420ca2878142b594abdfea5243b6250752079dfa055
target_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
stance_packet=78ca7a3e-e136-535a-9fe6-1d83aefff806
preference_packet=a5624f05-8d75-5b96-bfd7-9f56145f7ad9
superseded_packet=e21e39bb-20fe-5b95-bc72-77fd774f0faf

phase=initialization
run_tag=
status_file=
timer_state=
protected_tables=
protected_before=
protected_after=
timers_quiesced=0
schema_installed=0
installation_committed=0

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
  if [[ "$installation_committed" -eq 0 && "$schema_installed" -eq 1 ]]; then
    failed_phase=$phase
    phase=rollback_schema_after_failure
    run_sql <"$rollback" >/dev/null 2>&1 || exit_code=1
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
for pair in \
  "$migration:$expected_migration_sha" \
  "$rollback:$expected_rollback_sha" \
  "$security_test:$expected_security_test_sha" \
  "$clone_test:$expected_clone_test_sha"; do
  file=${pair%%:*}
  expected=${pair##*:}
  [[ -f "$file" ]]
  [[ "$(sha256sum "$file" | awk '{print $1}')" == "$expected" ]]
done
[[ "$(psql_scalar "
  SELECT (
    to_regrole('memory_v5_2_atom_admission_maintainer') IS NULL
    AND to_regclass('memory.v5_2_atom_admission_proposal') IS NULL
    AND to_regprocedure(
      'memory.plan_owner_v5_2_atom_admission_v1(uuid)'
    ) IS NULL
  )::int
")" == 1 ]]
authenticated_health

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_2_atom_admission_${run_tag}.status"
timer_state="$snapshot_dir/memory_v1_v5_2_atom_admission_timers_${run_tag}.tsv"
protected_tables="$snapshot_dir/memory_v1_v5_2_atom_admission_tables_${run_tag}.txt"
protected_before="$snapshot_dir/memory_v1_v5_2_atom_admission_before_${run_tag}.tsv"
protected_after="$snapshot_dir/memory_v1_v5_2_atom_admission_after_${run_tag}.tsv"
install_log="$snapshot_dir/memory_v1_v5_2_atom_admission_${run_tag}.log"
report="$snapshot_dir/memory_v1_v5_2_atom_admission_${run_tag}.json"

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
  WHERE table_schema='memory' AND table_type='BASE TABLE'
  ORDER BY table_name
" >"$protected_tables"
[[ -s "$protected_tables" ]]
chmod 0600 "$protected_tables"
capture_protected "$protected_before"
qdrant_before=$(qdrant_signature)
guard_before=$(psql_scalar "
  SELECT encode(public.digest(pg_get_functiondef(
    'memory.guard_v5_2_terminal_evidence_from_stage_v1()'::regprocedure
  ),'sha256'),'hex')
")

phase=fresh_backup
database_size=$(psql_scalar 'SELECT pg_database_size(current_database())')
free_bytes=$(df --output=avail -B1 "$snapshot_dir" | tail -1 | tr -d '[:space:]')
(( free_bytes >= database_size * 2 ))
backup_partial="$snapshot_dir/.memory_pre_v5_2_atom_admission_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_2_atom_admission_${run_tag}.dump"
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

phase=install_schema
: >"$install_log"
run_sql <"$migration" >>"$install_log" 2>&1
schema_installed=1
run_sql <"$migration" >>"$install_log" 2>&1

phase=rollback_only_security_test
run_sql \
  -v target_owner="$target_owner" \
  -v other_owner="$other_owner" \
  -v stance_packet="$stance_packet" \
  -v preference_packet="$preference_packet" \
  -v superseded_packet="$superseded_packet" \
  <"$security_test" >>"$install_log" 2>&1
chmod 0600 "$install_log"

phase=postflight
[[ "$(psql_scalar "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.v5_2_atom_admission_proposal),
    (SELECT count(*) FROM memory.v5_2_atom_admission_review),
    (SELECT count(*) FROM memory.v5_2_atom_admission_apply),
    (SELECT count(*) FROM memory.v5_2_atom_admission_operation)
  )
")" == '0,0,0,0' ]]
capture_protected "$protected_after"
cmp -s "$protected_before" "$protected_after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]
guard_after=$(psql_scalar "
  SELECT encode(public.digest(pg_get_functiondef(
    'memory.guard_v5_2_terminal_evidence_from_stage_v1()'::regprocedure
  ),'sha256'),'hex')
")
[[ "$guard_after" != "$guard_before" ]]
[[ "$(psql_scalar "
  SELECT (
    NOT has_table_privilege(
      'brains_app','memory.v5_2_atom_admission_proposal','SELECT'
    )
    AND has_function_privilege(
      'brains_app',
      'memory.plan_owner_v5_2_atom_admission_v1(uuid)','EXECUTE'
    )
    AND NOT has_function_privilege(
      'brains_app',
      'memory.v5_2_atom_stage_projection_authorized_v1(uuid,uuid,jsonb,text)',
      'EXECUTE'
    )
  )::int
")" == 1 ]]
authenticated_health

phase=restore_timers
restore_timers
authenticated_health

phase=write_report
jq -n \
  --arg contract_version memory_v1_v5_2_atom_admission_install_report_v1 \
  --arg run_tag "$run_tag" \
  --arg head "$(git rev-parse HEAD)" \
  --arg migration_sha256 "$expected_migration_sha" \
  --arg rollback_sha256 "$expected_rollback_sha" \
  --arg security_test_sha256 "$expected_security_test_sha" \
  --arg backup "$backup" \
  --arg backup_sha256 "$(sha256sum "$backup" | awk '{print $1}')" \
  --arg qdrant_signature "$qdrant_after" \
  --arg prior_guard_sha256 "$guard_before" \
  --arg installed_guard_sha256 "$guard_after" \
  '{
    contract_version:$contract_version,
    run_tag:$run_tag,
    head:$head,
    migration_sha256:$migration_sha256,
    rollback_sha256:$rollback_sha256,
    security_test_sha256:$security_test_sha256,
    backup:{path:$backup,sha256:$backup_sha256},
    results:{
      schema_installed:true,
      atom_rows_created:0,
      security_suite:"passed_rolled_back",
      account_isolation:"passed",
      replay:"passed_zero_write",
      prior_records_unchanged:true,
      qdrant_unchanged:true,
      timers_restored:true,
      proposal_generation_active:false,
      relational_staging_active:false,
      retrieval_or_prompt_influence:false
    },
    qdrant_signature:$qdrant_signature,
    prior_guard_sha256:$prior_guard_sha256,
    installed_guard_sha256:$installed_guard_sha256
  }' >"$report"
chmod 0600 "$report"

phase=complete
installation_committed=1
printf '%s\n' \
  "MEMORY_V1_V5_2_ATOM_ADMISSION_INSTALL=PASS" \
  "head=$(git rev-parse HEAD)" \
  "backup=$backup" \
  "report=$report" \
  "migration_sha256=$expected_migration_sha" \
  "qdrant_signature=$qdrant_after"
