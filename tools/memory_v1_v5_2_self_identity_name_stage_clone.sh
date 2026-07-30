#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Stages one reviewed self identity.name atom in a
# disposable production clone. Production remains read-only.

[[ "$EUID" -eq 0 ]]
repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

container=brains-postgres-1
production=memory
clone="memory_v5_2_self_name_stage_${$}"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=9dd7426d-77eb-4765-9db2-13e33ad7444d
apply_id=d57a2c0c-d8c9-5211-960f-64de1190efe0
evidence=a7065682-12c2-414f-9c6f-fdcb898ee1c8
self_entity=35029129-27bd-457b-8cb5-82dd37ba32ba
build_manifest=manifests/memory_v1_v5_2_self_identity_name_atom_stage_20260730.json
bundle_builder=scripts/memory_v1_v5_2_atom_stage_bundle_v2.py
stage_runner=scripts/memory_v1_v5_2_stage_batch.py
stage_fixture=tests/memory_v1_v5_2_stage_batch_fixture.py
backup=$(mktemp /tmp/memory-v5-2-self-name-stage.XXXXXX.dump)
review_root=/home/ubuntu/memory-v1-reviews
work=$(runuser -u ubuntu -- mktemp -d "$review_root/self-name-stage-clone.XXXXXX")
chmod 0600 "$backup"
chmod 0700 "$work"

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists --force "$clone" \
    >/dev/null 2>&1 || true
  rm -f "$backup"
  rm -rf "$work"
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

stage_counts() {
  local database=$1
  scalar "$database" "
    SELECT concat_ws(',',
      (SELECT count(*) FROM memory.relational_stage_batch
       WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid),
      (SELECT count(*) FROM memory.relational_operation_request
       WHERE owner_user_id='$owner'::uuid AND target_key='$evidence'),
      (SELECT count(*) FROM memory.entity_mention
       WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid),
      (SELECT count(*) FROM memory.entity_resolution_plan
       WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid),
      (SELECT count(*) FROM memory.entity_resolution_candidate AS candidate
       JOIN memory.entity_resolution_plan AS resolution
         USING(owner_user_id,resolution_id)
       WHERE resolution.owner_user_id='$owner'::uuid
         AND resolution.evidence_id='$evidence'::uuid),
      (SELECT count(*) FROM memory.observation
       WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid),
      (SELECT count(*) FROM memory.observation_temporal AS temporal
       JOIN memory.observation AS observation
         USING(owner_user_id,observation_id)
       WHERE observation.owner_user_id='$owner'::uuid
         AND observation.evidence_id='$evidence'::uuid),
      (SELECT count(*) FROM memory.entity_resolution_apply AS applied
       JOIN memory.entity_resolution_plan AS resolution
         USING(owner_user_id,resolution_id)
       WHERE resolution.owner_user_id='$owner'::uuid
         AND resolution.evidence_id='$evidence'::uuid),
      (SELECT count(*) FROM memory.observation_entity_binding AS binding
       JOIN memory.observation AS observation
         USING(owner_user_id,observation_id)
       WHERE observation.owner_user_id='$owner'::uuid
         AND observation.evidence_id='$evidence'::uuid),
      (SELECT count(*) FROM memory.claim_observation AS linked
       JOIN memory.observation AS observation
         USING(owner_user_id,observation_id)
       WHERE observation.owner_user_id='$owner'::uuid
         AND observation.evidence_id='$evidence'::uuid)
    )"
}

for file in "$build_manifest" "$bundle_builder" "$stage_runner" "$stage_fixture"; do
  [[ -f "$file" ]]
done
[[ -z "$(git status --porcelain)" ]]
[[ "$(stage_counts "$production")" == '0,0,0,0,0,0,0,0,0,0' ]]
[[ "$(scalar "$production" "
  SELECT count(*)
  FROM memory.v5_2_atom_admission_apply
  WHERE owner_user_id='$owner'::uuid
    AND evidence_id='$evidence'::uuid
    AND apply_id='$apply_id'::uuid
")" == 1 ]]

