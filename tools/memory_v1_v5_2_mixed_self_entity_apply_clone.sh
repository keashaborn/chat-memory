#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Applies two exact auto-link self resolutions in a
# disposable production clone. It creates no entities, claims, projections,
# Qdrant vectors, retrieval changes, or prompt influence.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
port=${MEMORY_V1_V5_2_MIXED_ENTITY_CLONE_PORT:-55499}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v52mixedentityclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
runner=scripts/memory_v1_v5_2_entity_resolution_batch.py
fixture=tests/memory_v1_v5_2_entity_resolution_batch_fixture.py
unit_test=tests/test_memory_v1_v5_2_entity_resolution_batch.py
manifest_source=manifests/memory_v1_v5_2_mixed_self_entity_apply_20260725.json
review_root=/home/ubuntu/memory-v1-reviews
target_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
resolution_one=7f924d81-f01a-4c3d-b57b-050d7013782c
resolution_two=bc33ee5d-e2b9-43c1-9ab5-8eb541c1fcf3
evidence_one=405fcdb1-a4d2-53ff-91ad-542b258cea03
evidence_two=4550d3a1-7649-5d1b-aff8-f2504e36f869
backup=$(mktemp /tmp/memory-v1-v5-2-mixed-entity.XXXXXX.dump)
role_sql=$(mktemp /tmp/memory-v1-v5-2-mixed-entity-roles.XXXXXX.sql)
work=$(mktemp -d "$review_root/mixed-self-entity-clone.XXXXXX")
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$role_sql"
  rm -rf "$work"
}
trap cleanup EXIT
chmod 0600 "$backup" "$role_sql"
chmod 0700 "$work"

assert_equal() {
  local label=$1 actual=$2 expected=$3
  if [[ "$actual" != "$expected" ]]; then
    printf 'ASSERTION_FAILED=%s\nexpected=%s\nactual=%s\n' \
      "$label" "$expected" "$actual" >&2
    exit 1
  fi
}

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory "$@"
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
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
  docker exec brains-postgres-1 psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "
      SELECT encode(public.digest(convert_to(
        coalesce(string_agg(value,E'\\n' ORDER BY value),''),
        'UTF8'),'sha256'),'hex')
      FROM (
        SELECT table_name || ':' || count(*)::text AS value
        FROM information_schema.tables
        CROSS JOIN LATERAL (SELECT 1) AS marker
        WHERE table_schema='memory' AND table_type='BASE TABLE'
        GROUP BY table_name
      ) AS counts"
}

[[ -z "$(GIT_OPTIONAL_LOCKS=0 git status --porcelain)" ]]
[[ "$(sha256sum "$manifest_source" | awk '{print $1}')" == \
  a7e8372bfb6401f4a440f99f74c2e2d16346d14c5152c17cd6c5cbdf33965711 ]]
[[ "$(sha256sum "$runner" | awk '{print $1}')" == \
  6b1a596c7d8e878c1f979866f95226c1f269505313d07cf036019f3b9a628d19 ]]
[[ "$(sha256sum "$fixture" | awk '{print $1}')" == \
  9d913127b681c0e781daae045357e24cd000a05e3702699d72caefe9f5f682e7 ]]
[[ "$(sha256sum "$unit_test" | awk '{print $1}')" == \
  033381b0682edceb5a390e055dff84890a105b61ec9a6fd9e46a675522a43719 ]]

qdrant_before=$(qdrant_signature)
production_before=$(production_signature)
production_head_before=$(git -C /opt/chat-memory rev-parse HEAD)

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc --no-owner --no-privileges >"$backup"
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
printf '%s\n' \
  "ALTER ROLE brains_app PASSWORD 'clone_only_brains_password';" | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"

PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$unit_test"
before_requests=$(scalar "SELECT count(*) FROM memory.relational_operation_request
  WHERE owner_user_id='$target_owner'::uuid")
before_entities=$(scalar "SELECT count(*) FROM memory.entity
  WHERE owner_user_id='$target_owner'::uuid")
before_reviews=$(scalar "SELECT count(*) FROM memory.entity_resolution_review
  WHERE owner_user_id='$target_owner'::uuid")
before_applies=$(scalar "SELECT count(*) FROM memory.entity_resolution_apply
  WHERE owner_user_id='$target_owner'::uuid")
before_bindings=$(scalar "SELECT count(*) FROM memory.observation_entity_binding
  WHERE owner_user_id='$target_owner'::uuid")
before_claims=$(scalar "SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$target_owner'::uuid")
before_projections=$(scalar "SELECT count(*) FROM memory.projection_plan
  WHERE owner_user_id='$target_owner'::uuid")

cp "$manifest_source" "$work/manifest.json"
chmod 0600 "$work/manifest.json"
head=$(git rev-parse HEAD)
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$runner" plan \
  --manifest "$work/manifest.json" \
  --review-root "$review_root" \
  --output "$work/plan.json"
jq --arg owner "$other_owner" '.owner_user_id=$owner' \
  "$work/manifest.json" >"$work/cross-owner-manifest.json"
chmod 0600 "$work/cross-owner-manifest.json"
if POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$runner" plan \
  --manifest "$work/cross-owner-manifest.json" \
  --review-root "$review_root" \
  --output "$work/cross-owner-plan.json" >/dev/null 2>&1; then
  echo 'cross-owner mixed entity plan unexpectedly passed' >&2
  exit 1
fi

PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$fixture" \
  --plan "$work/plan.json" \
  --output "$work/authorization.json" \
  --head "$head"
MEMORY_V1_V5_2_ENTITY_RESOLUTION_BATCH_APPLY=authorized \
  POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$runner" apply \
  --plan "$work/plan.json" \
  --authorization "$work/authorization.json" \
  --review-root "$review_root" \
  --confirm RECONCILE_REVIEW_AND_APPLY_OWNER_V5_2_ENTITY_RESOLUTIONS_ONLY \
  --output "$work/apply-report.json"

assert_equal database_rows \
  "$(jq -r '.database_rows_created' "$work/apply-report.json")" 6
assert_equal bindings \
  "$(jq -r '.bindings_created' "$work/apply-report.json")" 2
assert_equal item_count \
  "$(jq -r '.item_count' "$work/apply-report.json")" 2
assert_equal replay_outcomes \
  "$(jq -r '[.replayed[].apply_outcome] | unique | join(",")' \
    "$work/apply-report.json")" replayed
assert_equal request_delta "$(scalar "
  SELECT count(*) FROM memory.relational_operation_request
  WHERE owner_user_id='$target_owner'::uuid")" "$((before_requests + 2))"
assert_equal entities_unchanged "$(scalar "
  SELECT count(*) FROM memory.entity
  WHERE owner_user_id='$target_owner'::uuid")" "$before_entities"
assert_equal reviews_unchanged "$(scalar "
  SELECT count(*) FROM memory.entity_resolution_review
  WHERE owner_user_id='$target_owner'::uuid")" "$before_reviews"
assert_equal apply_delta "$(scalar "
  SELECT count(*) FROM memory.entity_resolution_apply
  WHERE owner_user_id='$target_owner'::uuid")" "$((before_applies + 2))"
assert_equal binding_delta "$(scalar "
  SELECT count(*) FROM memory.observation_entity_binding
  WHERE owner_user_id='$target_owner'::uuid")" "$((before_bindings + 2))"
assert_equal claims_unchanged "$(scalar "
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$target_owner'::uuid")" "$before_claims"
assert_equal projections_unchanged "$(scalar "
  SELECT count(*) FROM memory.projection_plan
  WHERE owner_user_id='$target_owner'::uuid")" "$before_projections"
assert_equal exact_applies "$(scalar "
  SELECT count(*) FROM memory.entity_resolution_apply
  WHERE owner_user_id='$target_owner'::uuid
    AND resolution_id IN ('$resolution_one'::uuid,'$resolution_two'::uuid)")" 2
assert_equal exact_bindings "$(scalar "
  SELECT count(*) FROM memory.observation_entity_binding AS binding
  JOIN memory.observation AS observation
    USING(owner_user_id,observation_id)
  WHERE binding.owner_user_id='$target_owner'::uuid
    AND observation.evidence_id IN (
      '$evidence_one'::uuid,'$evidence_two'::uuid
    )")" 2

assert_equal qdrant_unchanged "$(qdrant_signature)" "$qdrant_before"
assert_equal production_rows_unchanged \
  "$(production_signature)" "$production_before"
assert_equal production_head_unchanged \
  "$(git -C /opt/chat-memory rev-parse HEAD)" "$production_head_before"
assert_equal brains_service "$(systemctl is-active brains.service)" active

printf '%s\n' \
  'MEMORY_V1_V5_2_MIXED_SELF_ENTITY_APPLY_CLONE=PASS' \
  'item_count=2' \
  'database_rows_created=6' \
  'bindings_created=2' \
  'entity_rows_created=0' \
  'review_rows_created=0' \
  'claim_rows_created=0' \
  'production_writes=0' \
  'qdrant_writes=0' \
  'cross_owner_plan_rejected=true' \
  'hard_stop=before_candidate_generation_claims_projection_or_retrieval'
