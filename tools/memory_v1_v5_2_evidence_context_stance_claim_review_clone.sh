#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Stages and reviews one exact attributed stance in a
# disposable production clone. Production Postgres and Qdrant stay read-only.

[[ "$EUID" -eq 0 ]]
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[[ -z "$(GIT_OPTIONAL_LOCKS=0 git -C "$repo_root" status --porcelain)" ]]
head=$(git -C "$repo_root" rev-parse HEAD)
container=brains-postgres-1
source_db=memory
clone_db="memory_v5_2_evidence_context_stance_$(date -u +%Y%m%d%H%M%S)_$$"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
evidence=049205b4-9a6c-5e1a-bb8f-2ab9f05f8964
observation=87ce1a11-01ae-4d6f-80ea-8e62b5b43cff
stage_runner=scripts/memory_v1_v5_2_evidence_context_stance_claim_stage.py
stage_test=tests/test_memory_v1_v5_2_evidence_context_stance_claim_stage.py
review_manifest_runner=scripts/memory_v1_v5_2_compiler_v8_claim_review_manifest.py
review_runner=scripts/memory_v1_v5_2_evidence_context_stance_claim_review_batch.py
python_bin=/opt/chat-memory/venv/bin/python
review_root=/home/ubuntu/memory-v1-reviews
artifact_dir="$review_root/evidence-context-stance-claim-clone-$(date -u +%Y%m%dT%H%M%SZ)-${head:0:12}"

set -a
source /opt/chat-memory/.env
set +a
[[ -n "${POSTGRES_DSN:-}" ]]
for path in "$stage_runner" "$stage_test" "$review_manifest_runner" "$review_runner"; do
  [[ -f "$repo_root/$path" ]]
done
mkdir -m 0700 "$artifact_dir"

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists --force "$clone_db" \
    >/dev/null 2>&1 || true
}
trap cleanup EXIT

production_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$source_db" -c "$1" | sed -n '1p'
}

clone_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$clone_db" -c "$1" | sed -n '1p'
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

production_signature() {
  production_scalar "
    SELECT md5(jsonb_build_object(
      'requests',(SELECT count(*) FROM memory.relational_operation_request),
      'entailments',(SELECT count(*) FROM memory.observation_entailment_v5),
      'plans',(SELECT count(*) FROM memory.projection_plan),
      'items',(SELECT count(*) FROM memory.projection_plan_item),
      'payloads',(SELECT count(*) FROM memory.projection_claim_payload),
      'links',(SELECT count(*) FROM memory.projection_plan_observation),
      'reviews',(SELECT count(*) FROM memory.projection_review),
      'claims',(SELECT count(*) FROM memory.claim),
      'revisions',(SELECT count(*) FROM memory.claim_revision)
    )::text)"
}

qdrant_before=$(qdrant_signature)
production_before=$(production_signature)
production_head_before=$(git -C /opt/chat-memory rev-parse HEAD)

docker exec "$container" createdb -U sage -T template0 "$clone_db"
docker exec "$container" pg_dump -U sage -d "$source_db" -Fc \
  | docker exec -i "$container" pg_restore -U sage -d "$clone_db"

clone_dsn=$(SOURCE_DSN="$POSTGRES_DSN" CLONE_DB="$clone_db" python3 - <<'PY'
import os
from urllib.parse import urlsplit, urlunsplit

value = urlsplit(os.environ["SOURCE_DSN"])
print(urlunsplit((
    value.scheme,
    value.netloc,
    "/" + os.environ["CLONE_DB"],
    value.query,
    value.fragment,
)))
PY
)

PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$repo_root/$stage_test"

before_requests=$(clone_scalar "SELECT count(*) FROM memory.relational_operation_request WHERE owner_user_id='$owner'")
before_entailments=$(clone_scalar "SELECT count(*) FROM memory.observation_entailment_v5 WHERE owner_user_id='$owner'")
before_plans=$(clone_scalar "SELECT count(*) FROM memory.projection_plan WHERE owner_user_id='$owner'")
before_items=$(clone_scalar "SELECT count(*) FROM memory.projection_plan_item WHERE owner_user_id='$owner'")
before_payloads=$(clone_scalar "SELECT count(*) FROM memory.projection_claim_payload WHERE owner_user_id='$owner'")
before_links=$(clone_scalar "SELECT count(*) FROM memory.projection_plan_observation WHERE owner_user_id='$owner'")
before_reviews=$(clone_scalar "SELECT count(*) FROM memory.projection_review WHERE owner_user_id='$owner'")
before_claims=$(clone_scalar "SELECT count(*) FROM memory.claim WHERE owner_user_id='$owner'")
before_revisions=$(clone_scalar "SELECT count(*) FROM memory.claim_revision WHERE owner_user_id='$owner'")

