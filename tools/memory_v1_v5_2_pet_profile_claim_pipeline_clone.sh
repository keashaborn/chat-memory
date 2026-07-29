#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores production into a disposable database, stages
# exactly 11 pet-profile claim candidates, records 10 authorized reviews plus
# one temporal-conflict deferral, materializes only the 10 authorized claims,
# proves replay and owner isolation, then removes the clone.

if [[ "$#" -ne 1 ]]; then
  echo 'usage: memory_v1_v5_2_pet_profile_claim_pipeline_clone.sh ARTIFACT_DIR' >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel)
artifact_dir=$(realpath -m "$1")
review_root=/home/ubuntu/memory-v1-reviews
container=brains-postgres-1
source_db=memory
clone_db="memory_pet_profile_claim_$(date -u +%Y%m%d%H%M%S)_$$"
target_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
deferred_observation=bc8866ad-95e8-4413-832e-813f601eece6
env_file=/opt/chat-memory/.env
head=$(git -C "$repo_root" rev-parse HEAD)

stage_runner=scripts/memory_v1_v5_2_pet_profile_claim_stage.py
review_manifest_runner=scripts/memory_v1_v5_2_pet_profile_claim_review_manifest.py
review_runner=scripts/memory_v1_v5_2_pet_profile_claim_review_batch.py
apply_manifest_runner=scripts/memory_v1_v5_2_pet_profile_claim_apply_manifest.py
apply_runner=scripts/memory_v1_v5_claim_projection_apply_batch.py

stage_manifest="$artifact_dir/stage-manifest.json"
stage_authorization="$artifact_dir/stage-authorization.json"
stage_cross_owner="$artifact_dir/stage-cross-owner.json"
stage_apply="$artifact_dir/stage-apply.json"
stage_replay="$artifact_dir/stage-replay.json"
review_decisions="$artifact_dir/review-decisions.json"
review_manifest="$artifact_dir/review-manifest.json"
review_preflight="$artifact_dir/review-preflight.json"
review_apply="$artifact_dir/review-apply.json"
review_replay="$artifact_dir/review-replay.json"
claim_manifest="$artifact_dir/claim-apply-manifest.json"
claim_preflight="$artifact_dir/claim-preflight.json"
claim_apply="$artifact_dir/claim-apply.json"
claim_replay="$artifact_dir/claim-replay.json"
report="$artifact_dir/clone-report.json"

