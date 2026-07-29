#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores production into a disposable Postgres clone,
# stages the exact Max/Neko/Keasha packets, performs reviewed entity creation
# or linking, and proves production/Qdrant/claims remain unchanged.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
port=${MEMORY_V1_V5_2_PET_ENTITY_CLONE_PORT:-55509}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v52petentityclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
stage_runner=scripts/memory_v1_v5_2_stage_batch.py
stage_authorizer=tests/memory_v1_v5_2_stage_batch_fixture.py
entity_runner=scripts/memory_v1_v5_2_entity_resolution_batch.py
entity_authorizer=tests/memory_v1_v5_2_entity_resolution_batch_fixture.py
review_root=/home/ubuntu/memory-v1-reviews
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
self_entity=35029129-27bd-457b-8cb5-82dd37ba32ba
neko_entity=09308a2b-3019-4f59-8fc3-bb1fe1408a0d
max_evidence=5e938057-f012-5e54-8b37-148f3cc3ab9c
neko_evidence=ca637d00-7ff6-5147-8f6b-a82386dbc1c1
keasha_evidence=3eb237da-4eec-5fa4-83fb-51360356dba6
max_source="$review_root/v5-2-pet-species-domain-v1-24bcaf1f-f74c-59db-896d-06e05e83d7d9-stage.json"
neko_source="$review_root/v5-2-pet-species-domain-v1-c6adc46c-20e3-5e9e-80fd-ab53bfb65869-stage.json"
keasha_source="$review_root/v5-2-pet-species-domain-v1-c2c0a8e7-d9e7-53f4-a5a6-27f9d3a3570a-stage.json"
max_sha=5e011e10d7c9ca2ae314d86e741057fa719053c8c28c2301415b1b437a260d71
neko_sha=7d885de019dc5e8f8374d4ad470a58930a3ff68294898dd417d9c1be2fec9946
keasha_sha=ca203b435f526b4df7212a8312642a4ad03e06e0ce96f1ae2a494cb3bc39b7dc
backup=$(mktemp /tmp/memory-v1-v5-2-pet-entity.XXXXXX.dump)
role_sql=$(mktemp /tmp/memory-v1-v5-2-pet-entity-roles.XXXXXX.sql)
work=$(mktemp -d "$review_root/pet-species-entity-clone.XXXXXX")
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$role_sql"
  sudo rm -rf "$work"
}
trap cleanup EXIT
chmod 0600 "$backup" "$role_sql"
chmod 0700 "$work"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory "$@"
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

run_stage_root() {
  sudo env \
    POSTGRES_DSN="$dsn" \
    PYTHONPATH="$repo_root" \
    GIT_OPTIONAL_LOCKS=0 \
    GIT_CONFIG_COUNT=1 \
    GIT_CONFIG_KEY_0=safe.directory \
    GIT_CONFIG_VALUE_0="$repo_root" \
    "$@"
}

assert_equal() {
  local label=$1 actual=$2 expected=$3
  if [[ "$actual" != "$expected" ]]; then
    printf 'ASSERTION_FAILED=%s\nexpected=%s\nactual=%s\n' \
      "$label" "$expected" "$actual" >&2
    exit 1
  fi
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
  docker exec brains-postgres-1 psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "
      SELECT encode(public.digest(convert_to(concat_ws('|',
        (SELECT count(*) FROM memory.relational_stage_batch),
        (SELECT count(*) FROM memory.relational_operation_request),
        (SELECT count(*) FROM memory.entity_mention),
        (SELECT count(*) FROM memory.entity_resolution_plan),
        (SELECT count(*) FROM memory.entity_resolution_review),
        (SELECT count(*) FROM memory.entity_resolution_apply),
        (SELECT count(*) FROM memory.entity_alias_observation),
        (SELECT count(*) FROM memory.entity),
        (SELECT count(*) FROM memory.observation),
        (SELECT count(*) FROM memory.observation_temporal),
        (SELECT count(*) FROM memory.observation_entity_binding),
        (SELECT count(*) FROM memory.claim),
        (SELECT count(*) FROM memory.projection_outbox)
      ),'UTF8'),'sha256'),'hex')"
}

