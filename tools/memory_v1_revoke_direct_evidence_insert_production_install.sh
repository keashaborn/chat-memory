#!/usr/bin/env bash
set -euo pipefail

if [[ "${MEMORY_V1_EVIDENCE_PRIVILEGE_INSTALL:-}" != "authorized" ]]; then
  echo "MEMORY_V1_EVIDENCE_PRIVILEGE_INSTALL=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
migration=ops/sql/20260716_memory_v1_revoke_direct_evidence_insert.sql
rollback=ops/sql/20260716_memory_v1_revoke_direct_evidence_insert_rollback.sql
test_sql=tests/memory_v1_revoke_direct_evidence_insert.sql
required_ancestor=c2e31ff7f6999cb93da3b2683f29c876c404794c
expected_migration_sha=3268c191ec4bf161163a6529a68a7ae36e9d217c5821b5a1b6ee4a8007e5cde3
expected_rollback_sha=d4abfca4ea7fb5b9e9528f4f2fcd0a90a09d6846e175829a36798dc6578b8763
expected_test_sha=e45c0fbee59a3e3cbca4891e2289b3db903c90881b78f4af24ea4897f3bb475d
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_evidence_privilege_install.lock
phase=initialization
status_file=
migration_applied=0

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(sha256sum "$repo_root/$migration" | awk '{print $1}')" == "$expected_migration_sha" ]]
[[ "$(sha256sum "$repo_root/$rollback" | awk '{print $1}')" == "$expected_rollback_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | awk '{print $1}')" == "$expected_test_sha" ]]

if rg -n -U --glob '*.py' \
  'INSERT\s+INTO\s+memory\.evidence\s*\(' \
  "$repo_root/rag_engine" "$repo_root/scripts"; then
  echo "Python runtime still contains a direct evidence INSERT" >&2
  exit 1
fi

exec 9>"$lock_file"
flock -n 9 || {
  echo "another evidence privilege install holds the lock" >&2
  exit 1
}
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_evidence_privilege_${run_id}.status"

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
      >"$snapshot_dir/memory_v1_evidence_privilege_rollback_${run_id}.log" \
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
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    state=$(psql_scalar "
      SELECT count(*)::text || E'\\t' ||
             encode(digest(coalesce(string_agg(row_json,E'\\n'
               ORDER BY row_json),''),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(table_row)::text AS row_json
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

evidence_acl() {
  local role=$1
  psql_scalar "
    SELECT concat_ws(',',
      has_table_privilege('$role','memory.evidence','SELECT')::int,
      has_table_privilege('$role','memory.evidence','INSERT')::int,
      has_table_privilege('$role','memory.evidence','UPDATE')::int,
      has_table_privilege('$role','memory.evidence','DELETE')::int
    )
  "
}

phase=preflight
brains_acl_before=$(evidence_acl brains_app)
maintainer_acl_before=$(evidence_acl memory_evidence_maintainer)
[[ "$brains_acl_before" == "1,1,0,0" ]]
[[ "$maintainer_acl_before" == "1,1,1,0" ]]
[[ "$(psql_scalar "SELECT (
  EXISTS (
    SELECT 1
    FROM pg_proc
    WHERE oid=
      'memory.record_owner_evidence_v1(memory.evidence_kind,text,text,text,timestamptz,numeric,numeric,text,memory.sensitivity_level,jsonb)'
      ::regprocedure
      AND prosecdef
      AND proowner='memory_evidence_maintainer'::regrole
      AND proconfig=ARRAY['search_path=pg_catalog']::text[]
  )
  AND has_function_privilege(
    'brains_app',
    'memory.record_owner_evidence_v1(memory.evidence_kind,text,text,text,timestamptz,numeric,numeric,text,memory.sensitivity_level,jsonb)',
    'EXECUTE'
  )
)::int")" == "1" ]]

phase=backup
partial="$snapshot_dir/.memory_pre_evidence_privilege_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_evidence_privilege_${run_id}.dump"
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
baseline="$snapshot_dir/memory_v1_evidence_privilege_baseline_${run_id}.tsv"
post="$snapshot_dir/memory_v1_evidence_privilege_post_${run_id}.tsv"
capture_state "$baseline"
qdrant_before=$(qdrant_signature)

phase=privilege_install
log="$snapshot_dir/memory_v1_evidence_privilege_install_${run_id}.log"
run_sql_file "$migration" >"$log" 2>&1
migration_applied=1

phase=rollback_only_security_test
run_sql_file "$test_sql" >>"$log" 2>&1
chmod 0600 "$log"

phase=postflight
capture_state "$post"
cmp -s "$baseline" "$post"
brains_acl_after=$(evidence_acl brains_app)
maintainer_acl_after=$(evidence_acl memory_evidence_maintainer)
[[ "$brains_acl_after" == "1,0,0,0" ]]
[[ "$maintainer_acl_after" == "$maintainer_acl_before" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=report
report="$snapshot_dir/memory_v1_evidence_privilege_${run_id}.json"
BACKUP="$backup" CATALOG="$catalog" BASELINE="$baseline" POST="$post" \
LOG="$log" REPORT="$report" QDRANT_BEFORE="$qdrant_before" \
QDRANT_AFTER="$qdrant_after" HEAD="$(git -C "$repo_root" rev-parse HEAD)" \
BRAINS_ACL_BEFORE="$brains_acl_before" BRAINS_ACL_AFTER="$brains_acl_after" \
MAINTAINER_ACL_BEFORE="$maintainer_acl_before" \
MAINTAINER_ACL_AFTER="$maintainer_acl_after" \
python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

value = {
    "contract_version": "memory_v1_evidence_privilege_install_report_v1",
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
        "controlled_owner_writer_retained": True,
        "brains_direct_insert_revoked": True,
        "brains_select_retained": True,
        "direct_insert_denied": True,
        "zero_write_replay": True,
        "cross_owner_separation": True,
        "brains_evidence_acl_before": os.environ["BRAINS_ACL_BEFORE"],
        "brains_evidence_acl_after": os.environ["BRAINS_ACL_AFTER"],
        "maintainer_evidence_acl_before": os.environ["MAINTAINER_ACL_BEFORE"],
        "maintainer_evidence_acl_after": os.environ["MAINTAINER_ACL_AFTER"],
        "memory_rows_unchanged": True,
        "qdrant_unchanged": True,
        "retrieval_activation": False,
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
migration_applied=0
phase=complete
printf 'memory_v1_revoke_direct_evidence_insert_production_install: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
