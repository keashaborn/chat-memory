#!/usr/bin/env bash
set -euo pipefail

if [[ "${MEMORY_V1_RECORD_EVIDENCE_INSTALL:-}" != "authorized" ]]; then
  echo "MEMORY_V1_RECORD_EVIDENCE_INSTALL=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
migration=ops/sql/20260716_memory_v1_record_evidence_api.sql
rollback=ops/sql/20260716_memory_v1_record_evidence_api_rollback.sql
test_sql=tests/memory_v1_record_evidence_api.sql
required_ancestor=fba3198cb269dc0d43a118388828e38244d3bdc1
expected_migration_sha=2552ea0abdb25cd2de4165c6115402f3ff53cb94b2242fd2203d99f4ea7a19de
expected_rollback_sha=e8a0976235bb67382fdf189c35439d5a3c1217a0937df3cb866664d51f358aaa
expected_test_sha=ad914c6495c9a681403d09e518c8c10041a39965cb877315fe7f0948b3f66709
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_record_evidence_install.lock
phase=initialization
status_file=
function_installed=0

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(sha256sum "$repo_root/$migration" | awk '{print $1}')" == "$expected_migration_sha" ]]
[[ "$(sha256sum "$repo_root/$rollback" | awk '{print $1}')" == "$expected_rollback_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | awk '{print $1}')" == "$expected_test_sha" ]]

exec 9>"$lock_file"
flock -n 9 || {
  echo "another record evidence install holds the lock" >&2
  exit 1
}
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_record_evidence_${run_id}.status"

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
  if [[ "$code" -ne 0 && "$function_installed" -eq 1 ]]; then
    phase=automatic_rollback_after_failure
    run_sql_file "$rollback" \
      >"$snapshot_dir/memory_v1_record_evidence_rollback_${run_id}.log" \
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
        SELECT to_jsonb(table_row)::text row_json
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
[[ "$(psql_scalar "SELECT (
  to_regprocedure(
    'memory.record_owner_evidence_v1(memory.evidence_kind,text,text,text,timestamptz,numeric,numeric,text,memory.sensitivity_level,jsonb)'
  ) IS NULL
  AND to_regrole('memory_evidence_maintainer') IS NOT NULL
)::int")" == "1" ]]
brains_acl_before=$(evidence_acl brains_app)
maintainer_acl_before=$(evidence_acl memory_evidence_maintainer)
[[ "$brains_acl_before" == "1,1,0,0" ]]
[[ "$maintainer_acl_before" == "1,0,1,0" ]]

phase=backup
partial="$snapshot_dir/.memory_pre_record_evidence_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_record_evidence_${run_id}.dump"
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
baseline="$snapshot_dir/memory_v1_record_evidence_baseline_${run_id}.tsv"
post="$snapshot_dir/memory_v1_record_evidence_post_${run_id}.tsv"
capture_state "$baseline"
qdrant_before=$(qdrant_signature)

phase=function_install
log="$snapshot_dir/memory_v1_record_evidence_install_${run_id}.log"
run_sql_file "$migration" >"$log" 2>&1
function_installed=1

phase=rollback_only_security_test
run_sql_file "$test_sql" >>"$log" 2>&1
chmod 0600 "$log"

phase=postflight
capture_state "$post"
cmp -s "$baseline" "$post"
brains_acl_after=$(evidence_acl brains_app)
maintainer_acl_after=$(evidence_acl memory_evidence_maintainer)
[[ "$brains_acl_after" == "$brains_acl_before" ]]
[[ "$maintainer_acl_after" == "1,1,1,0" ]]
[[ "$(psql_scalar "SELECT (
  EXISTS (
    SELECT 1 FROM pg_proc
    WHERE oid=
      'memory.record_owner_evidence_v1(memory.evidence_kind,text,text,text,timestamptz,numeric,numeric,text,memory.sensitivity_level,jsonb)'
      ::regprocedure
      AND prosecdef
      AND provolatile='v'
      AND proowner='memory_evidence_maintainer'::regrole
      AND proconfig=ARRAY['search_path=pg_catalog']::text[]
  )
  AND has_function_privilege(
    'brains_app',
    'memory.record_owner_evidence_v1(memory.evidence_kind,text,text,text,timestamptz,numeric,numeric,text,memory.sensitivity_level,jsonb)',
    'EXECUTE'
  )
  AND NOT EXISTS (
    SELECT 1
    FROM pg_proc AS procedure
    CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
    WHERE procedure.oid=
      'memory.record_owner_evidence_v1(memory.evidence_kind,text,text,text,timestamptz,numeric,numeric,text,memory.sensitivity_level,jsonb)'
      ::regprocedure
      AND acl.grantee=0
      AND acl.privilege_type='EXECUTE'
  )
)::int")" == "1" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=report
report="$snapshot_dir/memory_v1_record_evidence_${run_id}.json"
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
    "contract_version": "memory_v1_record_evidence_install_report_v1",
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
        "controlled_owner_writer": True,
        "zero_write_replay": True,
        "changed_content_rejected": True,
        "cross_owner_separation": True,
        "brains_evidence_acl_before": os.environ["BRAINS_ACL_BEFORE"],
        "brains_evidence_acl_after": os.environ["BRAINS_ACL_AFTER"],
        "maintainer_evidence_acl_before": os.environ["MAINTAINER_ACL_BEFORE"],
        "maintainer_evidence_acl_after": os.environ["MAINTAINER_ACL_AFTER"],
        "memory_rows_unchanged": True,
        "qdrant_unchanged": True,
        "brains_direct_insert_revoked": False,
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
function_installed=0
phase=complete
printf 'memory_v1_record_evidence_api_production_install: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
