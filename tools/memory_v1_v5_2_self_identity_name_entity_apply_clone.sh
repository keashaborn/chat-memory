#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Applies the exact trusted-self identity.name resolution
# and observation binding in a disposable production clone.

[[ "$EUID" -eq 0 ]]
repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

container=brains-postgres-1
production=memory
clone="memory_v5_2_self_name_entity_${$}"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=9dd7426d-77eb-4765-9db2-13e33ad7444d
evidence=a7065682-12c2-414f-9c6f-fdcb898ee1c8
resolution=5e432b64-62aa-4ded-bf5e-4e4afb20e2f8
observation=fc86c43e-3fa6-465e-b348-8656e3a896c0
self_entity=35029129-27bd-457b-8cb5-82dd37ba32ba
runner=scripts/memory_v1_v5_2_entity_resolution_batch.py
fixture=tests/memory_v1_v5_2_entity_resolution_batch_fixture.py
manifest_source=manifests/memory_v1_v5_2_self_identity_name_entity_apply_20260730.json
review_root=/home/ubuntu/memory-v1-reviews
work=$(runuser -u ubuntu -- mktemp -d "$review_root/self-name-entity-clone.XXXXXX")
backup=$(mktemp /tmp/memory-v5-2-self-name-entity.XXXXXX.dump)
chmod 0700 "$work"
chmod 0600 "$backup"

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists --force "$clone" \
    >/dev/null 2>&1 || true
  rm -rf "$work"
  rm -f "$backup"
}
trap cleanup EXIT

scalar() {
  local database=$1 query=$2
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$query" | sed -n '1p'
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
  scalar "$production" "
    SELECT encode(public.digest(convert_to(
      coalesce(string_agg(value,E'\\n' ORDER BY value),''),
      'UTF8'),'sha256'),'hex')
    FROM (
      SELECT table_name || ':' || count(*)::text AS value
      FROM information_schema.tables
      WHERE table_schema='memory' AND table_type='BASE TABLE'
      GROUP BY table_name
    ) AS counts"
}

for file in "$runner" "$fixture" "$manifest_source"; do
  [[ -f "$file" ]]
done
[[ -z "$(git status --porcelain)" ]]
[[ "$(scalar "$production" "
  SELECT count(*)
  FROM memory.entity_resolution_plan
  WHERE owner_user_id='$owner'::uuid
    AND evidence_id='$evidence'::uuid
    AND resolution_id='$resolution'::uuid
    AND action='link_existing'
    AND decision_state='auto_link_eligible'
    AND selected_entity_id='$self_entity'::uuid
")" == 1 ]]
[[ "$(scalar "$production" "
  SELECT count(*) FROM memory.entity_resolution_apply
  WHERE owner_user_id='$owner'::uuid AND resolution_id='$resolution'::uuid
")" == 0 ]]

production_before=$(production_signature)
production_head_before=$(git -C /opt/chat-memory rev-parse HEAD)
qdrant_before=$(qdrant_signature)

docker exec "$container" pg_dump -U sage -d "$production" -Fc >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" <"$backup"