target_counts() {
  scalar "
    WITH target(evidence_id) AS (
      VALUES
        ('$max_evidence'::uuid),
        ('$neko_evidence'::uuid),
        ('$keasha_evidence'::uuid)
    )
    SELECT concat_ws(',',
      (SELECT count(*) FROM memory.relational_stage_batch
       WHERE owner_user_id='$owner'::uuid
         AND evidence_id IN (SELECT evidence_id FROM target)),
      (SELECT count(*) FROM memory.entity_mention
       WHERE owner_user_id='$owner'::uuid
         AND evidence_id IN (SELECT evidence_id FROM target)),
      (SELECT count(*) FROM memory.entity_resolution_plan
       WHERE owner_user_id='$owner'::uuid
         AND evidence_id IN (SELECT evidence_id FROM target)),
      (SELECT count(*) FROM memory.entity_resolution_candidate AS candidate
       JOIN memory.entity_resolution_plan AS plan
         USING(owner_user_id,resolution_id)
       WHERE plan.owner_user_id='$owner'::uuid
         AND plan.evidence_id IN (SELECT evidence_id FROM target)),
      (SELECT count(*) FROM memory.observation
       WHERE owner_user_id='$owner'::uuid
         AND evidence_id IN (SELECT evidence_id FROM target)),
      (SELECT count(*) FROM memory.observation_temporal AS temporal
       JOIN memory.observation AS observation
         USING(owner_user_id,observation_id)
       WHERE observation.owner_user_id='$owner'::uuid
         AND observation.evidence_id IN (SELECT evidence_id FROM target)),
      (SELECT count(*) FROM memory.observation_entity_binding AS binding
       JOIN memory.observation AS observation
         USING(owner_user_id,observation_id)
       WHERE observation.owner_user_id='$owner'::uuid
         AND observation.evidence_id IN (SELECT evidence_id FROM target))
    )"
}

[[ -z "$(GIT_OPTIONAL_LOCKS=0 git status --porcelain)" ]]
assert_equal max_bundle_hash "$(sudo sha256sum "$max_source" | awk '{print $1}')" "$max_sha"
assert_equal neko_bundle_hash "$(sudo sha256sum "$neko_source" | awk '{print $1}')" "$neko_sha"
assert_equal keasha_bundle_hash "$(sudo sha256sum "$keasha_source" | awk '{print $1}')" "$keasha_sha"

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
  ORDER BY rolname
" >"$role_sql"
[[ -s "$role_sql" ]]

"${compose[@]}" up -d --wait postgres
run_sql <"$role_sql"
printf '%s\n' "ALTER ROLE brains_app PASSWORD 'clone_only_brains_password';" | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists <"$backup"

assert_equal initial_target_counts "$(target_counts)" '0,0,0,0,0,0,0'
entities_before=$(scalar "SELECT count(*) FROM memory.entity WHERE owner_user_id='$owner'::uuid")
claims_before=$(scalar "SELECT count(*) FROM memory.claim WHERE owner_user_id='$owner'::uuid")
projections_before=$(scalar "SELECT count(*) FROM memory.projection_outbox WHERE owner_user_id='$owner'::uuid")
other_before=$(scalar "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.entity WHERE owner_user_id='$other_owner'::uuid),
    (SELECT count(*) FROM memory.observation_entity_binding WHERE owner_user_id='$other_owner'::uuid),
    (SELECT count(*) FROM memory.claim WHERE owner_user_id='$other_owner'::uuid))")

for source in "$max_source" "$neko_source" "$keasha_source"; do
  sudo install -m 0600 -o "$(id -un)" -g "$(id -gn)" "$source" "$work/$(basename "$source")"
done

python3 - "$work/stage-manifest.json" "$work" "$owner" \
  "$max_sha" "$neko_sha" "$keasha_sha" <<'PY'
import json
import os
from pathlib import Path
import sys

output = Path(sys.argv[1])
root = Path(sys.argv[2])
owner = sys.argv[3]
names = [
    "v5-2-pet-species-domain-v1-24bcaf1f-f74c-59db-896d-06e05e83d7d9-stage.json",
    "v5-2-pet-species-domain-v1-c6adc46c-20e3-5e9e-80fd-ab53bfb65869-stage.json",
    "v5-2-pet-species-domain-v1-c2c0a8e7-d9e7-53f4-a5a6-27f9d3a3570a-stage.json",
]
hashes = sys.argv[4:7]
counts = [
    {"mentions": 2, "resolutions": 2, "candidates": 1, "observations": 4, "temporals": 4},
    {"mentions": 2, "resolutions": 2, "candidates": 2, "observations": 3, "temporals": 3},
    {"mentions": 2, "resolutions": 2, "candidates": 1, "observations": 4, "temporals": 4},
]
value = {
    "contract_version": "memory_v1_v5_2_stage_batch_manifest_v1",
    "target_server": "seebx",
    "owner_user_id": owner,
    "bundles": [
        {
            "path": str(root / name),
            "sha256": digest,
            "expected_outcome": "applied",
            "expected_counts": count,
        }
        for name, digest, count in zip(names, hashes, counts, strict=True)
    ],
}
output.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
os.chmod(output, 0o600)
PY

run_stage_root /opt/chat-memory/venv/bin/python "$stage_runner" plan \
  --manifest "$work/stage-manifest.json" \
  --review-root "$review_root" \
  --output "$work/stage-plan.json"
