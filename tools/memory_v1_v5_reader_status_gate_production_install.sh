#!/usr/bin/env bash
set -euo pipefail

if [[ "${MEMORY_V1_V5_READER_STATUS_GATE_INSTALL:-}" != "authorized" ]]; then
  echo "MEMORY_V1_V5_READER_STATUS_GATE_INSTALL=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
migration=ops/sql/20260716_memory_v1_v5_reader_status_gate.sql
rollback=ops/sql/20260716_memory_v1_v5_reader_status_gate_rollback.sql
test_sql=tests/memory_v1_v5_reader_status_gate.sql
required_ancestor=b31941c72c5164845cc07e714e1833fa67827cbd
expected_migration_sha=565ca182fa305da30002e1aa8de4fab2fe5652c8f18f253965f3236a4ed9b1d4
expected_rollback_sha=4922c37a1a13daf5851dcf4d73095a38ae1be4f571c62b460d9e5de4c1245fc8
expected_test_sha=d41c6174011b7b7e40c17722953c8e63928b8d0f56b1b1fd8f4bb9d1a136a658
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_reader_status_gate_install.lock
phase=initialization
status_file=
policy_installed=0

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(sha256sum "$repo_root/$migration" | awk '{print $1}')" == "$expected_migration_sha" ]]
[[ "$(sha256sum "$repo_root/$rollback" | awk '{print $1}')" == "$expected_rollback_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | awk '{print $1}')" == "$expected_test_sha" ]]

exec 9>"$lock_file"
flock -n 9 || { echo "another V5 reader status install holds the lock" >&2; exit 1; }
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_reader_status_gate_${run_id}.status"

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
  if [[ "$code" -ne 0 && "$policy_installed" -eq 1 ]]; then
    phase=automatic_rollback_after_failure
    run_sql_file "$rollback" \
      >"$snapshot_dir/memory_v1_v5_reader_status_gate_rollback_${run_id}.log" \
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
    SELECT table_name FROM information_schema.tables
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name
  ")
  chmod 0600 "$output"
}
actor_read_count() {
  docker exec -i "$container" psql -X -q -A -t \
    -v ON_ERROR_STOP=1 -U sage -d "$database" <<'SQL'
BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
) \gset
SELECT count(*) FROM memory.read_v5_shadow_claims(
  ARRAY['50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid]
);
ROLLBACK;
RESET SESSION AUTHORIZATION;
SQL
}

phase=preflight
[[ "$(psql_scalar "SELECT count(*) FROM pg_policies
  WHERE schemaname='memory' AND tablename='claim'
    AND policyname='surfaceable_status_v5_reader'")" == "0" ]]
[[ "$(actor_read_count)" == "1" ]]

phase=backup
partial="$snapshot_dir/.memory_pre_v5_reader_status_gate_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_reader_status_gate_${run_id}.dump"
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
baseline="$snapshot_dir/memory_v1_v5_reader_status_gate_baseline_${run_id}.tsv"
post="$snapshot_dir/memory_v1_v5_reader_status_gate_post_${run_id}.tsv"
capture_state "$baseline"
qdrant_before=$(qdrant_signature)

phase=policy_install
log="$snapshot_dir/memory_v1_v5_reader_status_gate_install_${run_id}.log"
run_sql_file "$migration" >"$log" 2>&1
policy_installed=1

phase=rollback_only_security_test
run_sql_file "$test_sql" >>"$log" 2>&1
chmod 0600 "$log"

phase=postflight
capture_state "$post"
cmp -s "$baseline" "$post"
[[ "$(actor_read_count)" == "0" ]]
[[ "$(psql_scalar "SELECT (
  (SELECT count(*) FROM pg_policies
    WHERE schemaname='memory' AND tablename='claim'
      AND policyname='surfaceable_status_v5_reader'
      AND roles=ARRAY['memory_v5_reader']::name[]
      AND permissive='RESTRICTIVE' AND cmd='SELECT')=1
  AND (SELECT count(*) FROM memory.claim
    WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
      AND claim_id='50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid
      AND status='retracted' AND confidence=0)=1
)::int")" == "1" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=report
report="$snapshot_dir/memory_v1_v5_reader_status_gate_${run_id}.json"
BACKUP="$backup" CATALOG="$catalog" BASELINE="$baseline" POST="$post" \
LOG="$log" REPORT="$report" QDRANT_BEFORE="$qdrant_before" \
QDRANT_AFTER="$qdrant_after" HEAD="$(git -C "$repo_root" rev-parse HEAD)" \
python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

value = {
    "contract_version": "memory_v1_v5_reader_status_gate_install_report_v1",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "backup": {"path": os.environ["BACKUP"], "catalog": os.environ["CATALOG"]},
    "evidence": {
        "baseline": os.environ["BASELINE"], "post": os.environ["POST"],
        "log": os.environ["LOG"],
        "qdrant_before_sha256": os.environ["QDRANT_BEFORE"],
        "qdrant_after_sha256": os.environ["QDRANT_AFTER"],
    },
    "checks": {
        "restrictive_reader_rls": True,
        "retracted_claim_hidden": True,
        "quarantined_claims_hidden_by_policy": True,
        "surfaceable_candidate_control_test": True,
        "cross_owner_hidden": True,
        "memory_rows_unchanged": True,
        "qdrant_unchanged": True,
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
policy_installed=0
phase=complete
printf 'memory_v1_v5_reader_status_gate_production_install: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