set -a
source /opt/chat-memory/.env
set +a
clone_dsn=$(SOURCE_DSN="$POSTGRES_DSN" CLONE_DB="$clone" python3 - <<'PY'
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

before_requests=$(scalar "$clone" "
  SELECT count(*) FROM memory.relational_operation_request
  WHERE owner_user_id='$owner'::uuid")
before_reviews=$(scalar "$clone" "
  SELECT count(*) FROM memory.entity_resolution_review
  WHERE owner_user_id='$owner'::uuid")
before_applies=$(scalar "$clone" "
  SELECT count(*) FROM memory.entity_resolution_apply
  WHERE owner_user_id='$owner'::uuid")
before_bindings=$(scalar "$clone" "
  SELECT count(*) FROM memory.observation_entity_binding
  WHERE owner_user_id='$owner'::uuid")
before_entities=$(scalar "$clone" "
  SELECT count(*) FROM memory.entity WHERE owner_user_id='$owner'::uuid")
before_claims=$(scalar "$clone" "
  SELECT count(*) FROM memory.claim WHERE owner_user_id='$owner'::uuid")
before_projections=$(scalar "$clone" "
  SELECT count(*) FROM memory.projection_plan
  WHERE owner_user_id='$owner'::uuid")

cp "$manifest_source" "$work/manifest.json"
chown ubuntu:ubuntu "$work/manifest.json"
chmod 0600 "$work/manifest.json"

runuser -u ubuntu -- env POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python "$repo_root/$runner" plan \
  --manifest "$work/manifest.json" --review-root "$work" \
  --output "$work/plan.json"

jq --arg owner "$other" '.owner_user_id=$owner' \
  "$work/manifest.json" >"$work/cross-owner-manifest.json"
chown ubuntu:ubuntu "$work/cross-owner-manifest.json"
chmod 0600 "$work/cross-owner-manifest.json"
if runuser -u ubuntu -- env POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python "$repo_root/$runner" plan \
  --manifest "$work/cross-owner-manifest.json" --review-root "$work" \
  --output "$work/cross-owner-plan.json" >/dev/null 2>&1; then
  echo 'cross-owner self-name entity plan unexpectedly passed' >&2
  exit 1
fi

head=$(git rev-parse HEAD)
runuser -u ubuntu -- /opt/chat-memory/venv/bin/python "$repo_root/$fixture" \
  --plan "$work/plan.json" --output "$work/authorization.json" --head "$head"
runuser -u ubuntu -- env \
  MEMORY_V1_V5_2_ENTITY_RESOLUTION_BATCH_APPLY=authorized \
  POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$repo_root/$runner" apply \
  --plan "$work/plan.json" --authorization "$work/authorization.json" \
  --review-root "$work" \
  --confirm RECONCILE_REVIEW_AND_APPLY_OWNER_V5_2_ENTITY_RESOLUTIONS_ONLY \
  --output "$work/apply-report.json"

[[ "$(jq -r '.database_rows_created' "$work/apply-report.json")" == 3 ]]
[[ "$(jq -r '.bindings_created' "$work/apply-report.json")" == 1 ]]
[[ "$(jq -r '.item_count' "$work/apply-report.json")" == 1 ]]
[[ "$(jq -r '.applied[0].operation' "$work/apply-report.json")" == auto_apply ]]
[[ "$(jq -r '.applied[0].applied_entity_id' "$work/apply-report.json")" \
  == "$self_entity" ]]
[[ "$(jq -r '.replayed[0].apply_outcome' "$work/apply-report.json")" == replayed ]]
[[ "$(jq -r '.replayed[0].bindings_created' "$work/apply-report.json")" == 0 ]]

[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.relational_operation_request
  WHERE owner_user_id='$owner'::uuid")" == "$((before_requests + 1))" ]]
[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.entity_resolution_review
  WHERE owner_user_id='$owner'::uuid")" == "$before_reviews" ]]
[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.entity_resolution_apply
  WHERE owner_user_id='$owner'::uuid")" == "$((before_applies + 1))" ]]
[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.observation_entity_binding
  WHERE owner_user_id='$owner'::uuid")" == "$((before_bindings + 1))" ]]
[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.entity WHERE owner_user_id='$owner'::uuid
")" == "$before_entities" ]]
[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.claim WHERE owner_user_id='$owner'::uuid
")" == "$before_claims" ]]
[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.projection_plan
  WHERE owner_user_id='$owner'::uuid")" == "$before_projections" ]]
[[ "$(scalar "$clone" "
  SELECT count(*)
  FROM memory.entity_resolution_apply
  WHERE owner_user_id='$owner'::uuid
    AND resolution_id='$resolution'::uuid
    AND applied_entity_id='$self_entity'::uuid
")" == 1 ]]
[[ "$(scalar "$clone" "
  SELECT count(*)
  FROM memory.observation_entity_binding
  WHERE owner_user_id='$owner'::uuid
    AND observation_id='$observation'::uuid
    AND subject_entity_id='$self_entity'::uuid
    AND subject_resolution_id='$resolution'::uuid
    AND object_entity_id IS NULL
    AND object_resolution_id IS NULL
")" == 1 ]]
[[ "$(scalar "$clone" "
  SELECT count(*)
  FROM memory.claim_observation
  WHERE owner_user_id='$owner'::uuid AND observation_id='$observation'::uuid
")" == 0 ]]

[[ "$(production_signature)" == "$production_before" ]]
[[ "$(git -C /opt/chat-memory rev-parse HEAD)" == "$production_head_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(systemctl is-active brains.service)" == active ]]

printf '%s\n' \
  'memory_v1_v5_2_self_identity_name_entity_apply_clone: PASS' \
  'database_rows=3' \
  'entity_apply_rows=1' \
  'observation_binding_rows=1' \
  'entity_rows=0' \
  'claim_rows=0' \
  'production_writes=0' \
  'qdrant_writes=0' \
  'prompt_influence=0'
