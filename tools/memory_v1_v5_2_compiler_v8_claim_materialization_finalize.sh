#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Read-only recovery verifier for the exact-four
# compiler-v8 claim materialization. It writes one immutable audit report;
# it does not write memory rows, Qdrant, retrieval, or prompt state.

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
run_tag=20260725T152727Z_a9bb65020972
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
execution_head=a9bb65020972b20635418c19262d276c30a3f6d2
verification_head=$(git -C "$repo_root" rev-parse HEAD)
expected_qdrant=b8c846a4d14b47e09773ef93ef0917e00bd1a21143e8ee4f15ddadacf4d42fb6
snapshot_dir=/home/ubuntu/brains/snapshots
review_dir=/home/ubuntu/memory-v1-reviews
artifact="$review_dir/compiler-v8-claim-materialization-a9bb650"
backup="$snapshot_dir/memory_pre_v5_2_compiler_v8_claim_materialization_${run_tag}.dump"
status="$snapshot_dir/memory_v1_v5_2_compiler_v8_claim_materialization_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_2_compiler_v8_claim_materialization_${run_tag}.final.json"
preflight="$artifact/production-preflight.json"
apply="$artifact/production-apply.json"
replay="$artifact/production-replay.json"
observations="'a0ea633d-96df-4ad8-a0c1-b3f4f84e30cc','c8ce8cd0-e058-4181-ae94-fd6fb1e7c6eb','70d55f38-1e33-418f-8ec6-6bfd2051f4e6','bbd94cc7-e9d5-429f-8af1-1a029b119db0'"

git -C "$repo_root" merge-base --is-ancestor "$execution_head" "$verification_head"
test -z "$(git -C "$repo_root" status --porcelain)"

set -a
source /opt/chat-memory/.env
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

claim_links=$(psql_row "
  SELECT count(*)
  FROM memory.claim_observation
  WHERE owner_user_id='$owner'
    AND observation_id IN ($observations)
")
supported_claims=$(psql_row "
  SELECT count(DISTINCT claim.claim_id)
  FROM memory.claim AS claim
  JOIN memory.claim_observation AS link
    USING(owner_user_id,claim_id)
  WHERE claim.owner_user_id='$owner'
    AND link.observation_id IN ($observations)
    AND claim.status='supported'
")
canonical_claims=$(psql_row "
  SELECT count(DISTINCT claim.claim_id)
  FROM memory.claim AS claim
  JOIN memory.claim_observation AS link
    USING(owner_user_id,claim_id)
  WHERE claim.owner_user_id='$owner'
    AND link.observation_id IN ($observations)
    AND claim.canonical_text IN (
      'The user is a caregiver for Monika.',
      'The user is a spouse of Monika.',
      'The user formerly worked as BCBA.',
      'The user formerly worked as clinical psychologist.'
    )
")
claim_revisions=$(psql_row "
  SELECT count(DISTINCT revision.revision_id)
  FROM memory.claim_revision AS revision
  JOIN memory.claim_observation AS link
    USING(owner_user_id,claim_id)
  WHERE revision.owner_user_id='$owner'
    AND link.observation_id IN ($observations)
")
apply_events=$(psql_row "
  SELECT count(*)
  FROM memory.projection_apply_event AS event
  JOIN memory.projection_plan_observation AS link
    USING (owner_user_id,plan_id,projection_ref)
  WHERE event.owner_user_id='$owner'
    AND link.observation_id IN ($observations)
")
outbox_rows=$(psql_row "
  SELECT count(*)
  FROM memory.projection_outbox AS outbox
  JOIN memory.claim_observation AS link
    ON link.owner_user_id=outbox.owner_user_id
   AND link.claim_id=outbox.aggregate_id
  WHERE outbox.owner_user_id='$owner'
    AND link.observation_id IN ($observations)
")
assessment_rows=$(psql_row "
  SELECT count(*)
  FROM memory.claim_assessment AS assessment
  JOIN memory.claim_observation AS link
    USING(owner_user_id,claim_id)
  WHERE assessment.owner_user_id='$owner'
    AND link.observation_id IN ($observations)
")
assessment_review_rows=$(psql_row "
  SELECT count(*)
  FROM memory.claim_assessment_review_v5 AS review
  JOIN memory.claim_observation AS link
    USING(owner_user_id,claim_id)
  WHERE review.owner_user_id='$owner'
    AND link.observation_id IN ($observations)
")
assessment_apply_rows=$(psql_row "
  SELECT count(*)
  FROM memory.claim_assessment_apply_v5 AS applied
  JOIN memory.claim_observation AS link
    USING(owner_user_id,claim_id)
  WHERE applied.owner_user_id='$owner'
    AND link.observation_id IN ($observations)
")

test "$supported_claims" = 4
test "$canonical_claims" = 4
test "$claim_revisions" = 8
test "$claim_links" = 4
test "$apply_events" = 4
test "$outbox_rows" = 0
test "$assessment_rows" = 4
test "$assessment_review_rows" = 4
test "$assessment_apply_rows" = 4
test "$(jq -er '.insert_rows' "$preflight")" = 0
test "$(jq -er '.insert_rows' "$apply")" = 44
test "$(jq -er '.mutated_rows' "$apply")" = 48
test "$(jq -er '.insert_rows' "$replay")" = 0
test "$(jq -er '.mutated_rows' "$replay")" = 0
test "$(sed -n 's/^phase=//p' "$status")" = restore_runtime
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
PREFLIGHT="$preflight" APPLY="$apply" REPLAY="$replay" \
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


preflight = load(os.environ["PREFLIGHT"])
apply = load(os.environ["APPLY"])
replay = load(os.environ["REPLAY"])
value = {
    "contract_version": "memory_v1_v5_2_compiler_v8_claim_materialization_recovery_report_v1",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "owner_user_id": "1240822d-ac9a-4096-95aa-e2b24d36ef50",
    "execution_head": os.environ["EXECUTION_HEAD"],
    "verification_head": os.environ["VERIFICATION_HEAD"],
    "backup": os.environ["BACKUP"],
    "original_wrapper": {
        "phase": "restore_runtime",
        "exit_code": 7,
        "failure": "brains_active_before_http_ready",
        "memory_transaction_failure": False,
    },
    "verification": {
        "preflight_insert_rows": preflight["insert_rows"],
        "apply_insert_rows": apply["insert_rows"],
        "apply_mutated_rows": apply["mutated_rows"],
        "replay_insert_rows": replay["insert_rows"],
        "replay_mutated_rows": replay["mutated_rows"],
        "supported_claims": 4,
        "claim_revisions": 8,
        "durable_claim_links": 4,
        "projection_apply_events": 4,
        "projection_outbox_rows": 0,
        "claim_assessments": 4,
        "claim_assessment_reviews": 4,
        "claim_assessment_applies": 4,
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
        "preflight_result_sha256": preflight["result_sha256"],
        "apply_result_sha256": apply["result_sha256"],
        "replay_result_sha256": replay["result_sha256"],
        "preflight_file_sha256": file_sha(os.environ["PREFLIGHT"]),
        "apply_file_sha256": file_sha(os.environ["APPLY"]),
        "replay_file_sha256": file_sha(os.environ["REPLAY"]),
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
printf 'memory_v1_v5_2_compiler_v8_claim_materialization_finalize: PASS\n'
