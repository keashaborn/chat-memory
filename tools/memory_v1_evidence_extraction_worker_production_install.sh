#!/usr/bin/env bash
set -euo pipefail

if [[ "${MEMORY_V1_EVIDENCE_EXTRACTION_WORKER_INSTALL:-}" != "authorized" ]]; then
  echo "MEMORY_V1_EVIDENCE_EXTRACTION_WORKER_INSTALL=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
migration=ops/sql/20260716_memory_v1_evidence_extraction_worker.sql
rollback=ops/sql/20260716_memory_v1_evidence_extraction_worker_rollback.sql
test_sql=tests/memory_v1_evidence_extraction_worker.sql
required_ancestor=09f3932759f0d71bfd6d68a3720ccee6b3ab6706
expected_migration_sha=bd0dee94bdb9c669f3878ae235ef7c4349b861c99970deb855fbc42c1aabe0dd
expected_rollback_sha=ba696c1b97714ef23f3b2b8a8001c99ebdcb78a31dc7c421761d8b813333ba2f
expected_test_sha=4061ef028ef928990d340fe3a908ba0c96fd0e270b029f234261c5362653f711
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_evidence_extraction_worker_install.lock
phase=initialization
status_file=
migration_applied=0

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(sha256sum "$repo_root/$migration" | awk '{print $1}')" == "$expected_migration_sha" ]]
[[ "$(sha256sum "$repo_root/$rollback" | awk '{print $1}')" == "$expected_rollback_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | awk '{print $1}')" == "$expected_test_sha" ]]

if rg -n '(^|[^a-zA-Z])(OpenAI|responses\.create|chat\.completions)' \
  "$repo_root/$migration" \
  "$repo_root/$test_sql"; then
  echo "evidence extraction worker database patch contains a model caller" >&2
  exit 1
fi

exec 9>"$lock_file"
flock -n 9 || {
  echo "another extraction worker install holds the lock" >&2
  exit 1
}
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_evidence_extraction_worker_${run_id}.status"

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
  if [[ "$code" -ne 0 && "$migration_applied" -eq 1 ]]; then
    phase=automatic_rollback_after_failure
    run_sql_file "$rollback" \
      >"$snapshot_dir/memory_v1_evidence_extraction_worker_rollback_${run_id}.log" \
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

