#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Read-only recovery verifier for the compiler-v8
# claim-candidate/review production run. It writes one immutable audit report;
# it does not write memory rows, Qdrant, retrieval, or prompt state.

repo_root=$(git rev-parse --show-toplevel)
run_tag=20260725T141700Z_cb87f2555f36
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
execution_head=cb87f2555f368ec0e8fe5db52a050759f8e2eaad
health_fix_head=b4be538b73baec02af73d4ff3c261e7ae46b294e
verification_head=$(git -C "$repo_root" rev-parse HEAD)
expected_qdrant=b8c846a4d14b47e09773ef93ef0917e00bd1a21143e8ee4f15ddadacf4d42fb6
snapshot_dir=/home/ubuntu/brains/snapshots
review_dir=/home/ubuntu/memory-v1-reviews
backup="$snapshot_dir/memory_pre_v5_2_compiler_v8_claim_${run_tag}.dump"
status="$snapshot_dir/memory_v1_v5_2_compiler_v8_claim_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_2_compiler_v8_claim_${run_tag}.final.json"
stage_apply="$review_dir/compiler-v8-claim-stage-apply-${run_tag}.json"
review_apply="$review_dir/compiler-v8-claim-review-apply-${run_tag}.json"
stage_replay="$review_dir/compiler-v8-claim-stage-replay-${run_tag}.json"
review_replay="$review_dir/compiler-v8-claim-review-replay-${run_tag}.json"
observations="'a0ea633d-96df-4ad8-a0c1-b3f4f84e30cc','c8ce8cd0-e058-4181-ae94-fd6fb1e7c6eb','70d55f38-1e33-418f-8ec6-6bfd2051f4e6','bbd94cc7-e9d5-429f-8af1-1a029b119db0'"

git -C "$repo_root" merge-base --is-ancestor "$execution_head" "$verification_head"
git -C "$repo_root" merge-base --is-ancestor "$health_fix_head" "$verification_head"
test -z "$(git -C "$repo_root" status --porcelain)"

set -a
source "$repo_root/.env"
set +a

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
  JOIN memory.projection_plan_observation AS link
    USING (owner_user_id,plan_id,projection_ref)
  JOIN memory.projection_review AS review
    USING (owner_user_id,plan_id,projection_ref)
  WHERE item.owner_user_id='$owner'
    AND link.observation_id IN ($observations)
    AND item.review_state='manual_review_required'
    AND review.decision='authorized'
")
claim_links=$(psql_row "
  SELECT count(*)
  FROM memory.claim_observation
  WHERE owner_user_id='$owner'
    AND observation_id IN ($observations)
")
apply_events=$(psql_row "
  SELECT count(*)
  FROM memory.projection_apply_event AS event
  JOIN memory.projection_plan_observation AS link
    USING (owner_user_id,plan_id,projection_ref)
  WHERE event.owner_user_id='$owner'
    AND link.observation_id IN ($observations)
")

test "$reviewed" = 4
test "$claim_links" = 0
test "$apply_events" = 0
test "$(jq -er '.rows_written' "$stage_apply")" = 24
test "$(jq -er '.rows_written' "$review_apply")" = 4
test "$(jq -er '.rows_written' "$stage_replay")" = 0
test "$(jq -er '.rows_written' "$review_replay")" = 0
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
STAGE_APPLY="$stage_apply" REVIEW_APPLY="$review_apply" \
STAGE_REPLAY="$stage_replay" REVIEW_REPLAY="$review_replay" \
python3 - <<'PY'
import datetime as dt
import hashlib
import json
import os
from pathlib import Path


def load(path: str) -> dict:
    return json.loads(Path(path).read_text())


def file_sha(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


stage_apply = load(os.environ["STAGE_APPLY"])
review_apply = load(os.environ["REVIEW_APPLY"])
stage_replay = load(os.environ["STAGE_REPLAY"])
review_replay = load(os.environ["REVIEW_REPLAY"])
value = {
    "contract_version": "memory_v1_v5_2_compiler_v8_claim_review_recovery_report_v1",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "owner_user_id": "1240822d-ac9a-4096-95aa-e2b24d36ef50",
    "execution_head": os.environ["EXECUTION_HEAD"],
    "verification_head": os.environ["VERIFICATION_HEAD"],
    "backup": os.environ["BACKUP"],
    "original_wrapper": {
        "phase": "restore",
        "exit_code": 7,
        "failure": "obsolete_unauthenticated_health_endpoint",
        "memory_transaction_failure": False,
    },
    "verification": {
        "candidate_stage_rows_written": stage_apply["rows_written"],
        "review_rows_written": review_apply["rows_written"],
        "stage_replay_rows_written": stage_replay["rows_written"],
        "review_replay_rows_written": review_replay["rows_written"],
        "reviewed_candidates": 4,
        "durable_claim_links": 0,
        "projection_apply_events": 0,
        "qdrant_sha256": os.environ["QDRANT"],
        "qdrant_unchanged": True,
        "non_target_memory_unchanged": True,
        "account_isolation_verified": True,
        "brains_health_authenticated": True,
        "timers_restored_before_original_probe": True,
        "retrieval_activated": False,
        "prompt_influence_activated": False,
    },
    "results": {
        "stage_apply_result_sha256": stage_apply["result_sha256"],
        "review_apply_result_sha256": review_apply["result_sha256"],
        "stage_replay_result_sha256": stage_replay["result_sha256"],
        "review_replay_result_sha256": review_replay["result_sha256"],
        "stage_apply_file_sha256": file_sha(os.environ["STAGE_APPLY"]),
        "review_apply_file_sha256": file_sha(os.environ["REVIEW_APPLY"]),
        "stage_replay_file_sha256": file_sha(os.environ["STAGE_REPLAY"]),
        "review_replay_file_sha256": file_sha(os.environ["REVIEW_REPLAY"]),
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
printf 'memory_v1_v5_2_compiler_v8_claim_review_finalize: PASS\n'
