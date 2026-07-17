#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the empty V5.1 observation-entailment ledger and
# fail-closed projection guard. Test decisions are transactionally rolled back.

if [[ "${MEMORY_V1_OBSERVATION_ENTAILMENT_INSTALL:-}" != "authorized" ]]; then
  echo "MEMORY_V1_OBSERVATION_ENTAILMENT_INSTALL=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
migration=ops/sql/20260716_memory_v1_observation_entailment_v5_1.sql
rollback=ops/sql/20260716_memory_v1_observation_entailment_v5_1_rollback.sql
test_sql=tests/memory_v1_observation_entailment_v5_1.sql
required_ancestor=cde03538b56feda0c4c92b98bcd9b3e647a38e7d
expected_migration_sha=bbd22a712e1c52c401a2b8a906176042fc0996eb0d613b743e764cdeec56388d
expected_rollback_sha=d249af605f0f1a728399f00510b7e45dd3f2b42fdaeb71e5b09060b819488bd8
expected_test_sha=98de4009d3a6e01a0a1f0a7be0fbeca974118267014cf8536db16b2f5a5aa259
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_observation_entailment_v5_1_install.lock
phase=initialization
status_file=
schema_installed=0

[[ -z "$(git -C "$repo_root" status --porcelain)" ]] || {
  echo "production install requires a clean Git worktree" >&2
  exit 1
}
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(sha256sum "$repo_root/$migration" | awk '{print $1}')" == \
  "$expected_migration_sha" ]]
[[ "$(sha256sum "$repo_root/$rollback" | awk '{print $1}')" == \
  "$expected_rollback_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | awk '{print $1}')" == \
  "$expected_test_sha" ]]

exec 9>"$lock_file"
flock -n 9 || {
  echo "another V5.1 observation-entailment install holds the lock" >&2
  exit 1
}
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_observation_entailment_v5_1_install_${run_id}.status"

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
}

run_sql_file() {
  docker exec -e PGOPTIONS='-c lock_timeout=5s -c statement_timeout=120s' \
    -i "$container" psql -X -v ON_ERROR_STOP=1 -U sage -d "$database" \
    <"$repo_root/$1"
}

record_exit() {
  exit_code=$?
  if [[ "$exit_code" -ne 0 && "$schema_installed" -eq 1 ]]; then
    phase=automatic_rollback_after_failure
    run_sql_file "$rollback" \
      >"$snapshot_dir/memory_v1_observation_entailment_v5_1_rollback_${run_id}.log" \
      2>&1 || true
  fi
  {
    printf 'run_id=%s\n' "$run_id"
    printf 'phase=%s\n' "$phase"
    printf 'exit_code=%s\n' "$exit_code"
    printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"$status_file"
  chmod 0600 "$status_file"
}
trap record_exit EXIT

qdrant_signature() {
  curl --fail --silent --show-error \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id)' \
    | sha256sum | awk '{print $1}'
}