[[ "$artifact_dir" == "$review_root"/* ]]
[[ ! -e "$artifact_dir" ]]
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
mkdir -m 0700 "$artifact_dir"

set -a
source "$env_file"
set +a
[[ -n "${POSTGRES_DSN:-}" ]]

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

production_signature() {
  docker exec "$container" psql -X -A -t -U sage -d "$source_db" -c "
    SELECT md5(jsonb_build_object(
      'requests',(SELECT count(*) FROM memory.relational_operation_request),
      'entailments',(SELECT count(*) FROM memory.observation_entailment_v5),
      'plans',(SELECT count(*) FROM memory.projection_plan),
      'items',(SELECT count(*) FROM memory.projection_plan_item),
      'payloads',(SELECT count(*) FROM memory.projection_claim_payload),
      'links',(SELECT count(*) FROM memory.projection_plan_observation),
      'reviews',(SELECT count(*) FROM memory.projection_review),
      'claims',(SELECT count(*) FROM memory.claim),
      'revisions',(SELECT count(*) FROM memory.claim_revision),
      'claim_links',(SELECT count(*) FROM memory.claim_observation)
    )::text)"
}

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists --force "$clone_db" \
    >/dev/null 2>&1 || true
}
trap cleanup EXIT

qdrant_before=$(qdrant_signature)
production_before=$(production_signature)
production_head_before=$(git -C /opt/chat-memory rev-parse HEAD)

docker exec "$container" createdb -U sage -T template0 "$clone_db"
docker exec "$container" pg_dump -U sage -d "$source_db" -Fc \
  | docker exec -i "$container" pg_restore -U sage -d "$clone_db"

clone_dsn=$(SOURCE_DSN="$POSTGRES_DSN" CLONE_DB="$clone_db" python3 - <<'PY'
import os
from urllib.parse import urlsplit, urlunsplit

source = urlsplit(os.environ["SOURCE_DSN"])
print(
    urlunsplit(
        (
            source.scheme,
            source.netloc,
            "/" + os.environ["CLONE_DB"],
            source.query,
            source.fragment,
        )
    )
)
PY
)

scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$clone_db" -c "$1"
}

before_requests=$(scalar "SELECT count(*) FROM memory.relational_operation_request WHERE owner_user_id='$target_owner'")
before_entailments=$(scalar "SELECT count(*) FROM memory.observation_entailment_v5 WHERE owner_user_id='$target_owner'")
before_plans=$(scalar "SELECT count(*) FROM memory.projection_plan WHERE owner_user_id='$target_owner'")
before_items=$(scalar "SELECT count(*) FROM memory.projection_plan_item WHERE owner_user_id='$target_owner'")
before_payloads=$(scalar "SELECT count(*) FROM memory.projection_claim_payload WHERE owner_user_id='$target_owner'")
before_plan_links=$(scalar "SELECT count(*) FROM memory.projection_plan_observation WHERE owner_user_id='$target_owner'")
before_reviews=$(scalar "SELECT count(*) FROM memory.projection_review WHERE owner_user_id='$target_owner'")
before_claims=$(scalar "SELECT count(*) FROM memory.claim WHERE owner_user_id='$target_owner'")
before_revisions=$(scalar "SELECT count(*) FROM memory.claim_revision WHERE owner_user_id='$target_owner'")
before_claim_links=$(scalar "SELECT count(*) FROM memory.claim_observation WHERE owner_user_id='$target_owner'")

observations=(
  2e668700-7ae6-4752-ab6a-423e6c6a8c6d
  eed6cae7-33c9-43e9-8736-98861a057fa5
  e5939e3f-b732-4f72-b7c4-b60ee7c55244
  37c05103-3e18-4444-9722-4ab384896aa7
  978c1972-82d5-4d19-a32f-b45c7f4cfbaa
  cb711085-b49b-4b9c-be3b-c34d2d8456da
  6db9a4fd-e109-48ed-8d95-c97eef75382c
  8c4476ae-b917-4678-8d63-f4949982e196
  6151f123-d05f-404d-b3f6-d7079de6b4e6
  bc8866ad-95e8-4413-832e-813f601eece6
  82c87916-a90c-4af8-b4cb-fbd2981f9f96
)
observation_args=()
for observation in "${observations[@]}"; do
  observation_args+=(--observation "$observation")
done

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root:$repo_root/scripts" \
  /opt/chat-memory/venv/bin/python "$repo_root/$stage_runner" manifest \
  --owner "$target_owner" "${observation_args[@]}" \
  --required-head "$head" --output "$stage_manifest"

PYTHONPATH="$repo_root:$repo_root/scripts" \
  /opt/chat-memory/venv/bin/python "$repo_root/$stage_runner" authorize \
  --manifest "$stage_manifest" --output "$stage_authorization"

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root:$repo_root/scripts" \
  /opt/chat-memory/venv/bin/python "$repo_root/$stage_runner" cross-owner \
  --manifest "$stage_manifest" --other-owner "$other_owner" \
  --output "$stage_cross_owner"
[[ "$(jq -er '.cross_owner_rejected' "$stage_cross_owner")" == true ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_PET_PROFILE_CLAIM_STAGE_APPLY=authorized \
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root:$repo_root/scripts" \
  /opt/chat-memory/venv/bin/python "$repo_root/$stage_runner" apply \
  --manifest "$stage_manifest" --authorization "$stage_authorization" \
  --confirm STAGE_EXACT_ELEVEN_PET_PROFILE_CLAIM_CANDIDATES_ONLY \
  --output "$stage_apply"
[[ "$(jq -er '.rows_written' "$stage_apply")" == 66 ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_PET_PROFILE_CLAIM_STAGE_APPLY=authorized \
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root:$repo_root/scripts" \
  /opt/chat-memory/venv/bin/python "$repo_root/$stage_runner" replay \
  --manifest "$stage_manifest" --authorization "$stage_authorization" \
  --confirm STAGE_EXACT_ELEVEN_PET_PROFILE_CLAIM_CANDIDATES_ONLY \
  --output "$stage_replay"
[[ "$(jq -er '.rows_written' "$stage_replay")" == 0 ]]

MANIFEST="$stage_manifest" OUTPUT="$review_decisions" \
DEFERRED_OBSERVATION="$deferred_observation" \
PYTHONPATH="$repo_root:$repo_root/scripts" \
  /opt/chat-memory/venv/bin/python - <<'PY'
import json
import os
from pathlib import Path

from scripts.memory_v1_projection_v5_contract_test import sha256

stage = json.loads(Path(os.environ["MANIFEST"]).read_text())
deferred = os.environ["DEFERRED_OBSERVATION"]
value = {
    "contract_version": "memory_v1_v5_2_pet_profile_claim_review_decisions_v1",
    "owner_user_id": stage["owner_user_id"],
    "evidence_ids": stage["evidence_ids"],
    "decisions": [],
}
for item in stage["items"]:
    if item["observation_id"] == deferred:
        decision = "deferred"
        reason = (
            "A prior supported Neko pet-relationship claim uses current-tense "
            "wording; defer this historical version for controlled reconciliation."
        )
        codes = [
            "historical_interval_reviewed",
            "prior_current_tense_claim_requires_reconciliation",
        ]
    else:
        decision = "authorized"
        reason = (
            "Reviewed atomic pet-profile observation with bound owner-scoped "
            "entities, exact source evidence, and deterministic temporal rendering."
        )
        codes = [
            "bound_entities_reviewed",
            "pet_profile_semantics_reviewed",
            (
                "historical_interval_reviewed"
                if item["state_relation"] == "historical"
                else "observation_time_reviewed"
            ),
        ]
    value["decisions"].append(
        {
            "observation_id": item["observation_id"],
            "decision": decision,
            "reason": reason,
            "reason_codes": codes,
        }
    )
value["decisions_sha256"] = sha256(value)
path = Path(os.environ["OUTPUT"])
path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
path.chmod(0o600)
PY

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root:$repo_root/scripts" \
  /opt/chat-memory/venv/bin/python "$repo_root/$review_manifest_runner" \
  --owner "$target_owner" --required-head "$head" \
  --stage-manifest "$stage_manifest" --decisions "$review_decisions" \
  --output "$review_manifest"

MEMORY_V1_REQUIRED_HEAD="$head" \
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root:$repo_root/scripts" \
  /opt/chat-memory/venv/bin/python "$repo_root/$review_runner" \
  --mode preflight --manifest "$review_manifest" --output "$review_preflight"
[[ "$(jq -er '.rows_written' "$review_preflight")" == 0 ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_PET_PROFILE_CLAIM_REVIEW_APPLY=authorized \
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root:$repo_root/scripts" \
  /opt/chat-memory/venv/bin/python "$repo_root/$review_runner" \
  --mode apply --manifest "$review_manifest" --output "$review_apply"
[[ "$(jq -er '.rows_written' "$review_apply")" == 11 ]]
[[ "$(jq -er '.decision_counts.authorized' "$review_apply")" == 10 ]]
[[ "$(jq -er '.decision_counts.deferred' "$review_apply")" == 1 ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_PET_PROFILE_CLAIM_REVIEW_APPLY=authorized \
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root:$repo_root/scripts" \
  /opt/chat-memory/venv/bin/python "$repo_root/$review_runner" \
  --mode replay --manifest "$review_manifest" --output "$review_replay"
[[ "$(jq -er '.rows_written' "$review_replay")" == 0 ]]

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root:$repo_root/scripts" \
  /opt/chat-memory/venv/bin/python "$repo_root/$apply_manifest_runner" \
  --owner "$target_owner" --required-head "$head" \
  --review-manifest "$review_manifest" --review-result "$review_apply" \
  --output "$claim_manifest"
[[ "$(jq -er '.items | length' "$claim_manifest")" == 10 ]]
[[ "$(jq -er '.defer_projection_outbox' "$claim_manifest")" == true ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root:$repo_root/scripts" \
  /opt/chat-memory/venv/bin/python "$repo_root/$apply_runner" \
  --mode preflight --manifest "$claim_manifest" --output "$claim_preflight"
[[ "$(jq -er '.insert_rows' "$claim_preflight")" == 0 ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_CLAIM_PROJECTION_APPLY_BATCH=authorized \
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root:$repo_root/scripts" \
  /opt/chat-memory/venv/bin/python "$repo_root/$apply_runner" \
  --mode apply --manifest "$claim_manifest" --output "$claim_apply"
[[ "$(jq -er '.insert_rows' "$claim_apply")" == 110 ]]
[[ "$(jq -er '.mutated_rows' "$claim_apply")" == 120 ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root:$repo_root/scripts" \
  /opt/chat-memory/venv/bin/python "$repo_root/$apply_runner" \
  --mode replay --manifest "$claim_manifest" --apply-result "$claim_apply" \
  --output "$claim_replay"
[[ "$(jq -er '.insert_rows' "$claim_replay")" == 0 ]]
[[ "$(jq -er '.mutated_rows' "$claim_replay")" == 0 ]]

[[ "$(scalar "SELECT count(*) FROM memory.relational_operation_request WHERE owner_user_id='$target_owner'")" == "$((before_requests + 31))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.observation_entailment_v5 WHERE owner_user_id='$target_owner'")" == "$((before_entailments + 11))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_plan WHERE owner_user_id='$target_owner'")" == "$((before_plans + 11))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_plan_item WHERE owner_user_id='$target_owner'")" == "$((before_items + 11))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_claim_payload WHERE owner_user_id='$target_owner'")" == "$((before_payloads + 11))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_plan_observation WHERE owner_user_id='$target_owner'")" == "$((before_plan_links + 11))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_review WHERE owner_user_id='$target_owner'")" == "$((before_reviews + 11))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.claim WHERE owner_user_id='$target_owner'")" == "$((before_claims + 10))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.claim_revision WHERE owner_user_id='$target_owner'")" == "$((before_revisions + 20))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.claim_observation WHERE owner_user_id='$target_owner'")" == "$((before_claim_links + 10))" ]]

claim_ids=$(jq -r '[.outcomes[].claim_id] | join(",")' "$claim_apply")
[[ "$(scalar "SELECT count(*) FROM memory.claim WHERE owner_user_id='$target_owner' AND claim_id=ANY(string_to_array('$claim_ids',',')::uuid[]) AND status='supported'")" == 10 ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_outbox WHERE owner_user_id='$target_owner' AND aggregate_id=ANY(string_to_array('$claim_ids',',')::uuid[])")" == 0 ]]
[[ "$(scalar "SELECT count(*) FROM memory.claim_observation WHERE owner_user_id='$target_owner' AND observation_id='$deferred_observation'::uuid")" == 0 ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_review r JOIN memory.projection_plan_observation l USING(owner_user_id,plan_id,projection_ref) WHERE r.owner_user_id='$target_owner' AND l.observation_id='$deferred_observation'::uuid AND r.decision='deferred'")" == 1 ]]
[[ "$(scalar "SELECT count(*) FROM memory.claim WHERE owner_user_id='$target_owner' AND claim_id=ANY(string_to_array('$claim_ids',',')::uuid[]) AND predicate='relationship.has_pet' AND canonical_text LIKE 'The user formerly had a pet named %'")" == 2 ]]
[[ "$(scalar "SELECT count(*) FROM memory.claim WHERE owner_user_id='$target_owner' AND claim_id=ANY(string_to_array('$claim_ids',',')::uuid[]) AND canonical_text LIKE 'The user has a pet named %'")" == 0 ]]

probe_plan=$(jq -er '.items[0].plan_id' "$claim_manifest")
probe_review=$(jq -er '.items[0].review_id' "$claim_manifest")
POSTGRES_DSN="$clone_dsn" psql "$clone_dsn" -X -q -v ON_ERROR_STOP=1 \
  -v probe_plan="$probe_plan" -v probe_review="$probe_review" \
  -v other_owner="$other_owner" <<'SQL'
BEGIN READ ONLY;
SELECT set_config('app.user_id', :'other_owner', true);
SELECT set_config('test.probe_plan', :'probe_plan', true);
SELECT set_config('test.probe_review', :'probe_review', true);
DO $isolation$
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_projection_apply_v5(
      current_setting('test.probe_plan')::uuid,
      'p01',
      current_setting('test.probe_review')::uuid
    );
    RAISE EXCEPTION 'cross-owner projection apply preflight unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN
    NULL;
  END;
END
$isolation$;
ROLLBACK;
SQL

[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(production_signature)" == "$production_before" ]]
[[ "$(git -C /opt/chat-memory rev-parse HEAD)" == "$production_head_before" ]]

REPORT="$report" HEAD="$head" STAGE="$stage_manifest" REVIEW="$review_manifest" \
CLAIM="$claim_manifest" APPLY="$claim_apply" QDRANT="$qdrant_before" \
  python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

stage = json.loads(Path(os.environ["STAGE"]).read_text())
review = json.loads(Path(os.environ["REVIEW"]).read_text())
claim = json.loads(Path(os.environ["CLAIM"]).read_text())
applied = json.loads(Path(os.environ["APPLY"]).read_text())
value = {
    "contract_version": "memory_v1_v5_2_pet_profile_claim_pipeline_clone_report_v1",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "owner_user_id": stage["owner_user_id"],
    "stage_manifest_sha256": stage["manifest_sha256"],
    "review_manifest_sha256": review["manifest_sha256"],
    "claim_manifest_sha256": claim["manifest_sha256"],
    "claim_apply_result_sha256": applied["result_sha256"],
    "candidate_count": 11,
    "authorized_count": 10,
    "deferred_count": 1,
    "supported_claim_count": 10,
    "verification": {
        "stage_rows_written": 66,
        "review_rows_written": 11,
        "claim_insert_rows": 110,
        "claim_mutated_rows": 120,
        "stage_replay_zero_write": True,
        "review_replay_zero_write": True,
        "claim_replay_zero_write": True,
        "cross_owner_rejected": True,
        "production_unchanged": True,
        "qdrant_sha256": os.environ["QDRANT"],
        "qdrant_unchanged": True,
        "retrieval_activated": False,
        "prompt_influence_activated": False,
    },
}
path = Path(os.environ["REPORT"])
path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
path.chmod(0o600)
PY

printf 'report=%s\n' "$report"
printf 'memory_v1_v5_2_pet_profile_claim_pipeline_clone: PASS\n'
