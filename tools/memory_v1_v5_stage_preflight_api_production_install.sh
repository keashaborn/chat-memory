#!/usr/bin/env bash
set -euo pipefail

if [[ "${MEMORY_V1_V5_STAGE_PREFLIGHT_INSTALL:-}" != "authorized" ]]; then
  echo "MEMORY_V1_V5_STAGE_PREFLIGHT_INSTALL=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
migration=ops/sql/20260716_memory_v1_v5_stage_preflight_api.sql
rollback=ops/sql/20260716_memory_v1_v5_stage_preflight_api_rollback.sql
test_sql=tests/memory_v1_v5_stage_preflight_api.sql
required_ancestor=7eaee9f58db7ded0612fd95f27fc10670801a148
expected_migration_sha=918c9cda73fe10f8280e3bf7d2bd02bcc98335465657b19c54a1dfee2679c4b4
expected_rollback_sha=be321ae7b35516f8beee1150ac76ec99ab150be84ef78e6f5d4563c95d4910b5
expected_test_sha=d27c1535f4fd3993a4b47f91db00e2f64718372292d5ae4f08300a8c6f5d7435
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_stage_preflight_install.lock
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
  echo "another V5 stage preflight install holds the lock" >&2
  exit 1
}
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_stage_preflight_${run_id}.status"

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
      >"$snapshot_dir/memory_v1_v5_stage_preflight_rollback_${run_id}.log" \
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

phase=preflight
[[ "$(psql_scalar "SELECT (
  to_regprocedure(
    'memory.preflight_relational_stage_bundle_v5(uuid,text,text,timestamptz)'
  ) IS NULL
  AND to_regprocedure(
    'memory.stage_relational_packet_v5(uuid,uuid,text,text,text,text,text,text)'
  ) IS NOT NULL
)::int")" == "1" ]]

phase=backup
partial="$snapshot_dir/.memory_pre_v5_stage_preflight_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_stage_preflight_${run_id}.dump"
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
baseline="$snapshot_dir/memory_v1_v5_stage_preflight_baseline_${run_id}.tsv"
post="$snapshot_dir/memory_v1_v5_stage_preflight_post_${run_id}.tsv"
capture_state "$baseline"
qdrant_before=$(qdrant_signature)
evidence_select_before=$(psql_scalar "
  SELECT has_table_privilege(
    'brains_app','memory.evidence','SELECT'
  )::int
")

phase=function_install
log="$snapshot_dir/memory_v1_v5_stage_preflight_install_${run_id}.log"
run_sql_file "$migration" >"$log" 2>&1
function_installed=1

phase=rollback_only_security_test
run_sql_file "$test_sql" >>"$log" 2>&1
chmod 0600 "$log"

phase=postflight
capture_state "$post"
cmp -s "$baseline" "$post"
[[ "$(psql_scalar "SELECT (
  EXISTS (
    SELECT 1 FROM pg_proc
    WHERE oid=
      'memory.preflight_relational_stage_bundle_v5(uuid,text,text,timestamptz)'
      ::regprocedure
      AND prosecdef
      AND provolatile='s'
      AND proowner='memory_v5_writer'::regrole
      AND array_to_string(proconfig,',') LIKE '%search_path=%'
  )
  AND has_function_privilege(
    'brains_app',
    'memory.preflight_relational_stage_bundle_v5(uuid,text,text,timestamptz)',
    'EXECUTE'
  )
  AND NOT has_table_privilege('brains_app','memory.evidence','INSERT')
  AND NOT has_table_privilege('brains_app','memory.evidence','UPDATE')
  AND NOT has_table_privilege('brains_app','memory.evidence','DELETE')
  AND NOT EXISTS (
    SELECT 1
    FROM pg_proc AS procedure
    CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
    WHERE procedure.oid=
      'memory.preflight_relational_stage_bundle_v5(uuid,text,text,timestamptz)'
      ::regprocedure
      AND acl.grantee=0
      AND acl.privilege_type='EXECUTE'
  )
)::int")" == "1" ]]
evidence_select_after=$(psql_scalar "
  SELECT has_table_privilege(
    'brains_app','memory.evidence','SELECT'
  )::int
")
[[ "$evidence_select_after" == "$evidence_select_before" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=report
report="$snapshot_dir/memory_v1_v5_stage_preflight_${run_id}.json"
BACKUP="$backup" CATALOG="$catalog" BASELINE="$baseline" POST="$post" \
LOG="$log" REPORT="$report" QDRANT_BEFORE="$qdrant_before" \
QDRANT_AFTER="$qdrant_after" HEAD="$(git -C "$repo_root" rev-parse HEAD)" \
EVIDENCE_SELECT_BEFORE="$evidence_select_before" \
EVIDENCE_SELECT_AFTER="$evidence_select_after" \
python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

value = {
    "contract_version": "memory_v1_v5_stage_preflight_install_report_v1",
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
        "read_only_preflight": True,
        "runner_uses_function_only": True,
        "new_direct_evidence_grants": False,
        "legacy_evidence_select_before": bool(int(os.environ["EVIDENCE_SELECT_BEFORE"])),
        "legacy_evidence_select_after": bool(int(os.environ["EVIDENCE_SELECT_AFTER"])),
        "cross_owner_hidden": True,
        "missing_actor_rejected": True,
        "memory_rows_unchanged": True,
        "qdrant_unchanged": True,
        "external_model_calls": 0,
        "production_staging_rows_created": 0,
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
printf 'memory_v1_v5_stage_preflight_api_production_install: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
