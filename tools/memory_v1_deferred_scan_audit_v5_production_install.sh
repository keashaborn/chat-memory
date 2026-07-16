#!/usr/bin/env bash
set -euo pipefail

if [[ "${MEMORY_V1_DEFERRED_SCAN_AUDIT_INSTALL:-}" != "authorized" ]]; then
  echo "MEMORY_V1_DEFERRED_SCAN_AUDIT_INSTALL=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
migration=ops/sql/20260716_memory_v1_deferred_scan_audit_v5.sql
rollback=ops/sql/20260716_memory_v1_deferred_scan_audit_v5_rollback.sql
test_sql=tests/memory_v1_deferred_scan_audit_v5.sql
required_ancestor=4fae1bc73109b1a1c53ed848da9de6342944d528
expected_migration_sha=80acdd50f800ba6da925254838fa33fe3613d84fd36bde41daf105ef89496980
expected_rollback_sha=9927c741b2171f8df4c77130a4d50d499bf5c9825632f47f51bddad29a699c1f
expected_test_sha=afbdaf690dfc5c1b87d3f46ce51e59c8897054930bde40314fd275029a5b479c
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_deferred_scan_audit_install.lock
phase=initialization
status_file=
audit_installed=0

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(sha256sum "$repo_root/$migration" | awk '{print $1}')" == "$expected_migration_sha" ]]
[[ "$(sha256sum "$repo_root/$rollback" | awk '{print $1}')" == "$expected_rollback_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | awk '{print $1}')" == "$expected_test_sha" ]]

exec 9>"$lock_file"
flock -n 9 || {
  echo "another deferred scan audit install holds the lock" >&2
  exit 1
}
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_deferred_scan_audit_${run_id}.status"

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
  if [[ "$code" -ne 0 && "$audit_installed" -eq 1 ]]; then
    phase=automatic_rollback_after_failure
    run_sql_file "$rollback" \
      >"$snapshot_dir/memory_v1_deferred_scan_audit_rollback_${run_id}.log" \
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

capture_existing_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
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
  done < <(psql_scalar "
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema='memory'
      AND table_type='BASE TABLE'
      AND table_name<>'deferred_reconciliation_scan_run_v5'
    ORDER BY table_name
  ")
  chmod 0600 "$output"
}

phase=preflight
[[ "$(psql_scalar "SELECT (
  to_regclass('memory.deferred_reconciliation_scan_run_v5') IS NULL
  AND to_regprocedure(
    'memory.run_deferred_reconciliation_scan_v5(uuid,integer,text,text)'
  ) IS NULL
  AND to_regprocedure(
    'memory.scan_deferred_entailment_reconciliation_v5(integer)'
  ) IS NOT NULL
)::int")" == "1" ]]

phase=backup
partial="$snapshot_dir/.memory_pre_deferred_scan_audit_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_deferred_scan_audit_${run_id}.dump"
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
baseline="$snapshot_dir/memory_v1_deferred_scan_audit_baseline_${run_id}.tsv"
post="$snapshot_dir/memory_v1_deferred_scan_audit_post_${run_id}.tsv"
capture_existing_state "$baseline"
qdrant_before=$(qdrant_signature)

phase=audit_schema_install
log="$snapshot_dir/memory_v1_deferred_scan_audit_install_${run_id}.log"
run_sql_file "$migration" >"$log" 2>&1
audit_installed=1

phase=rollback_only_security_test
run_sql_file "$test_sql" >>"$log" 2>&1
chmod 0600 "$log"

phase=postflight
capture_existing_state "$post"
cmp -s "$baseline" "$post"
[[ "$(psql_scalar "SELECT count(*)
  FROM memory.deferred_reconciliation_scan_run_v5")" == "0" ]]
[[ "$(psql_scalar "SELECT (
  (SELECT pg_get_userbyid(relowner)='memory_v5_writer'
     AND relrowsecurity AND relforcerowsecurity
   FROM pg_class
   WHERE oid='memory.deferred_reconciliation_scan_run_v5'::regclass)
  AND (SELECT count(*)=1
       FROM pg_policies
       WHERE schemaname='memory'
         AND tablename='deferred_reconciliation_scan_run_v5'
         AND policyname='owner_isolation'
         AND roles=ARRAY['memory_v5_writer']::name[]
         AND cmd='ALL')
  AND (SELECT count(*)=1
       FROM pg_trigger
       WHERE tgrelid='memory.deferred_reconciliation_scan_run_v5'::regclass
         AND tgname='deferred_reconciliation_scan_run_v5_append_only_guard'
         AND tgenabled<>'D')
  AND pg_get_userbyid((
    SELECT proowner FROM pg_proc
    WHERE oid='memory.run_deferred_reconciliation_scan_v5(uuid,integer,text,text)'::regprocedure
  ))='memory_v5_writer'
  AND has_function_privilege(
    'brains_app',
    'memory.run_deferred_reconciliation_scan_v5(uuid,integer,text,text)',
    'EXECUTE'
  )
  AND NOT has_table_privilege(
    'brains_app',
    'memory.deferred_reconciliation_scan_run_v5',
    'SELECT'
  )
  AND NOT has_table_privilege(
    'brains_app',
    'memory.deferred_reconciliation_scan_run_v5',
    'INSERT'
  )
  AND NOT has_table_privilege(
    'brains_app',
    'memory.deferred_reconciliation_scan_run_v5',
    'UPDATE'
  )
  AND NOT has_table_privilege(
    'brains_app',
    'memory.deferred_reconciliation_scan_run_v5',
    'DELETE'
  )
  AND NOT EXISTS (
    SELECT 1
    FROM pg_proc AS procedure
    CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
    WHERE procedure.oid=
      'memory.run_deferred_reconciliation_scan_v5(uuid,integer,text,text)'::regprocedure
      AND acl.grantee=0
      AND acl.privilege_type='EXECUTE'
  )
)::int")" == "1" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=report
report="$snapshot_dir/memory_v1_deferred_scan_audit_${run_id}.json"
BACKUP="$backup" CATALOG="$catalog" BASELINE="$baseline" POST="$post" \
LOG="$log" REPORT="$report" QDRANT_BEFORE="$qdrant_before" \
QDRANT_AFTER="$qdrant_after" HEAD="$(git -C "$repo_root" rev-parse HEAD)" \
python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

value = {
    "contract_version": "memory_v1_deferred_scan_audit_install_report_v1",
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
        "audit_table_empty": True,
        "append_only": True,
        "forced_owner_rls": True,
        "brains_app_has_function_only": True,
        "rollback_only_security_tests_passed": True,
        "preexisting_memory_rows_unchanged": True,
        "qdrant_unchanged": True,
        "automatic_reconciliation": False,
        "prompt_influence": False,
    },
}
Path(os.environ["REPORT"]).write_text(
    json.dumps(value, indent=2, sort_keys=True) + "\n"
)
PY
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
audit_installed=0
phase=complete
printf 'memory_v1_deferred_scan_audit_v5_production_install: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
