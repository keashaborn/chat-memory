#!/usr/bin/env bash
set -euo pipefail

if [[ "${MEMORY_V1_EVIDENCE_INTAKE_APPLY:-}" != "authorized" ]]; then
  echo "MEMORY_V1_EVIDENCE_INTAKE_APPLY=authorized is required" >&2
  exit 1
fi

plan_report=${1:?dry-run plan report path is required}
repo_root=$(git rev-parse --show-toplevel)
selector=scripts/memory_v1_evidence_intake_selector.py
required_ancestor=fde3f617e8ef93e6ed6ea2c2e912d88320b7c8f4
expected_selector_sha=e104d2f0d3ce55cde4e42a7d0dde8e5fd98819b1f7a3099e961a01ea01c5b3c5
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_evidence_intake_apply.lock
owner_lifeswitch=557ea042-cb82-48f8-9429-472e96c957ef
owner_doctor=d839b4bc-0bd2-4f2d-aafe-0f3f75883db8
phase=initialization
status_file=

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(sha256sum "$repo_root/$selector" | awk '{print $1}')" == "$expected_selector_sha" ]]
[[ -f "$plan_report" ]]
[[ "$(stat -c '%a' "$plan_report")" == "600" ]]

if rg -n '(^|[^a-zA-Z])(OpenAI|responses\.create|chat\.completions)' \
  "$repo_root/$selector"; then
  echo "evidence intake selector contains an external model caller" >&2
  exit 1
fi

exec 9>"$lock_file"
flock -n 9 || {
  echo "another evidence intake apply holds the lock" >&2
  exit 1
}
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_evidence_intake_apply_${run_id}.status"

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
}

