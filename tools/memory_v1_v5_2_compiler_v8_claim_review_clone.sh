#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores production into a disposable clone, installs the
# temporal claim-text compatibility migration, stages exactly four compiler-v8
# claim candidates, records four reviews, proves replay/isolation, then removes
# the clone. Production data and Qdrant remain read-only.

if [[ "$#" -ne 1 ]]; then
  echo 'usage: ..._claim_review_clone.sh ARTIFACT_DIR' >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel)
artifact_dir=$(realpath -m "$1")
review_root=/home/ubuntu/memory-v1-reviews
target_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
stage_runner=scripts/memory_v1_v5_2_compiler_v8_claim_stage.py
review_manifest_runner=scripts/memory_v1_v5_2_compiler_v8_claim_review_manifest.py
review_runner=scripts/memory_v1_v5_2_compiler_v8_claim_review_batch.py
migration=ops/sql/20260725_memory_v1_v5_2_temporal_claim_projection.sql
security_test=tests/memory_v1_v5_2_temporal_claim_projection_security.sql
port=${MEMORY_V1_V5_2_COMPILER_V8_CLAIM_CLONE_PORT:-55496}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v52compilerv8claimclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
backup=$(mktemp /tmp/memory-v1-v5-2-compiler-v8-claim.XXXXXX.dump)
role_sql=$(mktemp /tmp/memory-v1-v5-2-compiler-v8-claim-roles.XXXXXX.sql)
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"
head=$(git -C "$repo_root" rev-parse HEAD)

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
report="$artifact_dir/clone-report.json"

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$role_sql"
}
trap cleanup EXIT