manifest="$artifact_dir/stage-manifest.json"
authorization="$artifact_dir/stage-authorization.json"
cross_result="$artifact_dir/stage-cross-owner.json"
stage_apply="$artifact_dir/stage-apply.json"
stage_replay="$artifact_dir/stage-replay.json"
decisions="$artifact_dir/review-decisions.json"
review_manifest="$artifact_dir/review-manifest.json"
review_preflight="$artifact_dir/review-preflight.json"
review_apply="$artifact_dir/review-apply.json"
review_replay="$artifact_dir/review-replay.json"
report="$artifact_dir/report.json"

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$repo_root/$stage_runner" manifest \
  --owner "$owner" --observation "$observation" \
  --required-head "$head" --output "$manifest"
PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$repo_root/$stage_runner" authorize \
  --manifest "$manifest" --output "$authorization"

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$repo_root/$stage_runner" cross-owner \
  --manifest "$manifest" --other-owner "$other_owner" --output "$cross_result"
[[ "$(jq -er '.cross_owner_rejected' "$cross_result")" == true ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_EVIDENCE_CONTEXT_STANCE_CLAIM_STAGE_APPLY=authorized \
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$repo_root/$stage_runner" apply \
  --manifest "$manifest" --authorization "$authorization" \
  --confirm STAGE_EXACT_ONE_EVIDENCE_CONTEXT_STANCE_CANDIDATE_ONLY \
  --output "$stage_apply"
[[ "$(jq -er '.rows_written' "$stage_apply")" == 6 ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_EVIDENCE_CONTEXT_STANCE_CLAIM_STAGE_APPLY=authorized \
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$repo_root/$stage_runner" replay \
  --manifest "$manifest" --authorization "$authorization" \
  --confirm STAGE_EXACT_ONE_EVIDENCE_CONTEXT_STANCE_CANDIDATE_ONLY \
  --output "$stage_replay"
[[ "$(jq -er '.rows_written' "$stage_replay")" == 0 ]]

MANIFEST="$manifest" OUTPUT="$decisions" PYTHONPATH="$repo_root" \
  "$python_bin" - <<'PY'
import json
import os
from pathlib import Path
from scripts.memory_v1_projection_v5_contract_test import sha256

stage = json.loads(Path(os.environ["MANIFEST"]).read_text())
value = {
    "contract_version": "memory_v1_v5_2_compiler_v8_claim_review_decisions_v1",
    "owner_user_id": stage["owner_user_id"],
    "evidence_ids": stage["evidence_ids"],
    "decisions": [{
        "observation_id": stage["items"][0]["observation_id"],
        "decision": "authorized",
        "reason": (
            "Direct owner-attributed reported stance. Preserve it as the "
            "user's stated belief, not as an externally established fact."
        ),
        "reason_codes": [
            "attributed_reported_stance",
            "belief_not_external_fact",
            "bound_self_entity",
        ],
    }],
}
value["decisions_sha256"] = sha256(value)
path = Path(os.environ["OUTPUT"])
path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
path.chmod(0o600)
PY

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$repo_root/$review_manifest_runner" \
  --owner "$owner" --required-head "$head" \
  --stage-manifest "$manifest" --decisions "$decisions" \
  --output "$review_manifest"

MEMORY_V1_REQUIRED_HEAD="$head" \
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$repo_root/$review_runner" \
  --mode preflight --manifest "$review_manifest" --output "$review_preflight"
[[ "$(jq -er '.rows_written' "$review_preflight")" == 0 ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_EVIDENCE_CONTEXT_STANCE_CLAIM_REVIEW_APPLY=authorized \
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$repo_root/$review_runner" \
  --mode apply --manifest "$review_manifest" --output "$review_apply"
[[ "$(jq -er '.rows_written' "$review_apply")" == 1 ]]
[[ "$(jq -er '.decision_counts.authorized' "$review_apply")" == 1 ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_EVIDENCE_CONTEXT_STANCE_CLAIM_REVIEW_APPLY=authorized \
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$repo_root/$review_runner" \
  --mode replay --manifest "$review_manifest" --output "$review_replay"
[[ "$(jq -er '.rows_written' "$review_replay")" == 0 ]]

[[ "$(clone_scalar "SELECT count(*) FROM memory.relational_operation_request WHERE owner_user_id='$owner'")" == "$((before_requests + 1))" ]]
[[ "$(clone_scalar "SELECT count(*) FROM memory.observation_entailment_v5 WHERE owner_user_id='$owner'")" == "$((before_entailments + 1))" ]]
[[ "$(clone_scalar "SELECT count(*) FROM memory.projection_plan WHERE owner_user_id='$owner'")" == "$((before_plans + 1))" ]]
[[ "$(clone_scalar "SELECT count(*) FROM memory.projection_plan_item WHERE owner_user_id='$owner'")" == "$((before_items + 1))" ]]
[[ "$(clone_scalar "SELECT count(*) FROM memory.projection_claim_payload WHERE owner_user_id='$owner'")" == "$((before_payloads + 1))" ]]
[[ "$(clone_scalar "SELECT count(*) FROM memory.projection_plan_observation WHERE owner_user_id='$owner'")" == "$((before_links + 1))" ]]
[[ "$(clone_scalar "SELECT count(*) FROM memory.projection_review WHERE owner_user_id='$owner'")" == "$((before_reviews + 1))" ]]
[[ "$(clone_scalar "SELECT count(*) FROM memory.claim WHERE owner_user_id='$owner'")" == "$before_claims" ]]
[[ "$(clone_scalar "SELECT count(*) FROM memory.claim_revision WHERE owner_user_id='$owner'")" == "$before_revisions" ]]

[[ "$(clone_scalar "
  SELECT count(*)
  FROM memory.projection_claim_payload AS payload
  JOIN memory.projection_plan_item AS item
    ON item.owner_user_id=payload.owner_user_id
   AND item.plan_id=payload.plan_id
   AND item.projection_ref=payload.projection_ref
  JOIN memory.projection_plan_observation AS link
    ON link.owner_user_id=item.owner_user_id
   AND link.plan_id=item.plan_id
   AND link.projection_ref=item.projection_ref
  JOIN memory.projection_review AS review
    ON review.owner_user_id=item.owner_user_id
   AND review.plan_id=item.plan_id
   AND review.projection_ref=item.projection_ref
  WHERE payload.owner_user_id='$owner'
    AND link.observation_id='$observation'::uuid
    AND payload.claim_class='reported_stance'
    AND payload.canonical_text='The user reports this position: \"Fractal Monism will help people in life.\"'
    AND payload.surface_policy='relevant_recall_or_explicit_recall'
    AND item.review_state='manual_review_required'
    AND review.decision='authorized'")" == 1 ]]
[[ "$(clone_scalar "SELECT count(*) FROM memory.claim_observation WHERE owner_user_id='$owner' AND observation_id='$observation'")" == 0 ]]

[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(production_signature)" == "$production_before" ]]
[[ "$(git -C /opt/chat-memory rev-parse HEAD)" == "$production_head_before" ]]

REPORT="$report" HEAD="$head" MANIFEST="$manifest" REVIEW="$review_manifest" \
QDRANT="$qdrant_before" python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

stage = json.loads(Path(os.environ["MANIFEST"]).read_text())
review = json.loads(Path(os.environ["REVIEW"]).read_text())
item = stage["items"][0]
value = {
    "contract_version": "memory_v1_v5_2_evidence_context_stance_claim_review_clone_report_v1",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "owner_user_id": stage["owner_user_id"],
    "evidence_id": item["evidence_id"],
    "observation_id": item["observation_id"],
    "predicate": item["predicate"],
    "canonical_text": item["canonical_text"],
    "stage_manifest_sha256": stage["manifest_sha256"],
    "review_manifest_sha256": review["manifest_sha256"],
    "verification": {
        "candidate_rows_written": 6,
        "review_rows_written": 1,
        "stage_replay_zero_write": True,
        "review_replay_zero_write": True,
        "cross_owner_rejected": True,
        "production_unchanged": True,
        "qdrant_sha256": os.environ["QDRANT"],
        "qdrant_unchanged": True,
        "claims_written": 0,
        "retrieval_activated": False,
        "prompt_influence_activated": False,
    },
}
path = Path(os.environ["REPORT"])
path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
path.chmod(0o600)
PY

printf 'report=%s\n' "$report"
printf 'memory_v1_v5_2_evidence_context_stance_claim_review_clone: PASS\n'
