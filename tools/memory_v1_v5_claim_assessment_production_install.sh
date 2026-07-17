#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the owner-scoped V5 claim-assessment API.
# The verification transaction rolls back. No live claim, evidence, Qdrant,
# retrieval, router, or prompt state is changed.

if [[ "${MEMORY_V1_V5_CLAIM_ASSESSMENT_INSTALL:-}" != "authorized" ]]; then
  echo "MEMORY_V1_V5_CLAIM_ASSESSMENT_INSTALL=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
migration=ops/sql/20260716_memory_v1_v5_claim_assessment.sql
rollback=ops/sql/20260716_memory_v1_v5_claim_assessment_rollback.sql
test_sql=tests/memory_v1_v5_claim_assessment.sql
required_ancestor=9cce3747681c3be284bc2e86878dca9f028da813
expected_migration_sha=79bdf07f54b9e324a4b235b31e647117aabf31d50d49f4b2f077fd3f077389bc
expected_rollback_sha=488dfa5aa93ea675e97e85d97d87ceba6d4a07c01bf29a8f271681f4c2dc08e0
expected_test_sha=886b83960be060dbe8151ab52a24b178b88a75a4adfbf33f33282c01c11c50c8
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_claim_assessment_install.lock
phase=initialization
status_file=
schema_installed=0

[[ -z "$(git -C "$repo_root" status --porcelain)" ]] || {
  echo "production install requires a clean Git worktree" >&2
  exit 1
}
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(sha256sum "$repo_root/$migration" | awk '{print $1}')" == "$expected_migration_sha" ]]
[[ "$(sha256sum "$repo_root/$rollback" | awk '{print $1}')" == "$expected_rollback_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | awk '{print $1}')" == "$expected_test_sha" ]]

exec 9>"$lock_file"
flock -n 9 || {
  echo "another V5 claim-assessment install holds the lock" >&2
  exit 1
}
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_claim_assessment_install_${run_id}.status"

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
      >"$snapshot_dir/memory_v1_v5_claim_assessment_rollback_${run_id}.log" 2>&1 \
      || true
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
        SELECT to_jsonb(t)::text row_json FROM memory.\"$table\" t
      ) rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

phase=preflight
[[ "$(psql_scalar "SELECT (
  to_regtype('memory.claim_assessment_action_v5') IS NULL
  AND to_regclass('memory.claim_assessment_review_v5') IS NULL
  AND to_regclass('memory.claim_assessment_apply_v5') IS NULL
  AND to_regprocedure(
    'memory.preflight_claim_assessment_review_v5(uuid,memory.claim_assessment_action_v5,numeric,numeric,numeric,numeric,jsonb,text,text,text)'
  ) IS NULL
  AND to_regprocedure(
    'memory.review_claim_assessment_v5(uuid,uuid,memory.claim_assessment_action_v5,numeric,numeric,numeric,numeric,jsonb,text,text,text,text)'
  ) IS NULL
  AND to_regprocedure(
    'memory.preflight_claim_assessment_apply_v5(uuid,uuid)'
  ) IS NULL
  AND to_regprocedure(
    'memory.apply_claim_assessment_v5(uuid,uuid,uuid,text)'
  ) IS NULL
)::int")" == "1" ]]

[[ "$(psql_scalar "
  SELECT pg_get_constraintdef(oid)
  FROM pg_constraint
  WHERE conrelid='memory.relational_operation_request'::regclass
    AND conname='relational_operation_request_operation_check'
")" == "CHECK ((operation = ANY (ARRAY['stage_packet'::text, 'review_resolution'::text, 'apply_resolution'::text])))" ]]

phase=backup
backup_partial="$snapshot_dir/.memory_pre_v5_claim_assessment_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_claim_assessment_${run_id}.dump"
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
table_list="$snapshot_dir/memory_v1_v5_claim_assessment_tables_${run_id}.txt"
baseline="$snapshot_dir/memory_v1_v5_claim_assessment_baseline_${run_id}.tsv"
post="$snapshot_dir/memory_v1_v5_claim_assessment_post_${run_id}.tsv"
psql_scalar "
  SELECT table_name FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
  ORDER BY table_name
" >"$table_list"
chmod 0600 "$table_list"
capture_state "$table_list" "$baseline"
qdrant_before=$(qdrant_signature)

phase=schema_install
install_log="$snapshot_dir/memory_v1_v5_claim_assessment_install_${run_id}.log"
run_sql_file "$migration" >"$install_log" 2>&1
schema_installed=1

phase=rollback_only_security_test
run_sql_file "$test_sql" >>"$install_log" 2>&1
chmod 0600 "$install_log"

phase=postflight
capture_state "$table_list" "$post"
cmp -s "$baseline" "$post" || {
  diff -u "$baseline" "$post" >&2 || true
  echo "Memory V1 row state changed during V5 claim-assessment installation" >&2
  exit 1
}
[[ "$(psql_scalar "
  SELECT (
    (SELECT count(*) FROM memory.claim_assessment_review_v5)=0
    AND (SELECT count(*) FROM memory.claim_assessment_apply_v5)=0
  )::int
")" == "1" ]]
[[ "$(psql_scalar "
  SELECT (
    pg_get_userbyid((SELECT relowner FROM pg_class
      WHERE oid='memory.claim_assessment_review_v5'::regclass))='memory_v5_writer'
    AND pg_get_userbyid((SELECT relowner FROM pg_class
      WHERE oid='memory.claim_assessment_apply_v5'::regclass))='memory_v5_writer'
    AND NOT has_table_privilege(
      'brains_app','memory.claim_assessment_review_v5','SELECT'
    )
    AND NOT has_table_privilege(
      'brains_app','memory.claim_assessment_apply_v5','SELECT'
    )
    AND has_function_privilege(
      'brains_app',
      'memory.review_claim_assessment_v5(uuid,uuid,memory.claim_assessment_action_v5,numeric,numeric,numeric,numeric,jsonb,text,text,text,text)',
      'EXECUTE'
    )
    AND has_function_privilege(
      'brains_app',
      'memory.apply_claim_assessment_v5(uuid,uuid,uuid,text)',
      'EXECUTE'
    )
  )::int
")" == "1" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]] || {
  echo "Qdrant changed during V5 claim-assessment installation" >&2
  exit 1
}

phase=report
report="$snapshot_dir/memory_v1_v5_claim_assessment_install_${run_id}.json"
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
    "contract_version": "memory_v1_v5_claim_assessment_install_report_v1",
    "instruction_source": "user_continue_memory_v1_20260716",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "backup": {"path": os.environ["BACKUP"], "catalog": os.environ["CATALOG"]},
    "evidence": {
        "table_list": os.environ["TABLE_LIST"],
        "baseline": os.environ["BASELINE"],
        "post": os.environ["POST"],
        "log": os.environ["LOG"],
        "qdrant_before_sha256": os.environ["QDRANT_BEFORE"],
        "qdrant_after_sha256": os.environ["QDRANT_AFTER"],
    },
    "checks": {
        "restricted_writer_owner": True,
        "owner_scoped_security_definer_functions": True,
        "missing_actor_rejected": True,
        "direct_table_access_rejected": True,
        "cross_owner_rejected": True,
        "hash_locked_review_and_apply": True,
        "zero_write_replay": True,
        "verification_transaction_rolled_back": True,
        "memory_rows_and_content_unchanged": True,
        "qdrant_unchanged": True,
        "router_modified": False,
        "prompt_influence": False,
    },
    "hard_stop": "before_live_claim_assessment_review_or_apply",
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
printf 'memory_v1_v5_claim_assessment_production_install: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