production_before=$(production_signature)
production_head_before=$(git -C /opt/chat-memory rev-parse HEAD)
qdrant_before=$(qdrant_signature)
entities_before=$(scalar "$production" "
  SELECT count(*) FROM memory.entity WHERE owner_user_id='$owner'::uuid")
claims_before=$(scalar "$production" "
  SELECT count(*) FROM memory.claim WHERE owner_user_id='$owner'::uuid")

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

runuser -u ubuntu -- env POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python \
  "$repo_root/$bundle_builder" build \
  --manifest "$repo_root/$build_manifest" --output-root "$work" \
  >"$work/build-report.json"
runuser -u ubuntu -- env POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python \
  "$repo_root/$bundle_builder" probe \
  --bundle "$work/bundle.json" --apply-id "$apply_id" \
  --other-owner-user-id "$other" >"$work/probe-report.json"

[[ "$(jq -r '.expected_new_rows' "$work/build-report.json")" == 7 ]]
[[ "$(jq -r '.database_writes' "$work/build-report.json")" == 0 ]]
[[ "$(jq -r '.cross_owner_rejection' "$work/probe-report.json")" == P0002 ]]
[[ "$(jq -r '.tampered_projection_rejection' "$work/probe-report.json")" == 23514 ]]

runuser -u ubuntu -- env POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python \
  "$repo_root/$stage_runner" plan \
  --manifest "$work/stage-manifest.json" \
  --review-root "$work" --output "$work/stage-plan.json"
head=$(git rev-parse HEAD)
runuser -u ubuntu -- /opt/chat-memory/venv/bin/python \
  "$repo_root/$stage_fixture" authorize \
  --plan "$work/stage-plan.json" \
  --output "$work/stage-authorization.json" --head "$head"

runuser -u ubuntu -- env MEMORY_V1_V5_2_STAGE_BATCH_APPLY=authorized \
  POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$repo_root/$stage_runner" apply \
  --plan "$work/stage-plan.json" \
  --authorization "$work/stage-authorization.json" \
  --review-root "$work" \
  --confirm STAGE_REVIEWED_OWNER_V5_2_PACKETS_ONLY \
  --output "$work/stage-apply.json"

[[ "$(jq -r '.database_rows_created' "$work/stage-apply.json")" == 7 ]]
[[ "$(jq -r '.checks.replay_rows_written' "$work/stage-apply.json")" == 0 ]]
[[ "$(stage_counts "$clone")" == '1,1,1,1,1,1,1,0,0,0' ]]
[[ "$(scalar "$clone" "
  SELECT count(*)
  FROM memory.entity_resolution_plan
  WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid
    AND action='link_existing'
    AND decision_state='auto_link_eligible'
    AND selected_entity_id='$self_entity'::uuid
    AND review_reason_codes='[\"trusted_owner_self_binding\"]'::jsonb
")" == 1 ]]
[[ "$(scalar "$clone" "
  SELECT count(*)
  FROM memory.observation
  WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid
    AND predicate='identity.name'
    AND object_literal->>'value'='Eric'
")" == 1 ]]
[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.entity WHERE owner_user_id='$owner'::uuid
")" == "$entities_before" ]]
[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.claim WHERE owner_user_id='$owner'::uuid
")" == "$claims_before" ]]

resolution_id=$(scalar "$clone" "
  SELECT resolution_id
  FROM memory.entity_resolution_plan
  WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid")
observation_id=$(scalar "$clone" "
  SELECT observation_id
  FROM memory.observation
  WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid")
[[ "$resolution_id" =~ ^[0-9a-f-]{36}$ ]]
[[ "$observation_id" =~ ^[0-9a-f-]{36}$ ]]

[[ "$(production_signature)" == "$production_before" ]]
[[ "$(git -C /opt/chat-memory rev-parse HEAD)" == "$production_head_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(systemctl is-active brains.service)" == active ]]

printf '%s\n' \
  'memory_v1_v5_2_self_identity_name_stage_clone: PASS' \
  'clone_stage_rows=7' \
  "resolution_id=$resolution_id" \
  "observation_id=$observation_id" \
  "self_entity_id=$self_entity" \
  'entity_apply_rows=0' \
  'observation_binding_rows=0' \
  'claim_rows=0' \
  'production_writes=0' \
  'qdrant_writes=0' \
  'prompt_influence=0'
