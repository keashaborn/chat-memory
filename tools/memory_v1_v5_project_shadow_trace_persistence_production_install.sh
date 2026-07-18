#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs one sanitized, append-only project-shadow trace
# store. It accepts hashes, counts, routing outcomes, and rejection codes only.

if [[ "${MEMORY_V1_V5_PROJECT_TRACE_INSTALL:-}" != "authorized" ]]; then
  echo 'MEMORY_V1_V5_PROJECT_TRACE_INSTALL=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
migration=ops/sql/20260718_memory_v1_v5_project_shadow_trace_persistence.sql
rollback=ops/sql/20260718_memory_v1_v5_project_shadow_trace_persistence_rollback.sql
test_sql=tests/memory_v1_v5_project_shadow_trace_persistence.sql
required_ancestor=7cf23216de4a25781d773d63d8e6a0830f7d8e92
expected_migration_sha=6744d65b892ed35edccb76fd33adeea2dac03dd672142819256ddcedad8b1a6f
expected_rollback_sha=42a01c5fa4041fb1fc47839f640be9f8abb849519e80a6e3e7f1ccb6546f18c0
expected_test_sha=6aa8b12c77ccd393958e0147b04889c6a38525120025b96b07e76c0491f0ed64
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_project_trace_install.lock
phase=initialization
schema_installed=0
quiesced=0
run_id=
status_file=
table_list=$(mktemp /tmp/memory-v1-v5-project-trace-tables.XXXXXX)
timer_state=$(mktemp /tmp/memory-v1-v5-project-trace-timers.XXXXXX)
baseline=
post=
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
      >"$snapshot_dir/memory_v1_v5_project_trace_rollback_${run_id}.log" 2>&1 \
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

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(sha256sum "$repo_root/$migration" | awk '{print $1}')" == "$expected_migration_sha" ]]
[[ "$(sha256sum "$repo_root/$rollback" | awk '{print $1}')" == "$expected_rollback_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | awk '{print $1}')" == "$expected_test_sha" ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_project_trace_install_${run_id}.status"
baseline="$snapshot_dir/memory_v1_v5_project_trace_baseline_${run_id}.tsv"
post="$snapshot_dir/memory_v1_v5_project_trace_post_${run_id}.tsv"

phase=preflight
[[ "$(psql_scalar "SELECT (
  to_regprocedure('memory.record_v5_project_shadow_trace_v1(text,text,text,text,text,text,text,text,text,text,text,text,integer,integer,integer,jsonb,integer,integer)') IS NULL
  AND to_regclass('memory.v5_project_shadow_trace_event') IS NULL
  AND to_regrole('memory_v5_trace_writer') IS NOT NULL
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
backup_partial="$snapshot_dir/.memory_pre_v5_project_trace_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_project_trace_${run_id}.dump"
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
install_log="$snapshot_dir/memory_v1_v5_project_trace_install_${run_id}.log"
run_sql_file "$migration" >"$install_log" 2>&1
schema_installed=1

phase=rollback_only_security_test
run_sql_file "$test_sql" >>"$install_log" 2>&1
chmod 0600 "$install_log"

phase=postflight
capture_state "$post"
cmp -s "$baseline" "$post" || {
  diff -u "$baseline" "$post" >&2 || true
  echo 'Memory V1 row state changed during project trace install' >&2
  exit 1
}
[[ "$(psql_scalar "SELECT count(*) FROM memory.v5_project_shadow_trace_event")" == 0 ]]
[[ "$(psql_scalar "SELECT pg_get_userbyid((SELECT proowner FROM pg_proc WHERE oid='memory.record_v5_project_shadow_trace_v1(text,text,text,text,text,text,text,text,text,text,text,text,integer,integer,integer,jsonb,integer,integer)'::regprocedure))")" == memory_v5_trace_writer ]]
[[ "$(psql_scalar "SELECT has_function_privilege('brains_app','memory.record_v5_project_shadow_trace_v1(text,text,text,text,text,text,text,text,text,text,text,text,integer,integer,integer,jsonb,integer,integer)','EXECUTE')::int")" == 1 ]]
[[ "$(psql_scalar "SELECT count(*) FROM information_schema.role_table_grants WHERE grantee='brains_app' AND table_schema='memory' AND table_name='v5_project_shadow_trace_event'")" == 0 ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

restore_timers

phase=report
report="$snapshot_dir/memory_v1_v5_project_trace_install_${run_id}.json"
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
    contract_version:"memory_v1_v5_project_trace_install_report_v1",
    completed_at:$completed_at,
    head_commit:$head_commit,
    backup:{path:$backup_path,sha256:$backup_sha256},
    evidence:{baseline:$baseline,post:$post,log:$log,qdrant_sha256:$qdrant_sha256},
    checks:{
      restricted_trace_writer:true,
      append_only:true,
      raw_query_absent:true,
      claim_prose_absent:true,
      prompt_content_absent:true,
      hashes_counts_outcomes_only:true,
      replay_zero_write:true,
      actor_derived_owner:true,
      rollback_only_test:true,
      memory_rows_unchanged:true,
      qdrant_unchanged:true,
      timers_restored:true,
      router_modified:false,
      prompt_influence:false
    },
    hard_stop:"before_project_shadow_runtime_deployment"
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

schema_installed=0
phase=complete
printf 'memory_v1_v5_project_shadow_trace_persistence_production_install: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