sudo chown "$(id -un):$(id -gn)" "$work/stage-plan.json"
head=$(git rev-parse HEAD)
/opt/chat-memory/venv/bin/python "$stage_authorizer" authorize \
  --plan "$work/stage-plan.json" \
  --output "$work/stage-authorization.json" \
  --head "$head"
sudo env \
  MEMORY_V1_V5_2_STAGE_BATCH_APPLY=authorized \
  POSTGRES_DSN="$dsn" \
  PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 \
  GIT_CONFIG_COUNT=1 \
  GIT_CONFIG_KEY_0=safe.directory \
  GIT_CONFIG_VALUE_0="$repo_root" \
  /opt/chat-memory/venv/bin/python "$stage_runner" apply \
  --plan "$work/stage-plan.json" \
  --authorization "$work/stage-authorization.json" \
  --review-root "$review_root" \
  --confirm STAGE_REVIEWED_OWNER_V5_2_PACKETS_ONLY \
  --output "$work/stage-apply.json"
sudo chown "$(id -un):$(id -gn)" "$work/stage-apply.json"

assert_equal stage_rows "$(jq -r '.database_rows_created' "$work/stage-apply.json")" 44
assert_equal stage_replay "$(jq -r '.checks.replay_rows_written' "$work/stage-apply.json")" 0
assert_equal staged_target_counts "$(target_counts)" '3,6,6,4,11,11,0'
assert_equal stage_entities_unchanged \
  "$(scalar "SELECT count(*) FROM memory.entity WHERE owner_user_id='$owner'::uuid")" \
  "$entities_before"

resolution_id() {
  local evidence=$1 entity_ref=$2
  scalar "
    SELECT plan.resolution_id
    FROM memory.entity_resolution_plan AS plan
    JOIN memory.entity_mention AS mention USING(owner_user_id,mention_id)
    WHERE plan.owner_user_id='$owner'::uuid
      AND plan.evidence_id='$evidence'::uuid
      AND mention.entity_ref='$entity_ref'"
}
max_pet_resolution=$(resolution_id "$max_evidence" e00)
max_self_resolution=$(resolution_id "$max_evidence" e01)
neko_pet_resolution=$(resolution_id "$neko_evidence" e00)
neko_self_resolution=$(resolution_id "$neko_evidence" e01)
keasha_pet_resolution=$(resolution_id "$keasha_evidence" e00)
keasha_self_resolution=$(resolution_id "$keasha_evidence" e01)

python3 - "$work/entity-manifest.json" "$owner" \
  "$max_pet_resolution" "$max_self_resolution" \
  "$neko_pet_resolution" "$neko_self_resolution" \
  "$keasha_pet_resolution" "$keasha_self_resolution" "$neko_entity" <<'PY'
import json
import os
from pathlib import Path
import sys

output = Path(sys.argv[1])
owner = sys.argv[2]
max_pet, max_self, neko_pet, neko_self, keasha_pet, keasha_self, neko_entity = sys.argv[3:10]
value = {
    "contract_version": "memory_v1_v5_2_entity_resolution_batch_manifest_v1",
    "target_server": "seebx",
    "owner_user_id": owner,
    "expected_total_bindings": 11,
    "expected_new_rows": 34,
    "items": [
        {
            "resolution_id": max_pet,
            "operation": "manual_create_new_and_apply",
            "expected_action": "create_new",
            "expected_decision_state": "manual_review_required",
            "review_reason": "The packet explicitly names Max as the owner's dog and has no owner-scoped animal match; approve creation of the reviewed named animal.",
        },
        {
            "resolution_id": max_self,
            "operation": "auto_apply",
            "expected_action": "link_existing",
            "expected_decision_state": "auto_link_eligible",
            "review_reason": None,
        },
        {
            "resolution_id": neko_pet,
            "operation": "manual_link_existing_and_apply",
            "expected_action": "link_existing",
            "expected_decision_state": "manual_review_required",
            "expected_entity_id": neko_entity,
            "review_reason": "The packet explicitly names Neko and exactly one active owner-scoped animal entity has canonical name Neko; approve the reviewed link.",
        },
        {
            "resolution_id": neko_self,
            "operation": "auto_apply",
            "expected_action": "link_existing",
            "expected_decision_state": "auto_link_eligible",
            "review_reason": None,
        },
        {
            "resolution_id": keasha_pet,
            "operation": "manual_create_new_and_apply",
            "expected_action": "create_new",
            "expected_decision_state": "manual_review_required",
            "review_reason": "The packet explicitly names Keasha von Steffen Haus as the owner's dog and has no owner-scoped animal match; approve creation of the reviewed named animal.",
        },
        {
            "resolution_id": keasha_self,
            "operation": "auto_apply",
            "expected_action": "link_existing",
            "expected_decision_state": "auto_link_eligible",
            "review_reason": None,
        },
    ],
}
output.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
os.chmod(output, 0o600)
PY

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$entity_runner" plan \
  --manifest "$work/entity-manifest.json" \
  --review-root "$review_root" \
  --output "$work/entity-plan.json"

