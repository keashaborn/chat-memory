#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the owner/thread/project/component binding API
# and exact-scope read-only project shadow reader. The rollback-only test writes
# no durable Memory V1 rows and the API is not connected to prompt assembly.

if [[ "${MEMORY_V1_V5_PROJECT_SHADOW_INSTALL:-}" != "authorized" ]]; then
  echo 'MEMORY_V1_V5_PROJECT_SHADOW_INSTALL=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
migration=ops/sql/20260718_memory_v1_v5_project_shadow_read_api.sql
rollback=ops/sql/20260718_memory_v1_v5_project_shadow_read_api_rollback.sql
test_sql=tests/memory_v1_v5_project_shadow_read_api.sql
required_ancestor=b6ace3734f4fec5124c58e2941f7079f81d40c85
expected_migration_sha=b92c8ea3ee67ea28580f81cdf08f1c80b23b164f51d82990f1de570b293e3375
expected_rollback_sha=844adf956085dd08e3e3b8d4bb9dc13d5d21e71880214eb5c1393248aa0d30d9
expected_test_sha=893474be70ae38fd28bbf5bb4f727714c6be9961991917ab995fd85abd639eb7
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_project_shadow_install.lock
phase=initialization
schema_installed=0
quiesced=0
run_id=
status_file=
table_list=$(mktemp /tmp/memory-v1-v5-project-shadow-tables.XXXXXX)
baseline=
post=
timer_state=$(mktemp /tmp/memory-v1-v5-project-shadow-timers.XXXXXX)
units=(
  memory-v1-consolidation.timer
  memory-v1-deferred-reconciliation-scan.timer
  memory-v1-evidence-intake-dispatcher.timer
  memory-v1-governance.timer
  memory-v1-projection.timer
  memory-v1-v5-chat-capture.timer
)

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
}

run_sql_file() {
  docker exec -e PGOPTIONS='-c lock_timeout=5s -c statement_timeout=120s' \
    -i "$container" psql -X -v ON_ERROR_STOP=1 -U sage -d "$database" \
    <"$repo_root/$1"
}

