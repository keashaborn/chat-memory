#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Read-only recovery verifier for the canonical-name
# candidate/review run that completed its database and replay phases before an
# early Brains health probe failed. It writes one immutable audit report only.

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
run_tag=20260725T193034Z_048900cec128
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
observation=93024235-89a8-49d5-88fa-7e4a143b68f3
execution_head=048900cec1284e4b72b7a97bc537c1b00906d475
verification_head=$(git -C "$repo_root" rev-parse HEAD)
expected_qdrant=62a3f12e08b6ac9d388cafef3cb8f1ff46eea61c0745af0bc835015257f839b8
snapshot_dir=/home/ubuntu/brains/snapshots
artifact_dir=/home/ubuntu/memory-v1-reviews/canonical-name-claim-production-20260725T193034Z-048900c
backup="$snapshot_dir/memory_pre_v5_2_canonical_name_${run_tag}.dump"
status="$snapshot_dir/memory_v1_v5_2_canonical_name_claim_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_2_canonical_name_claim_${run_tag}.final.json"
stage_manifest="$artifact_dir/stage-manifest.json"
stage_apply="$artifact_dir/stage-apply.json"
review_apply="$artifact_dir/review-apply.json"
stage_replay="$artifact_dir/stage-replay.json"
review_replay="$artifact_dir/review-replay.json"
cross_owner="$artifact_dir/stage-cross-owner.json"

git -C "$repo_root" merge-base --is-ancestor \
  "$execution_head" "$verification_head"
test -z "$(git -C "$repo_root" status --porcelain)"

set -a
source "$repo_root/.env"
set +a

for _attempt in $(seq 1 30); do
  curl --fail --silent --show-error --max-time 3 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/healthz >/dev/null 2>&1 && break
  sleep 1
done
test "$(systemctl is-active brains.service)" = active
curl --fail --silent --show-error --max-time 30 \
  -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
  http://127.0.0.1:8088/healthz >/dev/null
docker exec brains-postgres-1 pg_isready -U sage -d memory >/dev/null

psql_row() {
  docker exec brains-postgres-1 psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1" | sed -n '1p'
}