jq --arg owner "$other_owner" '.owner_user_id=$owner' \
  "$work/entity-manifest.json" >"$work/cross-owner-manifest.json"
chmod 0600 "$work/cross-owner-manifest.json"
if POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$entity_runner" plan \
  --manifest "$work/cross-owner-manifest.json" \
  --review-root "$review_root" \
  --output "$work/cross-owner-plan.json" >/dev/null 2>&1; then
  echo 'cross-owner entity plan unexpectedly passed' >&2
  exit 1
fi

PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$entity_authorizer" \
  --plan "$work/entity-plan.json" \
  --output "$work/entity-authorization.json" \
  --head "$head"
MEMORY_V1_V5_2_ENTITY_RESOLUTION_BATCH_APPLY=authorized \
  POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$entity_runner" apply \
  --plan "$work/entity-plan.json" \
  --authorization "$work/entity-authorization.json" \
  --review-root "$review_root" \
  --confirm RECONCILE_REVIEW_AND_APPLY_OWNER_V5_2_ENTITY_RESOLUTIONS_ONLY \
  --output "$work/entity-apply.json"

assert_equal entity_rows "$(jq -r '.database_rows_created' "$work/entity-apply.json")" 34
assert_equal entity_bindings "$(jq -r '.bindings_created' "$work/entity-apply.json")" 11
assert_equal entity_items "$(jq -r '.item_count' "$work/entity-apply.json")" 6
assert_equal entity_replay \
  "$(jq -r '[.replayed[].apply_outcome] | unique | join(",")' "$work/entity-apply.json")" \
  replayed
assert_equal final_target_counts "$(target_counts)" '3,6,6,4,11,11,11'
assert_equal entity_delta \
  "$(( $(scalar "SELECT count(*) FROM memory.entity WHERE owner_user_id='$owner'::uuid") - entities_before ))" \
  2
assert_equal created_animals "$(scalar "
  SELECT string_agg(canonical_name,',' ORDER BY canonical_name)
  FROM memory.entity
  WHERE owner_user_id='$owner'::uuid
    AND entity_type='animal'
    AND canonical_name IN ('Max','Keasha von Steffen Haus')")" \
  'Keasha von Steffen Haus,Max'
assert_equal self_links "$(scalar "
  SELECT count(*)
  FROM memory.entity_resolution_apply
  WHERE owner_user_id='$owner'::uuid
    AND applied_entity_id='$self_entity'::uuid
    AND resolution_id IN (
      '$max_self_resolution'::uuid,
      '$neko_self_resolution'::uuid,
      '$keasha_self_resolution'::uuid
    )")" 3
assert_equal neko_link "$(scalar "
  SELECT count(*)
  FROM memory.entity_resolution_apply
  WHERE owner_user_id='$owner'::uuid
    AND resolution_id='$neko_pet_resolution'::uuid
    AND applied_entity_id='$neko_entity'::uuid")" 1
assert_equal claims_unchanged \
  "$(scalar "SELECT count(*) FROM memory.claim WHERE owner_user_id='$owner'::uuid")" \
  "$claims_before"
assert_equal projections_unchanged \
  "$(scalar "SELECT count(*) FROM memory.projection_outbox WHERE owner_user_id='$owner'::uuid")" \
  "$projections_before"
assert_equal other_owner_unchanged "$(scalar "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.entity WHERE owner_user_id='$other_owner'::uuid),
    (SELECT count(*) FROM memory.observation_entity_binding WHERE owner_user_id='$other_owner'::uuid),
    (SELECT count(*) FROM memory.claim WHERE owner_user_id='$other_owner'::uuid))")" \
  "$other_before"
assert_equal qdrant_unchanged "$(qdrant_signature)" "$qdrant_before"
assert_equal production_unchanged "$(production_signature)" "$production_before"
assert_equal production_head_unchanged \
  "$(git -C /opt/chat-memory rev-parse HEAD)" "$production_head_before"
assert_equal brains_service "$(systemctl is-active brains.service)" active

printf '%s\n' \
  'MEMORY_V1_V5_2_PET_SPECIES_ENTITY_APPLY_CLONE=PASS' \
  'packets_staged=3' \
  'stage_rows_created=44' \
  'resolutions_applied=6' \
  'entities_created=2' \
  'existing_entities_linked=4' \
  'observation_bindings=11' \
  'claims_created=0' \
  'qdrant_writes=0' \
  'cross_owner_plan_rejected=true' \
  'zero_write_replay=true' \
  'hard_stop=before_claim_candidates_claims_projection_or_retrieval'
