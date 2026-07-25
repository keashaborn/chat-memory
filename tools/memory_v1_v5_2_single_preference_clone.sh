#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Exercises one exact audiobook preference through
# entailment, projection staging, review, and durable preference application
# in a disposable production clone. Production and Qdrant remain read-only.

[[ "$EUID" -eq 0 ]]
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
head=$(git -C "$repo_root" rev-parse HEAD)
container=brains-postgres-1
source_db=memory
clone_db="memory_v5_2_single_preference_$(date -u +%Y%m%d%H%M%S)_$$"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
evidence=405fcdb1-a4d2-53ff-91ad-542b258cea03
observation=d3c936dc-01c2-4288-9050-b709afa511d8
runner=scripts/memory_v1_v5_2_single_preference_pipeline.py
python_bin=/opt/chat-memory/venv/bin/python
artifact_dir="/home/ubuntu/memory-v1-reviews/single-preference-clone-$(date -u +%Y%m%dT%H%M%SZ)-${head:0:12}"

set -a
source /opt/chat-memory/.env
set +a
[[ -n "${POSTGRES_DSN:-}" ]]
[[ -x "$python_bin" ]]
[[ -f "$repo_root/$runner" ]]
mkdir -p "$artifact_dir"
chmod 0700 "$artifact_dir"

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists --force "$clone_db" \
    >/dev/null 2>&1 || true
}
trap cleanup EXIT

production_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$source_db" -c "$1" | sed -n '1p'
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
      'entailments',(SELECT count(*) FROM memory.observation_entailment_v5),
      'requests',(SELECT count(*) FROM memory.relational_operation_request),
      'plans',(SELECT count(*) FROM memory.projection_plan),
      'items',(SELECT count(*) FROM memory.projection_plan_item),
      'preference_payloads',(SELECT count(*) FROM memory.projection_preference_payload),
      'plan_observations',(SELECT count(*) FROM memory.projection_plan_observation),
      'reviews',(SELECT count(*) FROM memory.projection_review),
      'preference_heads',(SELECT count(*) FROM memory.preference_head_v5),
      'preference_revisions',(SELECT count(*) FROM memory.preference_revision_v5),
      'preference_observations',(SELECT count(*) FROM memory.preference_revision_observation),
      'apply_events',(SELECT count(*) FROM memory.projection_apply_event),
      'dispatches',(SELECT count(*) FROM memory.projection_dispatch_v5),
      'claims',(SELECT count(*) FROM memory.claim)
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

manifest="$artifact_dir/manifest.json"
authorization="$artifact_dir/authorization.json"
cross_owner="$artifact_dir/cross-owner.json"
apply_result="$artifact_dir/apply.json"
replay_result="$artifact_dir/replay.json"
report="$artifact_dir/report.json"
common=(
  "POSTGRES_DSN=$clone_dsn"
  "PYTHONPATH=$repo_root"
  "MEMORY_V1_REQUIRED_HEAD=$head"
  "MEMORY_V1_V5_2_SINGLE_PREFERENCE_APPLY=authorized"
)

env "${common[@]}" "$python_bin" "$repo_root/$runner" manifest \
  --owner "$owner" --evidence "$evidence" --observation "$observation" \
  --required-head "$head" --output "$manifest"
env "${common[@]}" "$python_bin" "$repo_root/$runner" authorize \
  --manifest "$manifest" --output "$authorization"
env "${common[@]}" "$python_bin" "$repo_root/$runner" cross-owner \
  --manifest "$manifest" --other-owner "$other_owner" --output "$cross_owner"
env "${common[@]}" "$python_bin" "$repo_root/$runner" apply \
  --manifest "$manifest" --authorization "$authorization" \
  --confirm APPLY_ONE_OWNER_V5_2_AUDIOBOOK_PREFERENCE_ONLY \
  --output "$apply_result"
env "${common[@]}" "$python_bin" "$repo_root/$runner" replay \
  --manifest "$manifest" --authorization "$authorization" \
  --apply-result "$apply_result" \
  --confirm APPLY_ONE_OWNER_V5_2_AUDIOBOOK_PREFERENCE_ONLY \
  --output "$replay_result"

[[ "$(jq -er '.rows_written' "$apply_result")" == 13 ]]
[[ "$(jq -er '.insert_rows' "$apply_result")" == 12 ]]
[[ "$(jq -er '.mutated_rows' "$apply_result")" == 13 ]]
[[ "$(jq -er '.rows_written' "$replay_result")" == 0 ]]
[[ "$(jq -er '.cross_owner_rejected' "$cross_owner")" == true ]]

plan_id=$(jq -er '.plan_id' "$manifest")
review_id=$(jq -er '.review_id' "$apply_result")
preference_id=$(jq -er '.preference_id' "$apply_result")
revision_id=$(jq -er '.revision_id' "$apply_result")
expected_key=$(jq -er '.packet.projections[0].payload.preference_key' "$manifest")