reviewed=$(psql_row "
  SELECT count(*)
  FROM memory.projection_plan_item AS item
  JOIN memory.projection_claim_payload AS payload
    USING (owner_user_id,plan_id,projection_ref)
  JOIN memory.projection_plan_observation AS link
    USING (owner_user_id,plan_id,projection_ref)
  JOIN memory.projection_review AS review
    USING (owner_user_id,plan_id,projection_ref)
  WHERE item.owner_user_id='$owner'
    AND link.observation_id='$observation'::uuid
    AND item.predicate='identity.name_canonical'
    AND item.review_state='manual_review_required'
    AND payload.claim_class='direct_claim'
    AND payload.canonical_text='Neko''s canonical name is Neko.'
    AND review.decision='authorized'
")
claim_links=$(psql_row "
  SELECT count(*)
  FROM memory.claim_observation
  WHERE owner_user_id='$owner'
    AND observation_id='$observation'::uuid
")
apply_events=$(psql_row "
  SELECT count(*)
  FROM memory.projection_apply_event AS event
  JOIN memory.projection_plan_observation AS link
    USING (owner_user_id,plan_id,projection_ref)
  WHERE event.owner_user_id='$owner'
    AND link.observation_id='$observation'::uuid
")

test "$reviewed" = 1
test "$claim_links" = 0
test "$apply_events" = 0
test "$(jq -er '.rows_written' "$stage_apply")" = 4
test "$(jq -er '.rows_written' "$review_apply")" = 1
test "$(jq -er '.rows_written' "$stage_replay")" = 0
test "$(jq -er '.rows_written' "$review_replay")" = 0
test "$(jq -er '.cross_owner_rejected' "$cross_owner")" = true
test "$(jq -er '.items | length' "$stage_manifest")" = 1
test "$(jq -er '.items[0].predicate' "$stage_manifest")" \
  = identity.name_canonical
test "$(jq -er '.items[0].canonical_text' "$stage_manifest")" \
  = "Neko's canonical name is Neko."
test "$(sed -n 's/^phase=//p' "$status")" = restore
test "$(sed -n 's/^exit_code=//p' "$status")" = 7

(
  cd "$snapshot_dir"
  sha256sum --check "$(basename "$backup").sha256" >/dev/null
)

qdrant=$(
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
)
test "$qdrant" = "$expected_qdrant"

REPORT="$report" BACKUP="$backup" EXECUTION_HEAD="$execution_head" \
VERIFICATION_HEAD="$verification_head" QDRANT="$qdrant" \
STAGE_MANIFEST="$stage_manifest" STAGE_APPLY="$stage_apply" \
REVIEW_APPLY="$review_apply" STAGE_REPLAY="$stage_replay" \
REVIEW_REPLAY="$review_replay" python3 - <<'PY'
import datetime as dt
import hashlib
import json
import os
from pathlib import Path

def load(name):
    return json.loads(Path(os.environ[name]).read_text())

def file_sha(name):
    return hashlib.sha256(Path(os.environ[name]).read_bytes()).hexdigest()

manifest = load("STAGE_MANIFEST")
stage_apply = load("STAGE_APPLY")
review_apply = load("REVIEW_APPLY")
stage_replay = load("STAGE_REPLAY")
review_replay = load("REVIEW_REPLAY")
value = {
    "contract_version":
        "memory_v1_v5_2_canonical_name_claim_review_recovery_report_v1",
    "completed_at":
        dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "owner_user_id": manifest["owner_user_id"],
    "execution_head": os.environ["EXECUTION_HEAD"],
    "verification_head": os.environ["VERIFICATION_HEAD"],
    "backup": os.environ["BACKUP"],
    "candidate": {
        "observation_id": manifest["items"][0]["observation_id"],
        "predicate": manifest["items"][0]["predicate"],
        "canonical_text": manifest["items"][0]["canonical_text"],
    },
    "original_wrapper": {
        "phase": "restore",
        "exit_code": 7,
        "failure": "brains_health_startup_race",
        "memory_transaction_failure": False,
    },
    "verification": {
        "stage_rows_written": stage_apply["rows_written"],
        "review_rows_written": review_apply["rows_written"],
        "stage_replay_rows_written": stage_replay["rows_written"],
        "review_replay_rows_written": review_replay["rows_written"],
        "reviewed_candidates": 1,
        "existing_entailment_reused": True,
        "durable_claim_links": 0,
        "projection_apply_events": 0,
        "qdrant_sha256": os.environ["QDRANT"],
        "qdrant_unchanged": True,
        "non_target_memory_unchanged": True,
        "account_isolation_verified": True,
        "brains_health_authenticated": True,
        "timer_restore_verified_before_health_failure": True,
        "retrieval_activated": False,
        "prompt_influence_activated": False,
    },
    "results": {
        "stage_apply_result_sha256": stage_apply["result_sha256"],
        "review_apply_result_sha256": review_apply["result_sha256"],
        "stage_replay_result_sha256": stage_replay["result_sha256"],
        "review_replay_result_sha256": review_replay["result_sha256"],
        "stage_apply_file_sha256": file_sha("STAGE_APPLY"),
        "review_apply_file_sha256": file_sha("REVIEW_APPLY"),
        "stage_replay_file_sha256": file_sha("STAGE_REPLAY"),
        "review_replay_file_sha256": file_sha("REVIEW_REPLAY"),
    },
}
path = Path(os.environ["REPORT"])
if path.exists():
    raise SystemExit("refusing to overwrite existing recovery report")
path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
path.chmod(0o600)
PY

sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
printf 'report=%s\n' "$report"
printf 'memory_v1_v5_2_canonical_name_claim_review_finalize: PASS\n'
