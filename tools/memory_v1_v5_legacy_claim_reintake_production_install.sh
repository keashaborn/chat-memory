#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the additive legacy-only V5 reintake planner
# and enqueue API. The installation writes no memory rows and restores the
# exact pre-run state of every Memory V1 timer.

if [[ "${MEMORY_V1_V5_LEGACY_REINTAKE_INSTALL:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_LEGACY_REINTAKE_INSTALL=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_legacy_reintake_install.lock
required_ancestor=f7fe07b3eeeec6f883d495eb1d5d14f79f421d8b
owner_a=557ea042-cb82-48f8-9429-472e96c957ef
owner_b=d839b4bc-0bd2-4f2d-aafe-0f3f75883db8
selector=20260719_v5_legacy_claim_reintake_v1
migration=ops/sql/20260719_memory_v1_v5_legacy_claim_reintake.sql
rollback=ops/sql/20260719_memory_v1_v5_legacy_claim_reintake_rollback.sql
test_sql=tests/memory_v1_v5_legacy_claim_reintake.sql
worker=scripts/memory_v1_v5_legacy_claim_reintake.py
worker_test=scripts/memory_v1_v5_legacy_claim_reintake_test.py
clone_test=tools/memory_v1_v5_legacy_claim_reintake_clone.sh

declare -A expected_sha256=(
  ["$migration"]="b197fa613316e66a63dccc147cd695d7715235fa67c70ad593c9a0530d92af88"
  ["$rollback"]="02e01b5e346521c506ba88deb4aec109591765fba0e232cac574d5c7a5ada67d"
  ["$test_sql"]="e0473efc66ee645aa5583ceca652551a75ecf2f25ea09c00f022fb89bf7ced76"
  ["$worker"]="cc27ec9d74ce7c825e842ef2900416f1e48aebf9cd3f3808bd9062d52a1ec278"
  ["$worker_test"]="b254b22ee9e7d0dedb161a50e76b48c85c7619a0e1095d088fb8aa25f6074e5e"
  ["$clone_test"]="642f3a6a1eb3309a08142414bde3cde86dba28e46723eae22c22d932f3fa2f1c"
)

timer_state=$(mktemp /tmp/memory-v1-v5-legacy-reintake-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-legacy-reintake-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-legacy-reintake-before.XXXXXX)
after=$(mktemp /tmp/memory-v1-v5-legacy-reintake-after.XXXXXX)
clone_output=$(mktemp /tmp/memory-v1-v5-legacy-reintake-clone.XXXXXX)
chmod 0600 "$timer_state" "$table_list" "$before" "$after" "$clone_output"
timers_quiesced=0
migration_installed=0
installation_committed=0
phase=initialization
status_file=

run_sql() {
  docker exec -i "$container" psql -U sage -d "$database" -X \
    -v ON_ERROR_STOP=1 "$@"
}

scalar() {
  docker exec "$container" psql -U sage -d "$database" -X -At \
    -v ON_ERROR_STOP=1 -c "$1" | tr -d '[:space:]'
}

restore_timers() {
  [[ "$timers_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
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
  if [[ "$migration_installed" -eq 1 && "$installation_committed" -eq 0 ]]; then
    run_sql <"$rollback" >/dev/null 2>&1 || exit_code=1
  fi
  restore_timers || exit_code=1
  rm -f "$timer_state" "$table_list" "$before" "$after" "$clone_output"
  if [[ -n "$status_file" ]]; then
    printf 'phase=%s\nexit_code=%s\ncompleted_at=%s\n' \
      "$phase" "$exit_code" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$exit_code"
}
trap record_exit EXIT

capture_state() {
  local output=$1 schema table state
  : >"$output"
  while IFS=$'\t' read -r schema table; do
    state=$(scalar "SELECT count(*)::text || E'\\t' ||
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

for required in "${!expected_sha256[@]}"; do
  [[ -f "$required" ]]
  [[ "$(sha256sum "$required" | awk '{print $1}')" \
    == "${expected_sha256[$required]}" ]]
done
[[ -z "$(git status --porcelain)" ]]
git merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(systemctl --failed --no-legend --no-pager | wc -l)" -eq 0 ]]

phase=production_clone_proof
bash "$clone_test" >"$clone_output"
[[ "$(tr -d '\r\n' <"$clone_output")" \
  == memory_v1_v5_legacy_claim_reintake_clone:\ PASS ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_legacy_reintake_install_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_legacy_reintake_install_${run_tag}.json"

phase=quiesce_timers
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

phase=fresh_backup
partial="$snapshot_dir/.memory_pre_v5_legacy_reintake_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_legacy_reintake_${run_tag}.dump"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$partial"
[[ -s "$partial" ]]
docker exec -i "$container" pg_restore -l <"$partial" >"$backup.catalog"
[[ -s "$backup.catalog" ]]
mv "$partial" "$backup"
chmod 0600 "$backup" "$backup.catalog"
backup_sha=$(sha256sum "$backup" | awk '{print $1}')
printf '%s  %s\n' "$backup_sha" "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline
docker exec "$container" psql -U sage -d "$database" -X -At -F $'\t' \
  -c "SELECT table_schema,table_name FROM information_schema.tables
      WHERE table_type='BASE TABLE' AND table_schema IN ('memory','public')
      ORDER BY table_schema,table_name" >"$table_list"
capture_state "$before"
qdrant_before=$(qdrant_signature)
selector_rows_before=$(scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE selector_version='$selector'")

phase=install_additive_functions
run_sql <"$migration" >/dev/null
migration_installed=1

phase=rollback_only_security_test
run_sql -v owner_a="$owner_a" -v owner_b="$owner_b" <"$test_sql" >/dev/null

phase=postflight
capture_state "$after"
cmp -s "$before" "$after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]
selector_rows_after=$(scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE selector_version='$selector'")
[[ "$selector_rows_before" == "$selector_rows_after" ]]
[[ "$(scalar "SELECT to_regprocedure('memory.plan_owner_v5_legacy_claim_reintake_v1(text,integer,uuid)') IS NOT NULL")" == t ]]
[[ "$(scalar "SELECT to_regprocedure('memory.enqueue_owner_v5_legacy_claim_reintake_v1(uuid,text,text,text,text)') IS NOT NULL")" == t ]]

phase=restore_timers
restore_timers
while IFS=$'\t' read -r unit enabled active; do
  [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
  [[ "$(systemctl is-active "$unit")" == "$active" ]]
done <"$timer_state"

installation_committed=1
migration_installed=0
phase=report
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg qdrant_sha256 "$qdrant_after" \
  --argjson selector_rows "$selector_rows_after" \
  '{contract_version:"memory_v1_v5_legacy_claim_reintake_install_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    backup:{path:$backup,sha256:$backup_sha256},
    scope:{selector_version:"20260719_v5_legacy_claim_reintake_v1",
      production_rows_written:0,external_model_calls:0,qdrant_writes:0,
      prompt_influence:0},
    checks:{hash_locked_artifacts:true,production_clone_passed:true,
      fresh_backup:true,all_memory_timers_quiesced:true,
      additive_functions_only:true,rollback_only_security_test_passed:true,
      owner_isolation_passed:true,all_table_rows_unchanged:true,
      selector_rows_unchanged:true,qdrant_unchanged:true,
      exact_timer_states_restored:true},
    metrics:{existing_selector_rows:$selector_rows},
    qdrant_sha256:$qdrant_sha256,
    hard_stop:"before_production_reintake_apply"}' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
jq -e '.checks | to_entries | map(.value == true) | all' "$report" >/dev/null

phase=complete
printf '%s\n' 'memory_v1_v5_legacy_claim_reintake_production_install: PASS'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
