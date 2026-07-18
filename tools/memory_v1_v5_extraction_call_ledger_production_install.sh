#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the append-only V5 external-call reservation
# ledger. It does not install or enable the extraction service or timer.

if [[ "${MEMORY_V1_V5_CALL_LEDGER_INSTALL:-}" != "authorized" ]]; then
  echo 'MEMORY_V1_V5_CALL_LEDGER_INSTALL=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
migration=ops/sql/20260718_memory_v1_v5_extraction_call_ledger.sql
rollback=ops/sql/20260718_memory_v1_v5_extraction_call_ledger_rollback.sql
test_sql=tests/memory_v1_v5_extraction_call_ledger.sql
worker=scripts/memory_v1_v5_bounded_extraction_worker.py
worker_test=scripts/memory_v1_v5_bounded_extraction_worker_test.py
required_ancestor=5e2ad536055701c74c235bd7b4729af220846156
expected_migration_sha=77fe3297a85ff9080d3a9b2beb369a251fc10f048104de666bc8fc749c69c27f
expected_rollback_sha=de8268679e34ac3c027c4dde2cc3a7ab0e117efb36e39d09905f78981a00ce41
expected_test_sha=d35a7044155deba994b9650b2c3a7323f44af5ef1cf891e61daaeaf12e74df48
expected_worker_sha=e73ffaf224d82a9fe2eedc8eed5c3069d3f996c28ca540dcc855b428db4360a5
expected_worker_test_sha=9d8a77788159c92f554331da86b71846c7b1193f54c2ea2a7d3b288702576dd3
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_call_ledger_install.lock
phase=initialization
schema_installed=0
quiesced=0
run_id=
status_file=
table_list=$(mktemp /tmp/memory-v1-v5-call-ledger-tables.XXXXXX)
timer_state=$(mktemp /tmp/memory-v1-v5-call-ledger-timers.XXXXXX)
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

run_test_as_brains() {
  docker exec -e PGOPTIONS='-c lock_timeout=5s -c statement_timeout=120s' \
    -i "$container" psql -X -v ON_ERROR_STOP=1 \
    -U brains_app -d "$database" <"$repo_root/$1"
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
      >"$snapshot_dir/memory_v1_v5_call_ledger_rollback_${run_id}.log" 2>&1 \
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
[[ "$(sha256sum "$repo_root/$worker" | awk '{print $1}')" == "$expected_worker_sha" ]]
[[ "$(sha256sum "$repo_root/$worker_test" | awk '{print $1}')" == "$expected_worker_test_sha" ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_call_ledger_install_${run_id}.status"
baseline="$snapshot_dir/memory_v1_v5_call_ledger_baseline_${run_id}.tsv"
post="$snapshot_dir/memory_v1_v5_call_ledger_post_${run_id}.tsv"

phase=preflight
[[ "$(psql_scalar "SELECT (
  to_regclass('memory.v5_extraction_call_event') IS NULL
  AND to_regprocedure(
    'memory.claim_owner_v5_bounded_extraction_job_v1(uuid,uuid,text,text,integer,integer,text,text,text,integer,integer,integer)'
  ) IS NULL
  AND to_regprocedure(
    'memory.complete_owner_v5_extraction_call_v1(uuid,uuid,uuid,uuid,text,integer,text,text,text,text)'
  ) IS NULL
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
backup_partial="$snapshot_dir/.memory_pre_v5_call_ledger_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_call_ledger_${run_id}.dump"
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
install_log="$snapshot_dir/memory_v1_v5_call_ledger_install_${run_id}.log"
run_sql_file "$migration" >"$install_log" 2>&1
schema_installed=1

phase=rollback_only_security_test
run_test_as_brains "$test_sql" >>"$install_log" 2>&1
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python \
  "$repo_root/$worker_test" >>"$install_log" 2>&1
chmod 0600 "$install_log"

phase=postflight
capture_state "$post"
cmp -s "$baseline" "$post" || {
  diff -u "$baseline" "$post" >&2 || true
  echo 'Memory V1 row state changed during call-ledger install' >&2
  exit 1
}
[[ "$(psql_scalar "SELECT count(*) FROM memory.v5_extraction_call_event")" == 0 ]]
[[ "$(psql_scalar "SELECT pg_get_userbyid((SELECT proowner FROM pg_proc WHERE oid=
  'memory.claim_owner_v5_bounded_extraction_job_v1(uuid,uuid,text,text,integer,integer,text,text,text,integer,integer,integer)'::regprocedure))")" == memory_v5_extraction_scheduler_maintainer ]]
[[ "$(psql_scalar "SELECT has_function_privilege('brains_app',
  'memory.claim_owner_v5_bounded_extraction_job_v1(uuid,uuid,text,text,integer,integer,text,text,text,integer,integer,integer)','EXECUTE')::int")" == 1 ]]
[[ "$(psql_scalar "SELECT count(*) FROM information_schema.role_table_grants
  WHERE grantee='brains_app' AND table_schema='memory'
    AND table_name='v5_extraction_call_event'")" == 0 ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

restore_timers

phase=report
report="$snapshot_dir/memory_v1_v5_call_ledger_install_${run_id}.json"
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
    contract_version:"memory_v1_v5_call_ledger_install_report_v1",
    completed_at:$completed_at,
    head_commit:$head_commit,
    backup:{path:$backup_path,sha256:$backup_sha256},
    evidence:{baseline:$baseline,post:$post,log:$log,qdrant_sha256:$qdrant_sha256},
    checks:{
      restricted_scheduler_writer:true,
      append_only:true,
      forced_rls:true,
      owner_actor_derived:true,
      atomic_claim_and_reservation:true,
      quota_enforced:true,
      circuit_breaker_enforced:true,
      replay_zero_write:true,
      raw_evidence_absent:true,
      prompt_content_absent:true,
      rollback_only_test:true,
      memory_rows_unchanged:true,
      qdrant_unchanged:true,
      timers_restored:true,
      external_calls:0,
      prompt_influence:false
    },
    hard_stop:"before_extraction_service_or_timer_installation"
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

schema_installed=0
phase=complete
printf 'memory_v1_v5_extraction_call_ledger_production_install: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