capture_state() {
  local table_list=$1
  local output=$2
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]] || exit 1
    state=$(psql_scalar "
      SELECT count(*)::text || E'\\t' ||
             encode(digest(coalesce(string_agg(row_json,E'\\n'
               ORDER BY row_json),''),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(table_row)::text row_json
        FROM memory.\"$table\" AS table_row
      ) rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

phase=preflight
[[ "$(psql_scalar "SELECT current_setting('server_version_num')::integer/10000")" \
  == "16" ]]
[[ "$(psql_scalar "SELECT (
  to_regclass('memory.observation_entailment_v5') IS NULL
  AND to_regtype('memory.observation_entailment_decision_v5') IS NULL
  AND NOT EXISTS (
    SELECT 1 FROM pg_proc AS procedure
    JOIN pg_namespace AS namespace ON namespace.oid=procedure.pronamespace
    WHERE namespace.nspname='memory'
      AND procedure.proname IN (
        'preflight_observation_entailment_v5',
        'record_observation_entailment_v5',
        'observation_entailment_allows_projection_v5',
        'guard_projection_observation_entailment_v5',
        'v5_source_spans_match_text',
        'v5_source_spans_cover'
      )
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid='memory.projection_plan_observation'::regclass
      AND tgname='projection_plan_observation_entailment_guard'
      AND NOT tgisinternal
  )
)::int")" == "1" ]]
[[ "$(psql_scalar "
  SELECT pg_get_constraintdef(oid)
  FROM pg_constraint
  WHERE conrelid='memory.relational_operation_request'::regclass
    AND conname='relational_operation_request_operation_check'
")" == \
"CHECK ((operation = ANY (ARRAY['stage_packet'::text, 'review_resolution'::text, 'apply_resolution'::text, 'review_claim_assessment_v5'::text, 'apply_claim_assessment_v5'::text])))" ]]
[[ "$(psql_scalar "SELECT (
  EXISTS (
    SELECT 1 FROM pg_roles
    WHERE rolname='memory_v5_writer'
      AND NOT rolcanlogin AND NOT rolinherit AND NOT rolbypassrls
  )
  AND to_regprocedure('memory.require_v5_writer_context()') IS NOT NULL
  AND to_regprocedure('memory.v5_source_spans_valid(jsonb)') IS NOT NULL
)::int")" == "1" ]]
database_size=$(psql_scalar "SELECT pg_database_size(current_database())")
available_kb=$(df -Pk "$snapshot_dir" | awk 'NR==2 {print $4}')
(( available_kb * 1024 >= database_size * 2 ))

phase=backup
backup_partial="$snapshot_dir/.memory_pre_observation_entailment_v5_1_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_observation_entailment_v5_1_${run_id}.dump"
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

phase=baseline_capture
table_list="$snapshot_dir/memory_v1_observation_entailment_v5_1_tables_${run_id}.txt"
baseline="$snapshot_dir/memory_v1_observation_entailment_v5_1_baseline_${run_id}.tsv"
post="$snapshot_dir/memory_v1_observation_entailment_v5_1_post_${run_id}.tsv"
psql_scalar "
  SELECT table_name FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
  ORDER BY table_name
" >"$table_list"
chmod 0600 "$table_list"
capture_state "$table_list" "$baseline"
qdrant_before=$(qdrant_signature)

phase=schema_install
install_log="$snapshot_dir/memory_v1_observation_entailment_v5_1_install_${run_id}.log"
run_sql_file "$migration" >"$install_log" 2>&1
schema_installed=1

phase=rollback_only_security_test
run_sql_file "$test_sql" >>"$install_log" 2>&1
chmod 0600 "$install_log"

phase=postflight
capture_state "$table_list" "$post"
cmp -s "$baseline" "$post" || {
  diff -u "$baseline" "$post" >&2 || true
  echo "preexisting Memory V1 rows changed" >&2
  exit 1
}
[[ "$(psql_scalar "SELECT (
  (SELECT count(*) FROM memory.observation_entailment_v5)=0
  AND (
    SELECT count(*) FROM memory.relational_operation_request
    WHERE operation='record_observation_entailment_v5'
  )=0
  AND pg_get_userbyid((
    SELECT relowner FROM pg_class
    WHERE oid='memory.observation_entailment_v5'::regclass
  ))='memory_v5_writer'
  AND (
    SELECT relrowsecurity AND relforcerowsecurity
    FROM pg_class
    WHERE oid='memory.observation_entailment_v5'::regclass
  )
  AND NOT has_table_privilege(
    'brains_app','memory.observation_entailment_v5','SELECT'
  )
  AND NOT has_table_privilege(
    'brains_app','memory.observation_entailment_v5','INSERT'
  )
  AND has_function_privilege(
    'brains_app',
    'memory.record_observation_entailment_v5(
      uuid,uuid,memory.observation_entailment_decision_v5,
      text,jsonb,text,text,text
    )',
    'EXECUTE'
  )
  AND EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid='memory.projection_plan_observation'::regclass
      AND tgname='projection_plan_observation_entailment_guard'
      AND tgenabled='O' AND NOT tgisinternal
  )
)::int")" == "1" ]]
[[ "$(psql_scalar "
  SELECT pg_get_constraintdef(oid)
  FROM pg_constraint
  WHERE conrelid='memory.relational_operation_request'::regclass
    AND conname='relational_operation_request_operation_check'
")" == \
"CHECK ((operation = ANY (ARRAY['stage_packet'::text, 'review_resolution'::text, 'apply_resolution'::text, 'review_claim_assessment_v5'::text, 'apply_claim_assessment_v5'::text, 'record_observation_entailment_v5'::text])))" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]] || {
  echo "Qdrant changed during V5.1 observation-entailment installation" >&2
  exit 1
}

phase=report
report="$snapshot_dir/memory_v1_observation_entailment_v5_1_install_${run_id}.json"
BACKUP="$backup" CATALOG="$catalog" TABLE_LIST="$table_list" \
BASELINE="$baseline" POST="$post" LOG="$install_log" REPORT="$report" \
QDRANT_BEFORE="$qdrant_before" QDRANT_AFTER="$qdrant_after" \
HEAD="$(git -C "$repo_root" rev-parse HEAD)" \
python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

value = {
    "contract_version": "memory_v1_observation_entailment_v5_1_install_report_v1",
    "instruction_source": "user_continue_next_step_20260716",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "backup": {
        "path": os.environ["BACKUP"],
        "catalog": os.environ["CATALOG"],
    },
    "evidence": {
        "table_list": os.environ["TABLE_LIST"],
        "baseline": os.environ["BASELINE"],
        "post": os.environ["POST"],
        "log": os.environ["LOG"],
        "qdrant_before_sha256": os.environ["QDRANT_BEFORE"],
        "qdrant_after_sha256": os.environ["QDRANT_AFTER"],
    },
    "checks": {
        "append_only_owner_scoped_ledger": True,
        "forced_rls": True,
        "restricted_writer_owner": True,
        "direct_brains_table_access_rejected": True,
        "missing_actor_rejected": True,
        "cross_owner_rejected": True,
        "tampered_or_insufficient_spans_rejected": True,
        "deferred_projection_blocked": True,
        "zero_write_replay": True,
        "verification_transaction_rolled_back": True,
        "ledger_rows_zero": True,
        "preexisting_memory_rows_unchanged": True,
        "qdrant_unchanged": True,
        "retrieval_activated": False,
        "prompt_influence": False,
        "frontend_modified": False,
    },
    "hard_stop": "before_live_entailment_decision_recording_or_projection",
}
Path(os.environ["REPORT"]).write_text(
    json.dumps(value, indent=2, sort_keys=True) + "\n"
)
PY
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

schema_installed=0
phase=complete
printf 'memory_v1_observation_entailment_v5_1_production_install: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
