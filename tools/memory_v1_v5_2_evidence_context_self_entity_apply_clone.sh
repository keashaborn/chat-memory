#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Applies one exact trusted-self entity resolution and
# observation binding in a disposable production clone.

[[ "$EUID" -eq 0 ]]
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
head=$(git -C "$repo_root" rev-parse HEAD)
container=brains-postgres-1
source_db=memory
clone_db="memory_v5_2_evidence_context_entity_$(date -u +%Y%m%d%H%M%S)_$$"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
evidence=049205b4-9a6c-5e1a-bb8f-2ab9f05f8964
resolution=96d5baad-cbe7-4480-b530-b6898280cab1
observation=87ce1a11-01ae-4d6f-80ea-8e62b5b43cff
entity=35029129-27bd-457b-8cb5-82dd37ba32ba
runner=scripts/memory_v1_v5_2_entity_resolution_batch.py
fixture=tests/memory_v1_v5_2_entity_resolution_batch_fixture.py
unit_test=tests/test_memory_v1_v5_2_entity_resolution_batch.py
manifest_source=manifests/memory_v1_v5_2_evidence_context_self_entity_apply_20260726.json
python_bin=/opt/chat-memory/venv/bin/python
review_root=/home/ubuntu/memory-v1-reviews
artifact_dir="$review_root/evidence-context-self-entity-clone-$(date -u +%Y%m%dT%H%M%SZ)-${head:0:12}"

set -a
source /opt/chat-memory/.env
set +a
[[ -n "${POSTGRES_DSN:-}" ]]
[[ -x "$python_bin" ]]
for path in "$runner" "$fixture" "$unit_test" "$manifest_source"; do
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
      'reviews',(SELECT count(*) FROM memory.entity_resolution_review),
      'applies',(SELECT count(*) FROM memory.entity_resolution_apply),
      'bindings',(SELECT count(*) FROM memory.observation_entity_binding),
      'entities',(SELECT count(*) FROM memory.entity),
      'claims',(SELECT count(*) FROM memory.claim),
      'projections',(SELECT count(*) FROM memory.projection_plan)
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

PYTHONPATH="$repo_root" "$python_bin" "$repo_root/$unit_test"

