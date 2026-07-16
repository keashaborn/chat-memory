#!/usr/bin/env bash
set -euo pipefail

if [[ "${MEMORY_V1_EVIDENCE_INTAKE_INSTALL:-}" != "authorized" ]]; then
  echo "MEMORY_V1_EVIDENCE_INTAKE_INSTALL=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
migration=ops/sql/20260716_memory_v1_evidence_intake_selector.sql
rollback=ops/sql/20260716_memory_v1_evidence_intake_selector_rollback.sql
test_sql=tests/memory_v1_evidence_intake_selector.sql
selector=scripts/memory_v1_evidence_intake_selector.py
required_ancestor=fde3f617e8ef93e6ed6ea2c2e912d88320b7c8f4
expected_migration_sha=ece97bba4eb48b5718afa8532adafa6724cd50f46d8d7be8097b1fe42b42df3e
expected_rollback_sha=cc2da6f29e9c020e44daee1d4e884113053dd7f3ef3be826a65bf23bef2b3089
expected_test_sha=4e2be39cb8a3b399996c322b104d6814102a76fa2513c026a134d661d227a715
expected_selector_sha=e104d2f0d3ce55cde4e42a7d0dde8e5fd98819b1f7a3099e961a01ea01c5b3c5
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_evidence_intake_install.lock
owner_lifeswitch=557ea042-cb82-48f8-9429-472e96c957ef
owner_doctor=d839b4bc-0bd2-4f2d-aafe-0f3f75883db8
phase=initialization
status_file=
migration_applied=0

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(sha256sum "$repo_root/$migration" | awk '{print $1}')" == "$expected_migration_sha" ]]
[[ "$(sha256sum "$repo_root/$rollback" | awk '{print $1}')" == "$expected_rollback_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | awk '{print $1}')" == "$expected_test_sha" ]]
[[ "$(sha256sum "$repo_root/$selector" | awk '{print $1}')" == "$expected_selector_sha" ]]

if rg -n '(^|[^a-zA-Z])(OpenAI|responses\.create|chat\.completions)' \
  "$repo_root/$selector"; then
  echo "evidence intake selector contains an external model caller" >&2
  exit 1
fi

exec 9>"$lock_file"
flock -n 9 || {
  echo "another evidence intake install holds the lock" >&2
  exit 1
}
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_evidence_intake_install_${run_id}.status"

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
      >"$snapshot_dir/memory_v1_evidence_intake_rollback_${run_id}.log" \
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
        SELECT to_jsonb(table_row)::text AS row_json
        FROM memory.\"$table\" AS table_row
      ) rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done < <(psql_scalar "
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema='memory'
      AND table_type='BASE TABLE'
      AND table_name<>'evidence_intake_terminal'
    ORDER BY table_name
  ")
  chmod 0600 "$output"
}

phase=preflight
[[ "$(psql_scalar "SELECT (
  to_regclass('memory.evidence_intake_terminal') IS NULL
  AND to_regrole('memory_intake_maintainer') IS NULL
  AND to_regprocedure(
    'memory.plan_owner_evidence_intake_v1(text,integer,uuid)'
  ) IS NULL
  AND to_regprocedure(
    'memory.record_owner_evidence_intake_terminal_v1(uuid,text,text,text,text)'
  ) IS NULL
)::int")" == "1" ]]

phase=backup
partial="$snapshot_dir/.memory_pre_evidence_intake_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_evidence_intake_${run_id}.dump"
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
baseline="$snapshot_dir/memory_v1_evidence_intake_baseline_${run_id}.tsv"
post="$snapshot_dir/memory_v1_evidence_intake_post_${run_id}.tsv"
capture_existing_state "$baseline"
qdrant_before=$(qdrant_signature)

phase=schema_install
log="$snapshot_dir/memory_v1_evidence_intake_install_${run_id}.log"
run_sql_file "$migration" >"$log" 2>&1
migration_applied=1

phase=rollback_only_security_test
run_sql_file "$test_sql" >>"$log" 2>&1
chmod 0600 "$log"

phase=production_dry_run
dry_report="$snapshot_dir/memory_v1_evidence_intake_dry_${run_id}.json"
set -a
source /opt/chat-memory/.env
set +a
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$selector" \
  --owner-user-id "$owner_lifeswitch" \
  --owner-user-id "$owner_doctor" \
  --selector-version 20260716_v1 \
  --limit 100 \
  --report-path "$dry_report" >>"$log" 2>&1
