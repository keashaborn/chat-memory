#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Records accepted source entailment and a deferred review
# for one exact medication stance in a disposable production clone. No durable
# claim, Qdrant write, retrieval, or prompt influence is allowed.

[[ "$EUID" -eq 0 ]]
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
head=$(git -C "$repo_root" rev-parse HEAD)
container=brains-postgres-1
source_db=memory
clone_db="memory_v5_2_stance_deferred_$(date -u +%Y%m%d%H%M%S)_$$"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
evidence=4550d3a1-7649-5d1b-aff8-f2504e36f869
observation=af2293d8-1461-4d04-8fae-e8e94467cced
runner=scripts/memory_v1_v5_2_single_stance_deferred.py
python_bin=/opt/chat-memory/venv/bin/python
artifact_dir="/home/ubuntu/memory-v1-reviews/single-stance-deferred-clone-$(date -u +%Y%m%dT%H%M%SZ)-${head:0:12}"

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
      'payloads',(SELECT count(*) FROM memory.projection_claim_payload),
      'links',(SELECT count(*) FROM memory.projection_plan_observation),
      'reviews',(SELECT count(*) FROM memory.projection_review),
      'claims',(SELECT count(*) FROM memory.claim),
      'apply_events',(SELECT count(*) FROM memory.projection_apply_event)
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
  "MEMORY_V1_V5_2_SINGLE_STANCE_DEFER=authorized"
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
  --confirm DEFER_ONE_OWNER_V5_2_MEDICATION_STANCE_ONLY \
  --output "$apply_result"
env "${common[@]}" "$python_bin" "$repo_root/$runner" replay \
  --manifest "$manifest" --authorization "$authorization" \
  --apply-result "$apply_result" \
  --confirm DEFER_ONE_OWNER_V5_2_MEDICATION_STANCE_ONLY \
  --output "$replay_result"

[[ "$(jq -er '.rows_written' "$apply_result")" == 7 ]]
[[ "$(jq -er '.review_decision' "$apply_result")" == deferred ]]
[[ "$(jq -er '.claims_written' "$apply_result")" == 0 ]]
[[ "$(jq -er '.rows_written' "$replay_result")" == 0 ]]
[[ "$(jq -er '.cross_owner_rejected' "$cross_owner")" == true ]]

plan_id=$(jq -er '.plan_id' "$manifest")
review_id=$(jq -er '.review_id' "$apply_result")
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
           AND lane='claim'
           AND predicate='stance.reported'
           AND review_state='manual_review_required'),
        (SELECT count(*) FROM memory.projection_claim_payload
         WHERE owner_user_id='$owner'::uuid
           AND plan_id='$plan_id'::uuid
           AND claim_class='reported_stance'),
        (SELECT count(*) FROM memory.projection_review
         WHERE owner_user_id='$owner'::uuid
           AND review_id='$review_id'::uuid
           AND decision='deferred'),
        (SELECT count(*) FROM memory.projection_apply_event
         WHERE owner_user_id='$owner'::uuid
           AND plan_id='$plan_id'::uuid),
        (SELECT count(*) FROM memory.claim_observation
         WHERE owner_user_id='$owner'::uuid
           AND observation_id='$observation'::uuid)
    "
)
[[ "$verification" == "1|1|1|1|1|0|0" ]]
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
    "contract_version": "memory_v1_v5_2_single_stance_deferred_clone_report_v1",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "owner_user_id": manifest["owner_user_id"],
    "evidence_id": manifest["evidence_id"],
    "observation_id": manifest["observation_id"],
    "manifest_sha256": manifest["manifest_sha256"],
    "review_id": apply["review_id"],
    "verification": {
        "source_entailment_accepted": True,
        "projection_review_deferred": True,
        "rows_written": 7,
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
printf 'memory_v1_v5_2_single_stance_deferred_clone: PASS\n'