before_requests=$(clone_scalar "SELECT count(*) FROM memory.relational_operation_request
  WHERE owner_user_id='$owner'::uuid")
before_reviews=$(clone_scalar "SELECT count(*) FROM memory.entity_resolution_review
  WHERE owner_user_id='$owner'::uuid")
before_applies=$(clone_scalar "SELECT count(*) FROM memory.entity_resolution_apply
  WHERE owner_user_id='$owner'::uuid")
before_bindings=$(clone_scalar "SELECT count(*) FROM memory.observation_entity_binding
  WHERE owner_user_id='$owner'::uuid")
before_entities=$(clone_scalar "SELECT count(*) FROM memory.entity
  WHERE owner_user_id='$owner'::uuid")
before_claims=$(clone_scalar "SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$owner'::uuid")
before_projections=$(clone_scalar "SELECT count(*) FROM memory.projection_plan
  WHERE owner_user_id='$owner'::uuid")

manifest="$artifact_dir/manifest.json"
plan="$artifact_dir/plan.json"
authorization="$artifact_dir/authorization.json"
cross_manifest="$artifact_dir/cross-owner-manifest.json"
report="$artifact_dir/apply-report.json"
final_report="$artifact_dir/report.json"
cp "$repo_root/$manifest_source" "$manifest"
chmod 0600 "$manifest"

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  "$python_bin" "$repo_root/$runner" plan \
  --manifest "$manifest" --review-root "$review_root" --output "$plan"

jq --arg owner "$other_owner" '.owner_user_id=$owner' \
  "$manifest" >"$cross_manifest"
chmod 0600 "$cross_manifest"
if POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  "$python_bin" "$repo_root/$runner" plan \
  --manifest "$cross_manifest" --review-root "$review_root" \
  --output "$artifact_dir/cross-owner-plan.json" >/dev/null 2>&1; then
  echo 'cross-owner evidence-context entity plan unexpectedly passed' >&2
  exit 1
fi

PYTHONPATH="$repo_root" "$python_bin" "$repo_root/$fixture" \
  --plan "$plan" --output "$authorization" --head "$head"

MEMORY_V1_V5_2_ENTITY_RESOLUTION_BATCH_APPLY=authorized \
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  "$python_bin" "$repo_root/$runner" apply \
  --plan "$plan" --authorization "$authorization" \
  --review-root "$review_root" \
  --confirm RECONCILE_REVIEW_AND_APPLY_OWNER_V5_2_ENTITY_RESOLUTIONS_ONLY \
  --output "$report"

[[ "$(jq -er '.database_rows_created' "$report")" == 3 ]]
[[ "$(jq -er '.bindings_created' "$report")" == 1 ]]
[[ "$(jq -er '.item_count' "$report")" == 1 ]]
[[ "$(jq -er '.applied[0].operation' "$report")" == auto_apply ]]
[[ "$(jq -er '.applied[0].applied_entity_id' "$report")" == "$entity" ]]
[[ "$(jq -er '.replayed[0].apply_outcome' "$report")" == replayed ]]
[[ "$(jq -er '.replayed[0].bindings_created' "$report")" == 0 ]]

[[ "$(clone_scalar "SELECT count(*) FROM memory.relational_operation_request
  WHERE owner_user_id='$owner'::uuid")" == "$((before_requests + 1))" ]]
[[ "$(clone_scalar "SELECT count(*) FROM memory.entity_resolution_review
  WHERE owner_user_id='$owner'::uuid")" == "$before_reviews" ]]
[[ "$(clone_scalar "SELECT count(*) FROM memory.entity_resolution_apply
  WHERE owner_user_id='$owner'::uuid")" == "$((before_applies + 1))" ]]
[[ "$(clone_scalar "SELECT count(*) FROM memory.observation_entity_binding
  WHERE owner_user_id='$owner'::uuid")" == "$((before_bindings + 1))" ]]
[[ "$(clone_scalar "SELECT count(*) FROM memory.entity
  WHERE owner_user_id='$owner'::uuid")" == "$before_entities" ]]
[[ "$(clone_scalar "SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$owner'::uuid")" == "$before_claims" ]]
[[ "$(clone_scalar "SELECT count(*) FROM memory.projection_plan
  WHERE owner_user_id='$owner'::uuid")" == "$before_projections" ]]
[[ "$(clone_scalar "SELECT count(*) FROM memory.entity_resolution_apply
  WHERE owner_user_id='$owner'::uuid
    AND resolution_id='$resolution'::uuid
    AND applied_entity_id='$entity'::uuid")" == 1 ]]
[[ "$(clone_scalar "SELECT count(*) FROM memory.observation_entity_binding
  WHERE owner_user_id='$owner'::uuid
    AND observation_id='$observation'::uuid
    AND subject_entity_id='$entity'::uuid
    AND subject_resolution_id='$resolution'::uuid
    AND object_entity_id IS NULL
    AND object_resolution_id IS NULL")" == 1 ]]
[[ "$(clone_scalar "SELECT count(*) FROM memory.claim_observation
  WHERE owner_user_id='$owner'::uuid
    AND observation_id='$observation'::uuid")" == 0 ]]
[[ "$(clone_scalar "SELECT count(*) FROM memory.entity_resolution_plan
  WHERE owner_user_id='$owner'::uuid
    AND evidence_id='$evidence'::uuid
    AND resolution_id='$resolution'::uuid
    AND action='link_existing'
    AND decision_state='auto_link_eligible'
    AND selected_entity_id='$entity'::uuid")" == 1 ]]

[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(production_signature)" == "$production_before" ]]
[[ "$(git -C /opt/chat-memory rev-parse HEAD)" == "$production_head_before" ]]

REPORT="$final_report" APPLY="$report" HEAD="$head" \
QDRANT="$qdrant_before" python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

apply = json.loads(Path(os.environ["APPLY"]).read_text())
value = {
    "contract_version": "memory_v1_v5_2_evidence_context_self_entity_clone_report_v1",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "owner_user_id": apply["owner_user_id"],
    "resolution_id": apply["applied"][0]["resolution_id"],
    "applied_entity_id": apply["applied"][0]["applied_entity_id"],
    "verification": {
        "database_rows_created": 3,
        "bindings_created": 1,
        "zero_write_replay": True,
        "cross_owner_rejected": True,
        "production_unchanged": True,
        "qdrant_sha256": os.environ["QDRANT"],
        "qdrant_unchanged": True,
        "entities_written": 0,
        "reviews_written": 0,
        "claims_written": 0,
        "retrieval_activated": False,
        "prompt_influence_activated": False
    }
}
path = Path(os.environ["REPORT"])
path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
path.chmod(0o600)
PY

printf 'report=%s\n' "$final_report"
printf 'memory_v1_v5_2_evidence_context_self_entity_apply_clone: PASS\n'
