#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the empty deferred-entailment reconciliation
# audit/API and runs the full retraction test inside a rollback transaction.

if [[ "${MEMORY_V1_DEFERRED_RECONCILIATION_INSTALL:-}" != "authorized" ]]; then
  echo "MEMORY_V1_DEFERRED_RECONCILIATION_INSTALL=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
migration=ops/sql/20260716_memory_v1_deferred_entailment_reconciliation_v5.sql
rollback=ops/sql/20260716_memory_v1_deferred_entailment_reconciliation_v5_rollback.sql
test_sql=tests/memory_v1_deferred_entailment_reconciliation_v5.sql
required_ancestor=f4ed84c2afdcb9a0f86aab7d887fd90270712bbd
expected_migration_sha=76bf523d622a8f22beed117feb1cc73a51aa90e54383872db5413914f4bcd12f
expected_rollback_sha=e48d6b56bfe249bcf97ab0d322cf16a0ecdfb6e29df2ce4c6c62aa87ccdf23d4
expected_test_sha=913deb63d08acd7b4a08f5211de0daff938cac0619058fd86b7a90c6c0935b7a
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_deferred_reconciliation_install.lock
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
  echo "another deferred-entailment reconciliation install holds the lock" >&2
  exit 1
}
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_deferred_reconciliation_install_${run_id}.status"

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
  code=$?
  if [[ "$code" -ne 0 && "$schema_installed" -eq 1 ]]; then
    phase=automatic_rollback_after_failure
    run_sql_file "$rollback" \
      >"$snapshot_dir/memory_v1_deferred_reconciliation_rollback_${run_id}.log" \
      2>&1 || true
  fi
  printf 'run_id=%s\nphase=%s\nexit_code=%s\ncompleted_at=%s\n' \
    "$run_id" "$phase" "$code" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    >"$status_file"
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
[[ "$(psql_scalar "SELECT (
  to_regclass('memory.claim_entailment_reconciliation_v5') IS NULL
  AND to_regprocedure(
    'memory.preflight_deferred_entailment_reconciliation_v5(uuid,uuid)'
  ) IS NULL
  AND to_regprocedure(
    'memory.reconcile_deferred_entailment_claim_v5(uuid,uuid,uuid,uuid,uuid,text)'
  ) IS NULL
  AND NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid='memory.observation_entailment_v5'::regclass
      AND conname='observation_entailment_v5_owner_decision_key'
  )
)::int")" == "1" ]]
[[ "$(psql_scalar "SELECT (
  (SELECT count(*) FROM memory.observation_entailment_v5)=2
  AND (SELECT count(*) FROM memory.claim_assessment_review_v5)=0
  AND (SELECT count(*) FROM memory.claim_assessment_apply_v5)=0
  AND (SELECT count(*) FROM memory.claim
    WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
      AND claim_id='50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid
      AND status='candidate' AND confidence=0.500)=1
)::int")" == "1" ]]
database_size=$(psql_scalar "SELECT pg_database_size(current_database())")
available_kb=$(df -Pk "$snapshot_dir" | awk 'NR==2 {print $4}')
(( available_kb * 1024 >= database_size * 2 ))

phase=backup
partial="$snapshot_dir/.memory_pre_deferred_reconciliation_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_deferred_reconciliation_${run_id}.dump"
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

phase=baseline_capture
table_list="$snapshot_dir/memory_v1_deferred_reconciliation_tables_${run_id}.txt"
baseline="$snapshot_dir/memory_v1_deferred_reconciliation_baseline_${run_id}.tsv"
post="$snapshot_dir/memory_v1_deferred_reconciliation_post_${run_id}.tsv"
psql_scalar "
  SELECT table_name FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
  ORDER BY table_name
" >"$table_list"
chmod 0600 "$table_list"
capture_state "$table_list" "$baseline"
qdrant_before=$(qdrant_signature)

phase=schema_install
log="$snapshot_dir/memory_v1_deferred_reconciliation_install_${run_id}.log"
run_sql_file "$migration" >"$log" 2>&1
schema_installed=1

phase=rollback_only_security_test
run_sql_file "$test_sql" >>"$log" 2>&1
chmod 0600 "$log"

phase=postflight
capture_state "$table_list" "$post"
cmp -s "$baseline" "$post" || {
  diff -u "$baseline" "$post" >&2 || true
  echo "preexisting Memory V1 rows changed" >&2
  exit 1
}
[[ "$(psql_scalar "SELECT (
  (SELECT count(*) FROM memory.claim_entailment_reconciliation_v5)=0
  AND (SELECT count(*) FROM memory.claim_assessment_review_v5)=0
  AND (SELECT count(*) FROM memory.claim_assessment_apply_v5)=0
  AND pg_get_userbyid((
    SELECT relowner FROM pg_class
    WHERE oid='memory.claim_entailment_reconciliation_v5'::regclass
  ))='memory_v5_writer'
  AND (
    SELECT relrowsecurity AND relforcerowsecurity
    FROM pg_class
    WHERE oid='memory.claim_entailment_reconciliation_v5'::regclass
  )
  AND NOT has_table_privilege(
    'brains_app','memory.claim_entailment_reconciliation_v5','SELECT'
  )
  AND has_function_privilege(
    'brains_app',
    'memory.reconcile_deferred_entailment_claim_v5(uuid,uuid,uuid,uuid,uuid,text)',
    'EXECUTE'
  )
  AND (SELECT count(*) FROM memory.claim
    WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
      AND claim_id='50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid
      AND status='candidate' AND confidence=0.500)=1
)::int")" == "1" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=report
report="$snapshot_dir/memory_v1_deferred_reconciliation_install_${run_id}.json"
BACKUP="$backup" CATALOG="$catalog" BASELINE="$baseline" POST="$post" \
LOG="$log" REPORT="$report" QDRANT_BEFORE="$qdrant_before" \
QDRANT_AFTER="$qdrant_after" HEAD="$(git -C "$repo_root" rev-parse HEAD)" \
python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

value = {
    "contract_version": "memory_v1_deferred_reconciliation_install_report_v1",
    "instruction_source": "user_build_and_apply_generic_reconciliation_20260716",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "backup": {"path": os.environ["BACKUP"], "catalog": os.environ["CATALOG"]},
    "evidence": {
        "baseline": os.environ["BASELINE"],
        "post": os.environ["POST"],
        "log": os.environ["LOG"],
        "qdrant_before_sha256": os.environ["QDRANT_BEFORE"],
        "qdrant_after_sha256": os.environ["QDRANT_AFTER"],
    },
    "checks": {
        "schema_installed_empty": True,
        "forced_rls": True,
        "direct_brains_table_access_rejected": True,
        "cross_owner_rejected": True,
        "full_retraction_test_rolled_back": True,
        "zero_write_replay": True,
        "preexisting_memory_rows_unchanged": True,
        "qdrant_unchanged": True,
        "retrieval_activated": False,
        "prompt_influence": False,
    },
    "hard_stop": "before_live_deferred_entailment_reconciliation",
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
printf 'memory_v1_deferred_entailment_reconciliation_v5_production_install: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