[[ "$artifact_dir" == "$review_root"/* ]]
[[ ! -e "$artifact_dir" ]]
mkdir -m 0700 "$artifact_dir"
chmod 0600 "$backup" "$role_sql"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory "$@"
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

production_scalar() {
  docker exec brains-postgres-1 psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
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

docker exec brains-postgres-1 pg_dump -U sage -d memory -Fc >"$backup"
[[ -s "$backup" ]]
docker exec brains-postgres-1 psql -X -A -t -U sage -d memory -c "
  SELECT format(
    'CREATE ROLE %I %s %s NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;',
    rolname,
    CASE WHEN rolcanlogin THEN 'LOGIN' ELSE 'NOLOGIN' END,
    CASE WHEN rolinherit THEN 'INHERIT' ELSE 'NOINHERIT' END
  )
  FROM pg_roles
  WHERE rolname NOT LIKE 'pg\\_%' ESCAPE '\\'
    AND rolname NOT IN ('sage','postgres')
  ORDER BY rolname" >"$role_sql"
[[ -s "$role_sql" ]]

"${compose[@]}" up -d --wait postgres
run_sql <"$role_sql"
printf '%s\n' "ALTER ROLE brains_app PASSWORD 'clone_only_brains_password';" \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists <"$backup"
run_sql <"$migration"
run_sql <"$migration"
run_sql <"$security_test"

PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python \
  "$repo_root/tests/test_memory_v1_v5_2_compiler_v8_claim_stage.py"

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$stage_runner" manifest \
  --owner "$target_owner" \
  --observation a0ea633d-96df-4ad8-a0c1-b3f4f84e30cc \
  --observation c8ce8cd0-e058-4181-ae94-fd6fb1e7c6eb \
  --observation 70d55f38-1e33-418f-8ec6-6bfd2051f4e6 \
  --observation bbd94cc7-e9d5-429f-8af1-1a029b119db0 \
  --required-head "$head" --output "$manifest"
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python \
  "$repo_root/$stage_runner" authorize \
  --manifest "$manifest" --output "$authorization"

before_requests=$(scalar "SELECT count(*) FROM memory.relational_operation_request WHERE owner_user_id='$target_owner'")
before_entailments=$(scalar "SELECT count(*) FROM memory.observation_entailment_v5 WHERE owner_user_id='$target_owner'")
before_plans=$(scalar "SELECT count(*) FROM memory.projection_plan WHERE owner_user_id='$target_owner'")
before_items=$(scalar "SELECT count(*) FROM memory.projection_plan_item WHERE owner_user_id='$target_owner'")
before_payloads=$(scalar "SELECT count(*) FROM memory.projection_claim_payload WHERE owner_user_id='$target_owner'")
before_links=$(scalar "SELECT count(*) FROM memory.projection_plan_observation WHERE owner_user_id='$target_owner'")
before_reviews=$(scalar "SELECT count(*) FROM memory.projection_review WHERE owner_user_id='$target_owner'")
before_claims=$(scalar "SELECT count(*) FROM memory.claim WHERE owner_user_id='$target_owner'")
before_revisions=$(scalar "SELECT count(*) FROM memory.claim_revision WHERE owner_user_id='$target_owner'")

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$stage_runner" cross-owner \
  --manifest "$manifest" --other-owner "$other_owner" --output "$cross_result"
[[ "$(jq -er '.cross_owner_rejected' "$cross_result")" == true ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_COMPILER_V8_CLAIM_STAGE_APPLY=authorized \
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$stage_runner" apply \
  --manifest "$manifest" --authorization "$authorization" \
  --confirm STAGE_EXACT_FOUR_COMPILER_V8_CLAIM_CANDIDATES_ONLY \
  --output "$stage_apply"
[[ "$(jq -er '.rows_written' "$stage_apply")" == 24 ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_COMPILER_V8_CLAIM_STAGE_APPLY=authorized \
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$stage_runner" replay \
  --manifest "$manifest" --authorization "$authorization" \
  --confirm STAGE_EXACT_FOUR_COMPILER_V8_CLAIM_CANDIDATES_ONLY \
  --output "$stage_replay"
[[ "$(jq -er '.rows_written' "$stage_replay")" == 0 ]]

MANIFEST="$manifest" OUTPUT="$decisions" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python - <<'PY'
import json
import os
from pathlib import Path
from scripts.memory_v1_projection_v5_contract_test import sha256

stage = json.loads(Path(os.environ["MANIFEST"]).read_text())
reasons = {
    "occupation.works_as": (
        "Reviewed atomic historical occupation observation with an ended "
        "state-validity interval.",
        ["bound_entities_reviewed", "historical_interval_reviewed"],
    ),
    "relationship.caregiver_for": (
        "Reviewed atomic current caregiving relationship with explicit-recall-only policy.",
        ["bound_entities_reviewed", "current_relationship_reviewed"],
    ),
    "relationship.spouse_of": (
        "Reviewed atomic current spouse relationship.",
        ["bound_entities_reviewed", "current_relationship_reviewed"],
    ),
}
value = {
    "contract_version": "memory_v1_v5_2_compiler_v8_claim_review_decisions_v1",
    "owner_user_id": stage["owner_user_id"],
    "evidence_ids": stage["evidence_ids"],
    "decisions": [],
}
for item in stage["items"]:
    reason, codes = reasons[item["predicate"]]
    value["decisions"].append({
        "observation_id": item["observation_id"],
        "decision": "authorized",
        "reason": reason,
        "reason_codes": codes,
    })
value["decisions_sha256"] = sha256(value)
path = Path(os.environ["OUTPUT"])
path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
path.chmod(0o600)
PY

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root/scripts:$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$review_manifest_runner" \
  --owner "$target_owner" --required-head "$head" \
  --stage-manifest "$manifest" --decisions "$decisions" \
  --output "$review_manifest"

MEMORY_V1_REQUIRED_HEAD="$head" \
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root/scripts:$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$review_runner" \
  --mode preflight --manifest "$review_manifest" --output "$review_preflight"
[[ "$(jq -er '.rows_written' "$review_preflight")" == 0 ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_COMPILER_V8_CLAIM_REVIEW_APPLY=authorized \
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root/scripts:$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$review_runner" \
  --mode apply --manifest "$review_manifest" --output "$review_apply"
[[ "$(jq -er '.rows_written' "$review_apply")" == 4 ]]
[[ "$(jq -er '.decision_counts.authorized' "$review_apply")" == 4 ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_COMPILER_V8_CLAIM_REVIEW_APPLY=authorized \
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root/scripts:$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$review_runner" \
  --mode replay --manifest "$review_manifest" --output "$review_replay"
[[ "$(jq -er '.rows_written' "$review_replay")" == 0 ]]

[[ "$(scalar "SELECT count(*) FROM memory.relational_operation_request WHERE owner_user_id='$target_owner'")" == "$((before_requests + 4))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.observation_entailment_v5 WHERE owner_user_id='$target_owner'")" == "$((before_entailments + 4))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_plan WHERE owner_user_id='$target_owner'")" == "$((before_plans + 4))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_plan_item WHERE owner_user_id='$target_owner'")" == "$((before_items + 4))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_claim_payload WHERE owner_user_id='$target_owner'")" == "$((before_payloads + 4))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_plan_observation WHERE owner_user_id='$target_owner'")" == "$((before_links + 4))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_review WHERE owner_user_id='$target_owner'")" == "$((before_reviews + 4))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.claim WHERE owner_user_id='$target_owner'")" == "$before_claims" ]]
[[ "$(scalar "SELECT count(*) FROM memory.claim_revision WHERE owner_user_id='$target_owner'")" == "$before_revisions" ]]

[[ "$(scalar "SELECT count(*) FROM memory.projection_claim_payload p JOIN memory.projection_plan_item i USING (owner_user_id,plan_id,projection_ref) JOIN memory.projection_plan_observation l USING (owner_user_id,plan_id) WHERE p.owner_user_id='$target_owner' AND l.observation_id IN ('a0ea633d-96df-4ad8-a0c1-b3f4f84e30cc','c8ce8cd0-e058-4181-ae94-fd6fb1e7c6eb','70d55f38-1e33-418f-8ec6-6bfd2051f4e6','bbd94cc7-e9d5-429f-8af1-1a029b119db0') AND i.review_state='authorized'")" == 4 ]]
[[ "$(scalar "SELECT count(*) FROM memory.claim_observation WHERE owner_user_id='$target_owner' AND observation_id IN ('a0ea633d-96df-4ad8-a0c1-b3f4f84e30cc','c8ce8cd0-e058-4181-ae94-fd6fb1e7c6eb','70d55f38-1e33-418f-8ec6-6bfd2051f4e6','bbd94cc7-e9d5-429f-8af1-1a029b119db0')")" == 0 ]]

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
value = {
    "contract_version": "memory_v1_v5_2_compiler_v8_claim_review_clone_report_v1",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "owner_user_id": stage["owner_user_id"],
    "stage_manifest_sha256": stage["manifest_sha256"],
    "review_manifest_sha256": review["manifest_sha256"],
    "candidates": [
        {
            "observation_id": item["observation_id"],
            "predicate": item["predicate"],
            "state_relation": item["state_relation"],
            "canonical_text": item["canonical_text"],
        }
        for item in stage["items"]
    ],
    "verification": {
        "stage_rows_written": 24,
        "review_rows_written": 4,
        "stage_replay_zero_write": True,
        "review_replay_zero_write": True,
        "cross_owner_rejected": True,
        "production_unchanged": True,
        "qdrant_sha256": os.environ["QDRANT"],
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
printf 'memory_v1_v5_2_compiler_v8_claim_review_clone: PASS\n'