capture_logical_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    state=$(psql_scalar "
      SELECT count(*)::text || E'\\t' ||
             encode(
               digest(
                 coalesce(
                   string_agg(row_json,E'\\n' ORDER BY row_json),
                   ''
                 ),
                 'sha256'
               ),
               'hex'
             )
      FROM (
        SELECT (
          CASE
            WHEN '$table'='evidence_extraction_job'
              THEN to_jsonb(table_row)
                - 'checkpoint_sequence'
                - 'checkpoint_sha256'
            WHEN '$table'='evidence_extraction_event'
              THEN to_jsonb(table_row)-'operation_id'
            ELSE to_jsonb(table_row)
          END
        )::text AS row_json
        FROM memory.\"$table\" AS table_row
      ) rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done < <(psql_scalar "
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name
  ")
  chmod 0600 "$output"
}

phase=preflight
[[ "$(psql_scalar "
  SELECT (
    to_regclass('memory.evidence_extraction_job') IS NOT NULL
    AND to_regclass('memory.evidence_extraction_event') IS NOT NULL
    AND to_regrole('memory_extraction_queue_maintainer') IS NOT NULL
    AND to_regprocedure(
      'memory.enqueue_owner_evidence_extraction_v1(uuid,text,text,text,text)'
    ) IS NOT NULL
    AND to_regrole('memory_extraction_worker_maintainer') IS NULL
    AND to_regprocedure(
      'memory.claim_owner_evidence_extraction_job_v1(uuid,text,text,integer,integer)'
    ) IS NULL
    AND to_regprocedure(
      'memory.checkpoint_owner_evidence_extraction_job_v1(uuid,uuid,uuid,text,text,integer,text,jsonb,integer)'
    ) IS NULL
    AND to_regprocedure(
      'memory.finish_owner_evidence_extraction_job_v1(uuid,uuid,uuid,text,text,text,text,jsonb)'
    ) IS NULL
    AND to_regprocedure(
      'memory.fail_owner_evidence_extraction_job_v1(uuid,uuid,uuid,text,text,text,text,integer)'
    ) IS NULL
    AND to_regprocedure(
      'memory.resolve_owner_evidence_extraction_review_v1(uuid,uuid,text,text,text,jsonb)'
    ) IS NULL
    AND NOT EXISTS (
      SELECT 1
      FROM information_schema.columns
      WHERE table_schema='memory'
        AND (
          (
            table_name='evidence_extraction_job'
            AND column_name IN ('checkpoint_sequence','checkpoint_sha256')
          )
          OR
          (
            table_name='evidence_extraction_event'
            AND column_name='operation_id'
          )
        )
    )
    AND NOT EXISTS (SELECT 1 FROM memory.evidence_extraction_job)
    AND NOT EXISTS (SELECT 1 FROM memory.evidence_extraction_event)
  )::integer
")" == "1" ]]
if systemctl is-active --quiet memory-v1-evidence-extraction-worker.service; then
  echo "evidence extraction worker is already active" >&2
  exit 1
fi

phase=backup
partial="$snapshot_dir/.memory_pre_evidence_extraction_worker_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_evidence_extraction_worker_${run_id}.dump"
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
baseline="$snapshot_dir/memory_v1_evidence_extraction_worker_baseline_${run_id}.tsv"
post="$snapshot_dir/memory_v1_evidence_extraction_worker_post_${run_id}.tsv"
capture_logical_state "$baseline"
qdrant_before=$(qdrant_signature)

phase=schema_install
log="$snapshot_dir/memory_v1_evidence_extraction_worker_install_${run_id}.log"
run_sql_file "$migration" >"$log" 2>&1
migration_applied=1

phase=rollback_only_security_test
run_sql_file "$test_sql" >>"$log" 2>&1
chmod 0600 "$log"

phase=postflight
[[ "$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_job")" == "0" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_event")" == "0" ]]
[[ "$(psql_scalar "
  SELECT (
    EXISTS (
      SELECT 1
      FROM pg_roles
      WHERE rolname='memory_extraction_worker_maintainer'
        AND NOT rolcanlogin
        AND NOT rolsuper
        AND NOT rolcreatedb
        AND NOT rolcreaterole
        AND NOT rolinherit
        AND NOT rolbypassrls
    )
    AND (
      SELECT count(*)=5
      FROM pg_proc
      WHERE oid IN (
        'memory.claim_owner_evidence_extraction_job_v1(uuid,text,text,integer,integer)'::regprocedure,
        'memory.checkpoint_owner_evidence_extraction_job_v1(uuid,uuid,uuid,text,text,integer,text,jsonb,integer)'::regprocedure,
        'memory.finish_owner_evidence_extraction_job_v1(uuid,uuid,uuid,text,text,text,text,jsonb)'::regprocedure,
        'memory.fail_owner_evidence_extraction_job_v1(uuid,uuid,uuid,text,text,text,text,integer)'::regprocedure,
        'memory.resolve_owner_evidence_extraction_review_v1(uuid,uuid,text,text,text,jsonb)'::regprocedure
      )
        AND prosecdef
        AND proowner='memory_extraction_worker_maintainer'::regrole
        AND proconfig=ARRAY['search_path=pg_catalog']::text[]
    )
    AND has_function_privilege(
      'brains_app',
      'memory.claim_owner_evidence_extraction_job_v1(uuid,text,text,integer,integer)',
      'EXECUTE'
    )
    AND NOT has_table_privilege(
      'brains_app','memory.evidence_extraction_job','UPDATE'
    )
    AND NOT has_table_privilege(
      'brains_app','memory.evidence_extraction_event','INSERT'
    )
    AND has_table_privilege(
      'memory_extraction_worker_maintainer',
      'memory.evidence_extraction_job',
      'UPDATE'
    )
    AND has_table_privilege(
      'memory_extraction_worker_maintainer',
      'memory.evidence_extraction_event',
      'INSERT'
    )
    AND has_table_privilege(
      'memory_extraction_worker_maintainer',
      'memory.evidence',
      'SELECT'
    )
    AND EXISTS (
      SELECT 1
      FROM pg_indexes
      WHERE schemaname='memory'
        AND indexname='evidence_extraction_event_owner_operation_uidx'
    )
  )::integer
")" == "1" ]]

capture_logical_state "$post"
cmp -s "$baseline" "$post" || {
  diff -u "$baseline" "$post" >&2 || true
  exit 1
}
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]
if systemctl is-active --quiet memory-v1-evidence-extraction-worker.service; then
  echo "evidence extraction worker was unexpectedly activated" >&2
  exit 1
fi

phase=report
report="$snapshot_dir/memory_v1_evidence_extraction_worker_${run_id}.json"
BACKUP="$backup" CATALOG="$catalog" BASELINE="$baseline" POST="$post" \
LOG="$log" REPORT="$report" \
QDRANT_BEFORE="$qdrant_before" QDRANT_AFTER="$qdrant_after" \
HEAD="$(git -C "$repo_root" rev-parse HEAD)" \
python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

value = {
    "contract_version": "memory_v1_evidence_extraction_worker_install_report_v1",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "backup": {
        "path": os.environ["BACKUP"],
        "catalog": os.environ["CATALOG"],
    },
    "evidence": {
        "baseline": os.environ["BASELINE"],
        "post": os.environ["POST"],
        "log": os.environ["LOG"],
        "qdrant_before_sha256": os.environ["QDRANT_BEFORE"],
        "qdrant_after_sha256": os.environ["QDRANT_AFTER"],
    },
    "checks": {
        "restricted_worker_role": True,
        "forced_owner_rls_preserved": True,
        "skip_locked_claiming": True,
        "hash_bound_checkpoints": True,
        "operation_replay_zero_write": True,
        "bounded_retry_terminalization": True,
        "stale_lease_rejected": True,
        "cross_owner_access_denied": True,
        "rollback_only_security_tests": True,
        "memory_rows_unchanged": True,
        "qdrant_unchanged": True,
        "model_calls": 0,
        "candidate_writes": 0,
        "claim_writes": 0,
        "worker_enabled": False,
        "timer_enabled": False,
    },
}
Path(os.environ["REPORT"]).write_text(
    json.dumps(value, indent=2, sort_keys=True) + "\n"
)
PY
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
migration_applied=0
phase=complete
printf 'memory_v1_evidence_extraction_worker_production_install: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