restore_timers() {
  [[ "$quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$timer_state"
  quiesced=0
}

record_exit() {
  exit_code=$?
  if [[ "$exit_code" -ne 0 && "$schema_installed" -eq 1 ]]; then
    phase=automatic_rollback_after_failure
    run_sql_file "$rollback" \
      >"$snapshot_dir/memory_v1_v5_project_shadow_rollback_${run_id}.log" 2>&1 \
      || true
  fi
  restore_timers || exit_code=1
  rm -f "$table_list" "$timer_state"
  if [[ -n "$status_file" ]]; then
    {
      printf 'run_id=%s\n' "$run_id"
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

capture_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    state=$(psql_scalar "
      SELECT count(*)::text || E'\\t' ||
             encode(public.digest(convert_to(coalesce(string_agg(
               row_json,E'\\n' ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value
      ) AS rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

[[ -z "$(git -C "$repo_root" status --porcelain)" ]] || {
  echo 'production install requires a clean Git worktree' >&2
  exit 1
}
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(sha256sum "$repo_root/$migration" | awk '{print $1}')" == "$expected_migration_sha" ]]
[[ "$(sha256sum "$repo_root/$rollback" | awk '{print $1}')" == "$expected_rollback_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | awk '{print $1}')" == "$expected_test_sha" ]]

exec 9>"$lock_file"
flock -n 9 || {
  echo 'another V5 project shadow install holds the lock' >&2
  exit 1
}
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_project_shadow_install_${run_id}.status"
baseline="$snapshot_dir/memory_v1_v5_project_shadow_baseline_${run_id}.tsv"
post="$snapshot_dir/memory_v1_v5_project_shadow_post_${run_id}.tsv"

phase=preflight
[[ "$(psql_scalar "SELECT (
  to_regprocedure('memory.read_v5_shadow_project_knowledge(uuid,integer)') IS NULL
  AND to_regprocedure('memory.apply_owner_project_thread_component_binding_v5(uuid,uuid,uuid,uuid,uuid,uuid,text,text,text)') IS NULL
  AND to_regclass('memory.project_thread_component_binding_event_v5') IS NULL
  AND to_regrole('memory_v5_reader') IS NOT NULL
  AND to_regrole('memory_v5_extraction_maintainer') IS NOT NULL
)::int")" == 1 ]]

: >"$timer_state"
for unit in "${units[@]}"; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done
quiesced=1
for unit in "${units[@]}"; do
  sudo -n systemctl stop "$unit"
  [[ "$(systemctl is-active "$unit")" == inactive ]]
done

phase=backup
backup_partial="$snapshot_dir/.memory_pre_v5_project_shadow_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_project_shadow_${run_id}.dump"
catalog="$backup.catalog"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$catalog"
backup_sha=$(sha256sum "$backup" | awk '{print $1}')
printf '%s  %s\n' "$backup_sha" "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline_capture
psql_scalar "
  SELECT table_name FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
  ORDER BY table_name
" >"$table_list"
capture_state "$baseline"
qdrant_before=$(qdrant_signature)

phase=schema_install
install_log="$snapshot_dir/memory_v1_v5_project_shadow_install_${run_id}.log"
run_sql_file "$migration" >"$install_log" 2>&1
schema_installed=1

phase=rollback_only_security_test
run_sql_file "$test_sql" >>"$install_log" 2>&1
chmod 0600 "$install_log"

phase=postflight
capture_state "$post"
cmp -s "$baseline" "$post" || {
  diff -u "$baseline" "$post" >&2 || true
  echo 'Memory V1 row state changed during project shadow installation' >&2
  exit 1
}
[[ "$(psql_scalar "SELECT count(*) FROM memory.project_thread_component_binding_event_v5")" == 0 ]]
[[ "$(psql_scalar "SELECT pg_get_userbyid((SELECT proowner FROM pg_proc WHERE oid='memory.read_v5_shadow_project_knowledge(uuid,integer)'::regprocedure))")" == memory_v5_reader ]]
[[ "$(psql_scalar "SELECT pg_get_userbyid((SELECT proowner FROM pg_proc WHERE oid='memory.apply_owner_project_thread_component_binding_v5(uuid,uuid,uuid,uuid,uuid,uuid,text,text,text)'::regprocedure))")" == memory_v5_extraction_maintainer ]]
[[ "$(psql_scalar "SELECT has_function_privilege('brains_app','memory.read_v5_shadow_project_knowledge(uuid,integer)','EXECUTE')::int")" == 1 ]]
[[ "$(psql_scalar "SELECT has_function_privilege('brains_app','memory.apply_owner_project_thread_component_binding_v5(uuid,uuid,uuid,uuid,uuid,uuid,text,text,text)','EXECUTE')::int")" == 1 ]]
[[ "$(psql_scalar "SELECT count(*) FROM information_schema.role_table_grants WHERE grantee='brains_app' AND table_schema='memory' AND table_name='project_thread_component_binding_event_v5'")" == 0 ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

restore_timers

phase=report
report="$snapshot_dir/memory_v1_v5_project_shadow_install_${run_id}.json"
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git -C "$repo_root" rev-parse HEAD)" \
  --arg backup_path "$backup" \
  --arg backup_sha256 "$backup_sha" \
  --arg baseline "$baseline" \
  --arg post "$post" \
  --arg log "$install_log" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{
    contract_version:"memory_v1_v5_project_shadow_install_report_v1",
    completed_at:$completed_at,
    head_commit:$head_commit,
    backup:{path:$backup_path,sha256:$backup_sha256},
    evidence:{baseline:$baseline,post:$post,log:$log,qdrant_sha256:$qdrant_sha256},
    checks:{
      restricted_component_binding_writer:true,
      append_only_component_binding:true,
      exact_thread_project_component_scope:true,
      active_evidence_required:true,
      applied_projection_required:true,
      cross_owner_rejected:true,
      replay_zero_write:true,
      rollback_only_test:true,
      memory_rows_unchanged:true,
      qdrant_unchanged:true,
      timers_restored:true,
      router_modified:false,
      prompt_influence:false
    },
    hard_stop:"before_component_binding_apply_or_runtime_integration"
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

schema_installed=0
phase=complete
printf 'memory_v1_v5_project_shadow_read_api_production_install: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