chmod 0600 "$dry_report"
sha256sum "$dry_report" >"$dry_report.sha256"
chmod 0600 "$dry_report.sha256"

DRY_REPORT="$dry_report" python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path

report = json.loads(Path(os.environ["DRY_REPORT"]).read_text())
stored = report.pop("report_sha256")
encoded = json.dumps(
    report,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
    default=str,
).encode()
assert hashlib.sha256(encoded).hexdigest() == stored
assert report["contract_version"] == "memory_v1_evidence_intake_selector_report_v1"
assert report["selector_version"] == "20260716_v1"
assert report["record_terminal"] is False
assert report["model_calls"] == 0
assert report["candidate_writes"] == 0
assert report["claim_writes"] == 0
assert len(report["owners"]) == 2
outcomes = {}
for owner in report["owners"]:
    for key, value in owner["before"]["outcomes"].items():
        outcomes[key] = outcomes.get(key, 0) + value
assert outcomes == {"empty": 24, "skipped": 2}
assert sum(owner["before"]["rows"] for owner in report["owners"]) == 26
assert sum(owner["before"]["terminal_rows"] for owner in report["owners"]) == 26
PY

phase=postflight
[[ "$(psql_scalar "SELECT count(*) FROM memory.evidence_intake_terminal")" == "0" ]]
capture_existing_state "$post"
cmp -s "$baseline" "$post"
[[ "$(psql_scalar "SELECT (
  EXISTS (
    SELECT 1
    FROM pg_roles
    WHERE rolname='memory_intake_maintainer'
      AND NOT rolcanlogin
      AND NOT rolsuper
      AND NOT rolcreatedb
      AND NOT rolcreaterole
      AND NOT rolinherit
      AND NOT rolbypassrls
  )
  AND NOT has_table_privilege(
    'brains_app','memory.evidence_intake_terminal','INSERT'
  )
  AND NOT has_table_privilege(
    'brains_app','memory.evidence_intake_terminal','UPDATE'
  )
  AND NOT has_table_privilege(
    'brains_app','memory.evidence_intake_terminal','DELETE'
  )
  AND has_table_privilege(
    'brains_app','memory.evidence_intake_terminal','SELECT'
  )
  AND to_regclass('memory.artifact_endorsement_owner_evidence_idx') IS NOT NULL
  AND to_regclass('memory.entity_alias_owner_evidence_idx') IS NOT NULL
  AND to_regclass('memory.user_preference_owner_evidence_idx') IS NOT NULL
)::int")" == "1" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=report
report="$snapshot_dir/memory_v1_evidence_intake_install_${run_id}.json"
BACKUP="$backup" CATALOG="$catalog" BASELINE="$baseline" POST="$post" \
LOG="$log" REPORT="$report" DRY_REPORT="$dry_report" \
QDRANT_BEFORE="$qdrant_before" QDRANT_AFTER="$qdrant_after" \
HEAD="$(git -C "$repo_root" rev-parse HEAD)" \
python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

value = {
    "contract_version": "memory_v1_evidence_intake_install_report_v1",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "backup": {"path": os.environ["BACKUP"], "catalog": os.environ["CATALOG"]},
    "dry_run_report": os.environ["DRY_REPORT"],
    "evidence": {
        "baseline": os.environ["BASELINE"],
        "post": os.environ["POST"],
        "log": os.environ["LOG"],
        "qdrant_before_sha256": os.environ["QDRANT_BEFORE"],
        "qdrant_after_sha256": os.environ["QDRANT_AFTER"],
    },
    "checks": {
        "restricted_writer_role": True,
        "forced_rls": True,
        "append_only_terminal_ledger": True,
        "direct_table_writes_denied": True,
        "cross_owner_access_denied": True,
        "production_dry_run_rows": 26,
        "production_dry_run_empty": 24,
        "production_dry_run_skipped": 2,
        "terminal_rows_written": 0,
        "nonterminal_memory_rows_unchanged": True,
        "qdrant_unchanged": True,
        "model_calls": 0,
        "candidate_writes": 0,
        "claim_writes": 0,
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
printf 'memory_v1_evidence_intake_selector_production_install: PASS\n'
printf 'report=%s\nbackup=%s\ndry_run_report=%s\n' \
  "$report" "$backup" "$dry_report"