verification=$(
  docker exec "$container" psql -X -A -F '|' -t -v ON_ERROR_STOP=1 \
    -U sage -d "$clone_db" -c "
      SELECT
        (SELECT count(*) FROM memory.observation_entailment_v5
         WHERE owner_user_id='$owner'::uuid
           AND observation_id='$observation'::uuid
           AND decision='accepted'),
        (SELECT count(*) FROM memory.projection_plan
         WHERE owner_user_id='$owner'::uuid
           AND plan_id='$plan_id'::uuid),
        (SELECT count(*) FROM memory.projection_plan_item
         WHERE owner_user_id='$owner'::uuid
           AND plan_id='$plan_id'::uuid
           AND lane='preference'
           AND predicate='preference.life'),
        (SELECT count(*) FROM memory.projection_preference_payload
         WHERE owner_user_id='$owner'::uuid
           AND plan_id='$plan_id'::uuid
           AND preference_domain='music'
           AND preference_key='$expected_key'
           AND value='{\"target\":\"audiobooks\",\"context\":null}'::jsonb
           AND preference_polarity='likes'
           AND stability='stable'),
        (SELECT count(*) FROM memory.projection_review
         WHERE owner_user_id='$owner'::uuid
           AND review_id='$review_id'::uuid
           AND decision='authorized'),
        (SELECT count(*) FROM memory.preference_head_v5
         WHERE owner_user_id='$owner'::uuid
           AND preference_id='$preference_id'::uuid
           AND preference_key='$expected_key'
           AND current_revision_id='$revision_id'::uuid
           AND revision_number=1),
        (SELECT count(*) FROM memory.preference_revision_v5
         WHERE owner_user_id='$owner'::uuid
           AND preference_id='$preference_id'::uuid
           AND revision_id='$revision_id'::uuid
           AND revision_number=1
           AND value='{\"target\":\"audiobooks\",\"context\":null}'::jsonb
           AND preference_polarity='likes'
           AND stability='stable'),
        (SELECT count(*) FROM memory.preference_revision_observation
         WHERE owner_user_id='$owner'::uuid
           AND revision_id='$revision_id'::uuid
           AND observation_id='$observation'::uuid
           AND stance='supports'),
        (SELECT count(*) FROM memory.projection_apply_event
         WHERE owner_user_id='$owner'::uuid
           AND plan_id='$plan_id'::uuid
           AND resulting_preference_revision_id='$revision_id'::uuid),
        (SELECT count(*) FROM memory.projection_dispatch_v5
         WHERE owner_user_id='$owner'::uuid
           AND resulting_preference_id='$preference_id'::uuid
           AND resulting_preference_revision_id='$revision_id'::uuid),
        (SELECT count(*) FROM memory.claim_observation
         WHERE owner_user_id='$owner'::uuid
           AND observation_id='$observation'::uuid)
    "
)
[[ "$verification" == "1|1|1|1|1|1|1|1|1|1|0" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(production_signature)" == "$production_before" ]]
[[ "$(git -C /opt/chat-memory rev-parse HEAD)" == "$production_head_before" ]]

REPORT="$report" MANIFEST="$manifest" APPLY="$apply_result" \
REPLAY="$replay_result" CROSS="$cross_owner" HEAD="$head" \
QDRANT="$qdrant_before" python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

manifest = json.loads(Path(os.environ["MANIFEST"]).read_text())
apply = json.loads(Path(os.environ["APPLY"]).read_text())
value = {
    "contract_version": "memory_v1_v5_2_single_preference_clone_report_v1",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "owner_user_id": manifest["owner_user_id"],
    "evidence_id": manifest["evidence_id"],
    "observation_id": manifest["observation_id"],
    "manifest_sha256": manifest["manifest_sha256"],
    "preference_id": apply["preference_id"],
    "revision_id": apply["revision_id"],
    "verification": {
        "exact_preference_materialized": True,
        "insert_rows": 12,
        "mutated_rows": 13,
        "zero_write_replay": True,
        "cross_owner_rejected": True,
        "production_unchanged": True,
        "qdrant_sha256": os.environ["QDRANT"],
        "qdrant_unchanged": True,
        "claims_written": 0,
        "retrieval_activated": False,
        "prompt_influence_activated": False,
    },
    "evidence": {
        "apply_result_sha256": apply["result_sha256"],
        "replay_result_sha256": json.loads(
            Path(os.environ["REPLAY"]).read_text()
        )["result_sha256"],
        "cross_owner_result_sha256": json.loads(
            Path(os.environ["CROSS"]).read_text()
        )["result_sha256"],
    },
}
path = Path(os.environ["REPORT"])
path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
path.chmod(0o600)
PY

printf 'report=%s\n' "$report"
printf 'memory_v1_v5_2_single_preference_clone: PASS\n'