record_exit() {
  code=$?
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

capture_nonterminal_state() {
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

terminal_signature() {
  psql_scalar "
    SELECT count(*)::text || E'\\t' ||
           encode(digest(coalesce(string_agg(row_json,E'\\n'
             ORDER BY row_json),''),'sha256'),'hex')
    FROM (
      SELECT to_jsonb(table_row)::text AS row_json
      FROM memory.evidence_intake_terminal AS table_row
    ) rows
  "
}

validate_plan() {
  local expected=$1
  local actual=${2:-}
  EXPECTED="$expected" ACTUAL="$actual" python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path

def load(path):
    report = json.loads(Path(path).read_text())
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
    assert sum(owner["before"]["rows"] for owner in report["owners"]) == 26
    outcomes = {}
    for owner in report["owners"]:
        for key, value in owner["before"]["outcomes"].items():
            outcomes[key] = outcomes.get(key, 0) + value
    assert outcomes == {"empty": 24, "skipped": 2}
    return {
        owner["owner_user_id"]: owner["plan_sha256"]
        for owner in report["owners"]
    }

expected = load(os.environ["EXPECTED"])
if os.environ["ACTUAL"]:
    assert load(os.environ["ACTUAL"]) == expected
PY
}

phase=preflight
validate_plan "$plan_report"
[[ "$(psql_scalar "SELECT (
  to_regclass('memory.evidence_intake_terminal') IS NOT NULL
  AND to_regrole('memory_intake_maintainer') IS NOT NULL
  AND to_regprocedure(
    'memory.plan_owner_evidence_intake_v1(text,integer,uuid)'
  ) IS NOT NULL
  AND to_regprocedure(
    'memory.record_owner_evidence_intake_terminal_v1(uuid,text,text,text,text)'
  ) IS NOT NULL
  AND (SELECT count(*) FROM memory.evidence_intake_terminal)=0
)::int")" == "1" ]]

phase=backup
partial="$snapshot_dir/.memory_pre_evidence_intake_apply_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_evidence_intake_apply_${run_id}.dump"
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
baseline="$snapshot_dir/memory_v1_evidence_intake_apply_baseline_${run_id}.tsv"
post="$snapshot_dir/memory_v1_evidence_intake_apply_post_${run_id}.tsv"
capture_nonterminal_state "$baseline"
qdrant_before=$(qdrant_signature)

set -a
source /opt/chat-memory/.env
set +a

phase=plan_revalidation
current_plan="$snapshot_dir/memory_v1_evidence_intake_apply_plan_${run_id}.json"
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$selector" \
  --owner-user-id "$owner_lifeswitch" \
  --owner-user-id "$owner_doctor" \
  --selector-version 20260716_v1 \
  --limit 100 \
  --report-path "$current_plan"
chmod 0600 "$current_plan"
validate_plan "$plan_report" "$current_plan"

phase=terminal_apply
apply_report="$snapshot_dir/memory_v1_evidence_intake_apply_rows_${run_id}.json"
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$selector" \
  --owner-user-id "$owner_lifeswitch" \
  --owner-user-id "$owner_doctor" \
  --selector-version 20260716_v1 \
  --limit 100 \
  --record-terminal \
  --report-path "$apply_report"
chmod 0600 "$apply_report"
[[ "$(psql_scalar "
  SELECT (
    count(*)=26
    AND count(*) FILTER(
      WHERE outcome='empty'
        AND reason_code='upstream_completed_empty'
    )=24
    AND count(*) FILTER(
      WHERE outcome='skipped'
        AND reason_code='upstream_review_required'
    )=2
    AND count(DISTINCT decision_fingerprint)=26
    AND count(*) FILTER(
      WHERE owner_user_id='$owner_lifeswitch'::uuid
    )=20
    AND count(*) FILTER(
      WHERE owner_user_id='$owner_doctor'::uuid
    )=6
  )::int
  FROM memory.evidence_intake_terminal
  WHERE selector_version='20260716_v1'
")" == "1" ]]

phase=replay
terminal_before_replay=$(terminal_signature)
replay_report="$snapshot_dir/memory_v1_evidence_intake_apply_replay_${run_id}.json"
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$selector" \
  --owner-user-id "$owner_lifeswitch" \
  --owner-user-id "$owner_doctor" \
  --selector-version 20260716_v1 \
  --limit 100 \
  --record-terminal \
  --report-path "$replay_report"
chmod 0600 "$replay_report"
terminal_after_replay=$(terminal_signature)
[[ "$terminal_after_replay" == "$terminal_before_replay" ]]

phase=postflight
capture_nonterminal_state "$post"
cmp -s "$baseline" "$post"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

APPLY_REPORT="$apply_report" REPLAY_REPORT="$replay_report" python3 - <<'PY'
import json
import os
from pathlib import Path

apply = json.loads(Path(os.environ["APPLY_REPORT"]).read_text())
replay = json.loads(Path(os.environ["REPLAY_REPORT"]).read_text())
assert sum(owner["record"]["applied"] for owner in apply["owners"]) == 26
assert sum(owner["record"]["replayed"] for owner in apply["owners"]) == 0
assert sum(owner["after"]["rows"] for owner in apply["owners"]) == 0
assert sum(owner["before"]["rows"] for owner in replay["owners"]) == 0
assert sum(owner["record"]["applied"] for owner in replay["owners"]) == 0
assert sum(owner["record"]["replayed"] for owner in replay["owners"]) == 0
PY

phase=report
report="$snapshot_dir/memory_v1_evidence_intake_apply_${run_id}.json"
BACKUP="$backup" CATALOG="$catalog" BASELINE="$baseline" POST="$post" \
PLAN_REPORT="$plan_report" CURRENT_PLAN="$current_plan" \
APPLY_REPORT="$apply_report" REPLAY_REPORT="$replay_report" \
REPORT="$report" QDRANT_BEFORE="$qdrant_before" \
QDRANT_AFTER="$qdrant_after" HEAD="$(git -C "$repo_root" rev-parse HEAD)" \
python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

value = {
    "contract_version": "memory_v1_evidence_intake_apply_report_v1",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "backup": {"path": os.environ["BACKUP"], "catalog": os.environ["CATALOG"]},
    "plan_report": os.environ["PLAN_REPORT"],
    "current_plan": os.environ["CURRENT_PLAN"],
    "apply_report": os.environ["APPLY_REPORT"],
    "replay_report": os.environ["REPLAY_REPORT"],
    "evidence": {
        "baseline": os.environ["BASELINE"],
        "post": os.environ["POST"],
        "qdrant_before_sha256": os.environ["QDRANT_BEFORE"],
        "qdrant_after_sha256": os.environ["QDRANT_AFTER"],
    },
    "checks": {
        "plan_hashes_revalidated": True,
        "all_owner_transaction": True,
        "terminal_rows_created": 26,
        "empty_rows": 24,
        "skipped_rows": 2,
        "zero_write_replay": True,
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
phase=complete
printf 'memory_v1_evidence_intake_terminal_production_apply: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
