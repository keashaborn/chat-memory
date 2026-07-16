#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
sql_file=ops/sql/20260716_memory_v1_phase0_backlog_manifest.sql
production_repo=/opt/chat-memory
container=brains-postgres-1
database=memory
timer=memory-v1-consolidation.timer
service=memory-v1-consolidation.service
trigger=chat_log_enqueue_memory_v1_consolidation
expected_rows=596
expected_production_head=7484c4432cd9b920bc20024277776109584b70b7
output=${OUTPUT:-$repo_root/ops/manifests/memory_v1_phase0_backlog_20260716.json}

if [[ -z "${FREEZE_REPORT:-}" ]]; then
  echo "FREEZE_REPORT is required" >&2
  exit 1
fi

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
[[ -z "$(git -C "$production_repo" status --porcelain)" ]]
[[ "$(git -C "$production_repo" rev-parse HEAD)" == "$expected_production_head" ]]
[[ -f "$FREEZE_REPORT" ]]
[[ -f "$FREEZE_REPORT.sha256" ]]
(cd "$(dirname "$FREEZE_REPORT")" && sha256sum -c "$(basename "$FREEZE_REPORT").sha256")
jq -e '
  .checks.v4_capture_trigger_disabled == true and
  .checks.consolidation_timer_disabled == true and
  .checks.postgres_rows_unchanged == true and
  .checks.qdrant_metadata_unchanged == true and
  .counts.pending_jobs_after == 596
' "$FREEZE_REPORT" >/dev/null
[[ "$(jq -r .production_head_commit "$FREEZE_REPORT")" == "$expected_production_head" ]]
[[ "$(docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 -U sage -d "$database" -c \
  "SELECT tgenabled FROM pg_trigger WHERE tgrelid='public.chat_log'::regclass AND tgname='$trigger' AND NOT tgisinternal")" == "D" ]]
if systemctl is-enabled --quiet "$timer"; then
  echo "$timer must remain disabled" >&2
  exit 1
fi
if systemctl is-active --quiet "$timer"; then
  echo "$timer must remain inactive" >&2
  exit 1
fi
if systemctl is-active --quiet "$service"; then
  echo "$service must be inactive" >&2
  exit 1
fi
[[ ! -e "$output" ]]

umask 077
rows_file=$(mktemp)
trap 'rm -f "$rows_file"' EXIT

docker exec -i "$container" psql -X -q -A -t -v ON_ERROR_STOP=1 \
  -U sage -d "$database" <"$repo_root/$sql_file" >"$rows_file"

ROWS_FILE="$rows_file" OUTPUT="$output" FREEZE_REPORT="$FREEZE_REPORT" \
SQL_SHA256="$(sha256sum "$repo_root/$sql_file" | awk '{print $1}')" \
PHASE0_HEAD="$(git -C "$repo_root" rev-parse HEAD)" \
PRODUCTION_HEAD="$expected_production_head" EXPECTED_ROWS="$expected_rows" \
python3 - <<'PY'
import collections
import datetime as dt
import hashlib
import json
import os
from pathlib import Path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


rows = json.loads(Path(os.environ["ROWS_FILE"]).read_text(encoding="utf-8"))
expected = int(os.environ["EXPECTED_ROWS"])
if not isinstance(rows, list) or len(rows) != expected:
    raise RuntimeError(f"expected {expected} rows, received {len(rows) if isinstance(rows, list) else 'non-list'}")

job_ids = [row["job_id"] for row in rows]
if len(set(job_ids)) != expected or job_ids != sorted(job_ids):
    raise RuntimeError("job IDs must be unique and sorted")

allowed = {
    "source_missing",
    "source_owner_mismatch",
    "source_contract_mismatch",
    "source_hash_changed",
    "already_represented_by_evidence",
    "inactive_or_deleted_owner_review",
    "active_owner_v5_eligible",
}
if any(row["classification"] not in allowed for row in rows):
    raise RuntimeError("unexpected classification")

