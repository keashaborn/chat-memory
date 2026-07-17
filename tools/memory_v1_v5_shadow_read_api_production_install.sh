#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs one owner-scoped, read-only V5 shadow API.
# It writes no Memory V1 rows and does not index, retrieve, or alter prompts.

if [[ "${MEMORY_V1_V5_SHADOW_READ_INSTALL:-}" != "authorized" ]]; then
  echo "MEMORY_V1_V5_SHADOW_READ_INSTALL=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
migration=ops/sql/20260716_memory_v1_v5_shadow_read_api.sql
rollback=ops/sql/20260716_memory_v1_v5_shadow_read_api_rollback.sql
test_sql=tests/memory_v1_v5_shadow_read_api.sql
required_ancestor=6ee1720a7bf6cd797e936dea09a63362b50932a6
expected_migration_sha=ba867c1d9c6c12b8a5ec2fc6339100dee2cda1d382bc514b3475b8192ded7f8b
expected_test_sha=2db9a37302e5aba10c6fa094fb7d241ad17d367e01b5430d69e2694f8ccb1b77
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_shadow_read_install.lock
phase=initialization
status_file=
schema_installed=0

[[ -z "$(git -C "$repo_root" status --porcelain)" ]] || {
  echo "production install requires a clean Git worktree" >&2
  exit 1
}
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(sha256sum "$repo_root/$migration" | awk '{print $1}')" == "$expected_migration_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | awk '{print $1}')" == "$expected_test_sha" ]]

exec 9>"$lock_file"
flock -n 9 || {
  echo "another V5 shadow reader install holds the lock" >&2
  exit 1
}
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_shadow_read_install_${run_id}.status"

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
      >"$snapshot_dir/memory_v1_v5_shadow_read_rollback_${run_id}.log" 2>&1 \
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
    | jq -cS '.result.points | sort_by(.id)' | sha256sum | awk '{print $1}'
}

capture_state() {
  local output=$1
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
  done < <(psql_scalar "
    SELECT table_name FROM information_schema.tables
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name
  ")
  chmod 0600 "$output"
}

phase=preflight
[[ "$(psql_scalar "SELECT (
  to_regprocedure('memory.require_v5_reader_context()') IS NULL
  AND to_regprocedure('memory.read_v5_shadow_claims(uuid[])') IS NULL
  AND to_regrole('memory_v5_reader') IS NULL
  AND to_regclass('memory.projection_apply_event_owner_claim_idx') IS NULL
)::int")" == "1" ]]

phase=backup
backup_partial="$snapshot_dir/.memory_pre_v5_shadow_read_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_shadow_read_${run_id}.dump"
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
baseline="$snapshot_dir/memory_v1_v5_shadow_read_baseline_${run_id}.tsv"
post="$snapshot_dir/memory_v1_v5_shadow_read_post_${run_id}.tsv"
capture_state "$baseline"
qdrant_before=$(qdrant_signature)

phase=schema_install
install_log="$snapshot_dir/memory_v1_v5_shadow_read_install_${run_id}.log"
run_sql_file "$migration" >"$install_log" 2>&1
schema_installed=1

phase=read_only_security_test
run_sql_file "$test_sql" >>"$install_log" 2>&1
chmod 0600 "$install_log"

phase=postflight
capture_state "$post"
cmp -s "$baseline" "$post" || {
  diff -u "$baseline" "$post" >&2 || true
  echo "Memory V1 row state changed during V5 reader installation" >&2
  exit 1
}
[[ "$(psql_scalar "
  SELECT pg_get_userbyid((SELECT proowner FROM pg_proc
    WHERE oid='memory.read_v5_shadow_claims(uuid[])'::regprocedure))
")" == "memory_v5_reader" ]]
[[ "$(psql_scalar "
  SELECT has_function_privilege(
    'brains_app','memory.read_v5_shadow_claims(uuid[])','EXECUTE'
  )::int
")" == "1" ]]
[[ "$(psql_scalar "
  SELECT count(*) FROM information_schema.role_table_grants
  WHERE grantee='brains_app' AND table_schema='memory'
    AND table_name IN (
      'claim_observation','observation','observation_temporal',
      'projection_apply_event'
    )
")" == "0" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]] || {
  echo "Qdrant changed during V5 reader installation" >&2
  exit 1
}

phase=report
report="$snapshot_dir/memory_v1_v5_shadow_read_install_${run_id}.json"
BACKUP="$backup" CATALOG="$catalog" BASELINE="$baseline" POST="$post" \
LOG="$install_log" REPORT="$report" QDRANT_BEFORE="$qdrant_before" \
QDRANT_AFTER="$qdrant_after" HEAD="$(git -C "$repo_root" rev-parse HEAD)" \
python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

value = {
    "contract_version": "memory_v1_v5_shadow_read_install_report_v1",
    "instruction_source": "user_continue_memory_v1_20260716",
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
        "restricted_reader_role": True,
        "owner_scoped_read_function": True,
        "missing_actor_rejected": True,
        "direct_table_read_rejected": True,
        "cross_owner_non_disclosure": True,
        "bounded_unique_candidate_input": True,
        "memory_rows_and_content_unchanged": True,
        "qdrant_unchanged": True,
        "router_modified": False,
        "prompt_influence": False,
    },
    "hard_stop": "before_v5_router_integration_or_prompt_exposure",
}
Path(os.environ["REPORT"]).write_text(
    json.dumps(value,indent=2,sort_keys=True)+"\n"
)
PY
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
schema_installed=0
phase=complete
printf 'memory_v1_v5_shadow_read_api_production_install: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
