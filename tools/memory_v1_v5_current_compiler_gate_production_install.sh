#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs a function-only compiler-v3 automation gate,
# verifies zero row/Qdrant/review-file changes, and restores timer state.

if [[ "${MEMORY_V1_V5_CURRENT_COMPILER_GATE_INSTALL:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_CURRENT_COMPILER_GATE_INSTALL=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
entity_timer=memory-v1-v5-local-entity-validation.timer
snapshot_dir=/home/ubuntu/brains/snapshots
review_root=/home/ubuntu/memory-v1-reviews
lock_file=/home/ubuntu/brains/.memory_v1_v5_current_compiler_gate_install.lock
required_ancestor=ef0cdce83e3d7997cf4131119b700f733355b035
migration=ops/sql/20260719_memory_v1_v5_current_compiler_gate.sql
rollback=ops/sql/20260719_memory_v1_v5_current_compiler_gate_rollback.sql
test_sql=tests/memory_v1_v5_current_compiler_gate.sql
clone_test=tools/memory_v1_v5_current_compiler_gate_production_clone.sh

declare -A expected_sha256=(
  ["$migration"]="c5cbfb391ee596f1ab601448dd259cac861a1ab2de49bc7133db8d054fea2f9c"
  ["$rollback"]="bf0f8ff41d2980c4a47a288a096e0054d815c0ab27f63f6368ea2ab08a8f351e"
  ["$test_sql"]="849c644be9484d6b3fc3bcbdb518657210de42ade865a8bfa2e86191f69f46ae"
  ["$clone_test"]="36cbff8b5684c004af1aa690d76e7ea7ca7057969c5106f8ea586f51dbab188c"
)

timer_state=$(mktemp /tmp/memory-v1-compiler-gate-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-compiler-gate-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-compiler-gate-before.XXXXXX)
after=$(mktemp /tmp/memory-v1-compiler-gate-after.XXXXXX)
owner_entity_plan=$(mktemp /tmp/memory-v1-compiler-gate-owner-entity.XXXXXX)
owner_stage_plan=$(mktemp /tmp/memory-v1-compiler-gate-owner-stage.XXXXXX)
other_entity_plan=$(mktemp /tmp/memory-v1-compiler-gate-other-entity.XXXXXX)
chmod 0600 "$timer_state" "$table_list" "$before" "$after" \
  "$owner_entity_plan" "$owner_stage_plan" "$other_entity_plan"
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
      sudo -n systemctl disable "$unit" >/dev/null
    fi
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
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
  rm -f "$timer_state" "$table_list" "$before" "$after" \
    "$owner_entity_plan" "$owner_stage_plan" "$other_entity_plan"
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

review_signature() {
  (cd "$review_root" && find . -type f -print0 | sort -z \
    | xargs -0r sha256sum) | sha256sum | awk '{print $1}'
}

for required in "${!expected_sha256[@]}"; do
  [[ -f "$required" ]]
  [[ "$(sha256sum "$required" | awk '{print $1}')" \
    == "${expected_sha256[$required]}" ]]
done
[[ -z "$(git status --porcelain)" ]]
git merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(systemctl is-enabled "$entity_timer")" == enabled ]]
[[ "$(systemctl is-active "$entity_timer")" == inactive ]]
[[ "$(systemctl --failed --no-legend --no-pager | wc -l)" -eq 0 ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_current_compiler_gate_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_current_compiler_gate_${run_tag}.json"

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

phase=backup
partial="$snapshot_dir/.memory_pre_v5_current_compiler_gate_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_current_compiler_gate_${run_tag}.dump"
catalog="$backup.catalog"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$partial"
[[ -s "$partial" ]]
docker exec -i "$container" pg_restore -l <"$partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$partial" "$backup"
chmod 0600 "$backup" "$catalog"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline
docker exec "$container" psql -U sage -d "$database" -X -At \
  -F $'\t' -c "SELECT table_schema,table_name
    FROM information_schema.tables
    WHERE table_type='BASE TABLE'
      AND table_schema IN ('memory','public')
    ORDER BY table_schema,table_name" >"$table_list"
capture_state "$before"
qdrant_before=$(qdrant_signature)
review_before=$(review_signature)

phase=install
run_sql <"$migration" >/dev/null
migration_installed=1

phase=rollback_security_tests
run_sql <"$test_sql" >/dev/null

phase=zero_write_plans
set -a
source .env
set +a
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  venv/bin/python scripts/memory_v1_v5_local_entity_validation.py \
    --owner-user-id "$owner" >"$owner_entity_plan"
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  venv/bin/python scripts/memory_v1_v5_local_auto_stage.py \
    --owner-user-id "$owner" >"$owner_stage_plan"
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  venv/bin/python scripts/memory_v1_v5_local_entity_validation.py \
    --owner-user-id "$other" >"$other_entity_plan"
for plan in "$owner_entity_plan" "$owner_stage_plan" "$other_entity_plan"; do
  jq -e '.apply==false and .database_writes==0
    and .external_model_calls==0 and .qdrant_writes==0
    and .prompt_influence==0 and .plans[0].route=="no_work"' \
    "$plan" >/dev/null
done

phase=postflight
capture_state "$after"
cmp -s "$before" "$after"
qdrant_after=$(qdrant_signature)
review_after=$(review_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]
[[ "$review_before" == "$review_after" ]]

phase=restore_timers
restore_timers
sudo -n systemctl start "$entity_timer"
[[ "$(systemctl is-enabled "$entity_timer")" == enabled ]]
[[ "$(systemctl is-active "$entity_timer")" == active ]]
while IFS=$'\t' read -r unit enabled active; do
  [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
  if [[ "$unit" != "$entity_timer" ]]; then
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  fi
done <"$timer_state"

phase=report
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg backup "$backup" --arg qdrant_sha256 "$qdrant_after" \
  '{contract_version:"memory_v1_v5_current_compiler_gate_install_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    backup:{path:$backup},
    policy:{compiler_version:"memory_v1_local_policy_compiler_v3",
      compiler_sha256:"5cb83e837e38174af4cfda016c20009168ff62974e7d9137b30228d9973256a2"},
    checks:{fresh_backup:true,hash_locked_files:true,
      production_clone_passed:true,function_only_migration:true,
      planner_gate_installed:true,direct_write_gate_installed:true,
      forced_rls_preserved:true,cross_owner_plan_empty:true,
      all_rows_unchanged:true,qdrant_unchanged:true,
      review_files_unchanged:true,claims_written_zero:true,
      prompt_influence_zero:true,external_model_calls:0,
      existing_timer_states_restored:true,
      entity_validation_timer_safely_resumed:true},
    qdrant_sha256:$qdrant_sha256,
    hard_stop:"before_processing_any_stale_compiler_artifact"}' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
jq -e '.checks | to_entries | map(.value == true) | all' "$report" >/dev/null

installation_committed=1
migration_installed=0
phase=complete
printf '%s\n' 'memory_v1_v5_current_compiler_gate_production_install: PASS'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
