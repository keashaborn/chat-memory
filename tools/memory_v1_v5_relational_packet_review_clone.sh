#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Verifies that an accepted local relational packet can
# produce a restricted entity-resolution review artifact even when extraction
# itself did not request manual review. Production and Qdrant remain read-only.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source .env
set +a

container=brains-postgres-1
production=memory
clone="memory_v5_review_lane_${$}"
router=scripts/memory_v1_v5_local_packet_router.py
reviewer=scripts/memory_v1_v5_review_local_packet.py
unit_test=scripts/memory_v1_v5_review_local_packet_test.py
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
backup=$(mktemp /tmp/memory-v1-review-lane.XXXXXX.dump)
review_root=$(mktemp -d /tmp/memory-v1-review-lane.XXXXXX)
dry=$(mktemp /tmp/memory-v1-review-lane-dry.XXXXXX.json)
applied=$(mktemp /tmp/memory-v1-review-lane-applied.XXXXXX.json)
replayed=$(mktemp /tmp/memory-v1-review-lane-replayed.XXXXXX.json)
chmod 0600 "$backup" "$dry" "$applied" "$replayed"
chmod 0700 "$review_root"

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists "$clone" >/dev/null 2>&1 || true
  rm -f "$backup" "$dry" "$applied" "$replayed"
  find "$review_root" -type f -delete
  rmdir "$review_root"
}
trap cleanup EXIT

clone_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$clone" -c "$1" | tr -d '[:space:]'
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

[[ -z "$(git status --porcelain)" ]]
[[ "$(sha256sum "$reviewer" | awk '{print $1}')" \
  == c94817bf7b0c74fb9a2808794b5a34284021d090b23e022e60db63168c71f111 ]]
[[ "$(sha256sum "$unit_test" | awk '{print $1}')" \
  == a8e1758faa4b33cc7de55ca72399e8f538c16bef5a11c200db378218721bf59e ]]
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$unit_test" >/dev/null

qdrant_before=$(qdrant_signature)
production_claims_before=$(docker exec "$container" psql -X -A -t \
  -U sage -d "$production" -c 'SELECT count(*) FROM memory.claim' \
  | tr -d '[:space:]')

docker exec "$container" pg_dump -U sage -d "$production" -Fc >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --exit-on-error <"$backup"

target=$(docker exec "$container" psql -X -A -t -F $'\t' \
  -v ON_ERROR_STOP=1 -U sage -d "$clone" -c "
    SELECT packet.owner_user_id,packet.packet_id
    FROM memory.evidence_extraction_packet_v5_local AS packet
    JOIN memory.evidence_extraction_job AS job
      ON job.owner_user_id=packet.owner_user_id
     AND job.job_id=packet.job_id
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id=packet.owner_user_id
     AND evidence.evidence_id=packet.evidence_id
    WHERE packet.provider_id='local_llama_cpp'
      AND packet.local_model_calls=1
      AND packet.external_model_calls=0
      AND NOT packet.manual_review_required
      AND (packet.entity_mention_count>0
        OR packet.observation_count>0
        OR packet.comparison_hint_count>0)
      AND job.status='review_required'
      AND job.route='relational_extraction'
      AND job.lease_token IS NULL
      AND job.lease_expires_at IS NULL
      AND job.last_error IS NULL
      AND evidence.status='active'
      AND NOT EXISTS (
        SELECT 1 FROM memory.relational_stage_batch AS stage
        WHERE stage.owner_user_id=packet.owner_user_id
          AND stage.evidence_id=packet.evidence_id
      )
      AND NOT EXISTS (
        SELECT 1 FROM memory.v5_local_packet_review_artifact AS artifact
        WHERE artifact.owner_user_id=packet.owner_user_id
          AND artifact.packet_id=packet.packet_id
      )
    ORDER BY packet.created_at,packet.packet_id
    LIMIT 1")
[[ -n "$target" ]]
IFS=$'\t' read -r owner packet <<<"$target"
[[ "$owner" =~ ^[0-9a-f-]{36}$ && "$packet" =~ ^[0-9a-f-]{36}$ ]]

clone_dsn=$(/opt/chat-memory/venv/bin/python -c \
  'import sys; from urllib.parse import urlsplit,urlunsplit; u=urlsplit(sys.argv[1]); print(urlunsplit((u.scheme,u.netloc,"/"+sys.argv[2],u.query,u.fragment)))' \
  "$POSTGRES_DSN" "$clone")

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$router" \
  --owner-user-id "$owner" --review-root "$review_root" >"$dry"
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
MEMORY_V1_V5_LOCAL_PACKET_ROUTER_APPLY=memory_v1_v5_local_packet_router_apply_v1 \
  /opt/chat-memory/venv/bin/python "$router" \
  --owner-user-id "$owner" --review-root "$review_root" --apply >"$applied"
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
MEMORY_V1_V5_LOCAL_PACKET_ROUTER_APPLY=memory_v1_v5_local_packet_router_apply_v1 \
  /opt/chat-memory/venv/bin/python "$router" \
  --owner-user-id "$owner" --review-root "$review_root" --apply >"$replayed"

DRY="$dry" APPLIED="$applied" REPLAYED="$replayed" python3 - <<'PY'
import json
import os
from pathlib import Path

dry=json.loads(Path(os.environ['DRY']).read_text())
applied=json.loads(Path(os.environ['APPLIED']).read_text())
replayed=json.loads(Path(os.environ['REPLAYED']).read_text())
assert dry['plans'][0]['route']=='manual_review'
assert applied['outcome']=='manual_review_artifact_ready'
assert applied['write_counts']['packet_route_events']==1
assert applied['write_counts']['restricted_review_artifacts']==2
assert applied['write_counts']['claims']==0
assert applied['write_counts']['qdrant']==0
assert applied['write_counts']['prompt_influence']==0
assert applied['external_model_calls']==0
assert applied['zero_write_replay_proved'] is True
assert replayed['outcome']=='no_work'
assert replayed['write_counts']['packet_route_events']==0
serialized=json.dumps([dry,applied,replayed])
assert 'source_text' not in serialized and 'evidence_content' not in serialized
PY

[[ "$(find "$review_root" -maxdepth 1 -type f -perm 0600 | wc -l)" -eq 2 ]]
[[ "$(clone_scalar "SELECT count(*) FROM memory.v5_local_packet_review_artifact
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid")" == 1 ]]

foreign_count=$(POSTGRES_DSN="$clone_dsn" psql "$clone_dsn" -X -q -A -t \
  -v ON_ERROR_STOP=1 -c "BEGIN;
  SELECT set_config('app.user_id','$other_owner',true);
  SELECT count(*) FROM memory.read_owner_v5_local_packet_review_v1('$packet'::uuid);
  ROLLBACK;" | tail -n 1)
[[ "$foreign_count" == 0 ]]

[[ "$(docker exec "$container" psql -X -A -t -U sage -d "$production" \
  -c 'SELECT count(*) FROM memory.claim' | tr -d '[:space:]')" \
  == "$production_claims_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

printf '%s\n' 'memory_v1_v5_relational_packet_review_clone: PASS'
