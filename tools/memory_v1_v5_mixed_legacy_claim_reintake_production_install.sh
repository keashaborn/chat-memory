#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs additive mixed preference/legacy-claim reintake
# functions. Writes no memory rows and restores the exact discovered Memory V1
# timer inventory.

if [[ "${MEMORY_V1_V5_MIXED_REINTAKE_INSTALL:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_MIXED_REINTAKE_INSTALL=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_mixed_reintake_install.lock
required_ancestor=d195fd5870625954a796b6f32de30112774e9dda
owner_a=d839b4bc-0bd2-4f2d-aafe-0f3f75883db8
owner_b=557ea042-cb82-48f8-9429-472e96c957ef
selector=20260719_v5_mixed_preference_legacy_reintake_v1
migration=ops/sql/20260719_memory_v1_v5_mixed_legacy_claim_reintake.sql
rollback=ops/sql/20260719_memory_v1_v5_mixed_legacy_claim_reintake_rollback.sql
test_sql=tests/memory_v1_v5_mixed_legacy_claim_reintake.sql
worker=scripts/memory_v1_v5_mixed_legacy_claim_reintake.py
worker_test=scripts/memory_v1_v5_mixed_legacy_claim_reintake_test.py
clone_test=tools/memory_v1_v5_mixed_legacy_claim_reintake_clone.sh

declare -A expected_sha256=(
  ["$migration"]="be78a2a441d96027fc54b092904c8e5d79f3025ad04759c034db0735b3c09987"
  ["$rollback"]="4fdf8ed319c23d85a859ce38254124387cdfa3d6b4bf9c621662e3de755e80e1"
  ["$test_sql"]="5bd71bb59bb9c11cf7101a7a1a4223c25b4c4deb27182cd18ea628814304935f"
  ["$worker"]="3e8170aca8ad5ba2ecaf01351780f53c28b5d1797f095a2a9d4886f0f1587772"
  ["$worker_test"]="3d87dfc9000b3b7925306500682849ab9a14060171ddba0ffdc2d859cd83625f"
  ["$clone_test"]="09a8f5acca3443d2aea3f49ec56f6185c0b88ee7b0ecaffd8c47145ba8eec008"
)

required_timers=(
  memory-v1-consolidation.timer
  memory-v1-deferred-reconciliation-scan.timer
  memory-v1-evidence-intake-dispatcher.timer
  memory-v1-governance.timer
  memory-v1-projection.timer
  memory-v1-v5-chat-capture.timer
  memory-v1-v5-local-auto-resolution.timer
  memory-v1-v5-local-auto-stage.timer
  memory-v1-v5-local-claim-projection.timer
  memory-v1-v5-local-entailment.timer
  memory-v1-v5-local-entity-validation.timer
  memory-v1-v5-local-inference-scheduler.timer
  memory-v1-v5-local-packet-router.timer
)

timer_state=$(mktemp /tmp/memory-v1-v5-mixed-reintake-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-mixed-reintake-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-mixed-reintake-before.XXXXXX)
after=$(mktemp /tmp/memory-v1-v5-mixed-reintake-after.XXXXXX)
clone_output=$(mktemp /tmp/memory-v1-v5-mixed-reintake-clone.XXXXXX)
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
  == memory_v1_v5_mixed_legacy_claim_reintake_clone:\ PASS ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_mixed_reintake_install_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_mixed_reintake_install_${run_tag}.json"

phase=inventory_timers
while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
timer_count=$(wc -l <"$timer_state")
(( timer_count >= 13 && timer_count <= 32 ))
for unit in "${required_timers[@]}"; do
  grep -Fqx "$unit"$'\t'"$(systemctl is-enabled "$unit")"$'\t'"$(systemctl is-active "$unit")" \
    "$timer_state"
done

phase=quiesce_timers
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
partial="$snapshot_dir/.memory_pre_v5_mixed_reintake_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_mixed_reintake_${run_tag}.dump"
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
[[ "$(scalar "SELECT to_regprocedure('memory.plan_owner_v5_mixed_legacy_claim_reintake_v1(text,integer,uuid)') IS NOT NULL")" == t ]]
[[ "$(scalar "SELECT to_regprocedure('memory.enqueue_owner_v5_mixed_legacy_claim_reintake_v1(uuid,text,text,text,text)') IS NOT NULL")" == t ]]

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
  --argjson timer_count "$timer_count" \
  --argjson selector_rows "$selector_rows_after" \
  '{contract_version:"memory_v1_v5_mixed_legacy_claim_reintake_install_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    backup:{path:$backup,sha256:$backup_sha256},
    scope:{selector_version:"20260719_v5_mixed_preference_legacy_reintake_v1",
      production_rows_written:0,external_model_calls:0,qdrant_writes:0,
      prompt_influence:0},
    checks:{hash_locked_artifacts:true,production_clone_passed:true,
      fresh_backup:true,all_discovered_memory_timers_quiesced:true,
      additive_functions_only:true,rollback_only_security_test_passed:true,
      owner_isolation_passed:true,preference_lane_unchanged:true,
      all_table_rows_unchanged:true,selector_rows_unchanged:true,
      qdrant_unchanged:true,exact_timer_states_restored:true},
    metrics:{discovered_timer_count:$timer_count,
      existing_selector_rows:$selector_rows},
    qdrant_sha256:$qdrant_sha256,
    hard_stop:"before_production_mixed_reintake_apply"}' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
jq -e '.checks | to_entries | map(.value == true) | all' "$report" >/dev/null

phase=complete
printf '%s\n' 'memory_v1_v5_mixed_legacy_claim_reintake_production_install: PASS'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