by_pipeline = collections.Counter(row["pipeline_version"] for row in rows)
by_classification = collections.Counter(row["classification"] for row in rows)
by_pipeline_classification = collections.Counter(
    (row["pipeline_version"], row["classification"]) for row in rows
)
by_owner_classification = collections.Counter(
    (row["owner_user_id"], row["classification"]) for row in rows
)

expected_pipeline = {"20260714_v1": 540, "20260714_v4": 56}
expected_classification = {
    "active_owner_v5_eligible": 262,
    "already_represented_by_evidence": 11,
    "inactive_or_deleted_owner_review": 323,
}
if dict(by_pipeline) != expected_pipeline:
    raise RuntimeError(f"pipeline counts changed: {dict(by_pipeline)}")
if dict(by_classification) != expected_classification:
    raise RuntimeError(f"classification counts changed: {dict(by_classification)}")

freeze_report = Path(os.environ["FREEZE_REPORT"])
freeze_sha = hashlib.sha256(freeze_report.read_bytes()).hexdigest()
value = {
    "contract_version": "memory_v1_phase0_backlog_manifest_v1",
    "captured_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "phase0_head_commit": os.environ["PHASE0_HEAD"],
    "production_head_commit": os.environ["PRODUCTION_HEAD"],
    "freeze_report": {"path": str(freeze_report), "sha256": freeze_sha},
    "query": {"path": os.environ.get("SQL_FILE", "ops/sql/20260716_memory_v1_phase0_backlog_manifest.sql"), "sha256": os.environ["SQL_SHA256"]},
    "effects": {"database_writes": 0, "qdrant_writes": 0, "model_calls": 0, "prompt_influence_changes": 0},
    "producer_state": {"v4_capture_trigger": "disabled", "consolidation_timer": "disabled_inactive", "consolidation_service": "inactive"},
    "active_owner_roster": [
        "1240822d-ac9a-4096-95aa-e2b24d36ef50",
        "557ea042-cb82-48f8-9429-472e96c957ef",
        "d839b4bc-0bd2-4f2d-aafe-0f3f75883db8",
        "818b60b9-89bd-442a-998c-fc1924184dfc",
        "5c9f624a-a66d-4183-babb-b3a0f0f4e733",
        "673d64a3-c4ba-4d1c-89e3-e0c579022fad",
    ],
    "classification_precedence": [
        "source_missing",
        "source_owner_mismatch",
        "source_contract_mismatch",
        "source_hash_changed",
        "already_represented_by_evidence",
        "inactive_or_deleted_owner_review",
        "active_owner_v5_eligible",
    ],
    "counts": {
        "total": len(rows),
        "by_pipeline": dict(sorted(by_pipeline.items())),
        "by_classification": dict(sorted(by_classification.items())),
        "by_pipeline_classification": [
            {"pipeline_version": key[0], "classification": key[1], "count": count}
            for key, count in sorted(by_pipeline_classification.items())
        ],
        "by_owner_classification": [
            {"owner_user_id": key[0], "classification": key[1], "count": count}
            for key, count in sorted(by_owner_classification.items())
        ],
    },
    "rows_sha256": hashlib.sha256(canonical(rows)).hexdigest(),
    "rows": rows,
}
value["manifest_payload_sha256"] = hashlib.sha256(canonical(value)).hexdigest()
Path(os.environ["OUTPUT"]).write_text(
    json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY

chmod 0600 "$output"
sha256sum "$output" >"$output.sha256"
chmod 0600 "$output.sha256"

[[ "$(docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 -U sage -d "$database" -c \
  "SELECT count(*) FROM memory.consolidation_job WHERE status='pending'")" == "$expected_rows" ]]
[[ "$(docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 -U sage -d "$database" -c \
  "SELECT tgenabled FROM pg_trigger WHERE tgrelid='public.chat_log'::regclass AND tgname='$trigger' AND NOT tgisinternal")" == "D" ]]

printf 'memory_v1_phase0_backlog_manifest: PASS\n'
printf 'manifest=%s\n' "$output"
printf 'sha256=%s\n' "$(awk '{print $1}' "$output.sha256")"
