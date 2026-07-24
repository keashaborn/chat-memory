#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores current production into a disposable clone and
# applies one exact, reviewed V5.2 entity-resolution manifest there.

if [[ "$#" -ne 2 ]]; then
  echo 'usage: memory_v1_v5_2_entity_resolution_exact_production_clone.sh MANIFEST REPORT' >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel)
manifest=$(realpath "$1")
clone_report=$(realpath -m "$2")
approved_root=/home/ubuntu/memory-v1-reviews
target_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
evidence_id=22bd0732-3539-4180-8f89-8f84114131c0
resolution_id=bc18362b-a660-41b2-953b-a2068e0ca923
port=${MEMORY_V1_V5_2_ENTITY_EXACT_CLONE_PORT:-55494}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v52entityexactclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
runner=scripts/memory_v1_v5_2_entity_resolution_batch.py
fixture=tests/memory_v1_v5_2_entity_resolution_batch_fixture.py
unit_test=tests/test_memory_v1_v5_2_entity_resolution_batch.py
backup=$(mktemp /tmp/memory-v1-v5-2-entity-exact.XXXXXX.dump)
role_sql=$(mktemp /tmp/memory-v1-v5-2-entity-exact-roles.XXXXXX.sql)
work=$(mktemp -d /tmp/memory-v1-v5-2-entity-exact.XXXXXX)
reviews="$work/reviews"
plan="$reviews/plan.json"
authorization="$reviews/authorization.json"
apply_report="$reviews/apply-report.json"
cross_manifest="$reviews/cross-owner-manifest.json"
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$role_sql"
  rm -rf "$work"
}
trap cleanup EXIT

[[ "$manifest" == "$approved_root"/* ]]
[[ -f "$manifest" && "$(stat -c '%a' "$manifest")" == 600 ]]
[[ "$clone_report" == "$approved_root"/* && ! -e "$clone_report" ]]
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
[[ "$(jq -er '.contract_version' "$manifest")" == \
  memory_v1_v5_2_entity_resolution_batch_manifest_v1 ]]
[[ "$(jq -er '.target_server' "$manifest")" == seebx ]]
[[ "$(jq -er '.owner_user_id' "$manifest")" == "$target_owner" ]]
[[ "$(jq -er '.expected_total_bindings' "$manifest")" == 4 ]]
[[ "$(jq -er '.expected_new_rows' "$manifest")" == 6 ]]
[[ "$(jq -er '.items | length' "$manifest")" == 1 ]]
[[ "$(jq -er '.items[0].resolution_id' "$manifest")" == "$resolution_id" ]]
[[ "$(jq -er '.items[0].operation' "$manifest")" == auto_apply ]]

chmod 0600 "$backup" "$role_sql"
mkdir -m 0700 "$reviews"
cp "$manifest" "$reviews/manifest.json"
chmod 0600 "$reviews/manifest.json"

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
      'entities',(SELECT count(*) FROM memory.entity),
      'reviews',(SELECT count(*) FROM memory.entity_resolution_review),
      'applies',(SELECT count(*) FROM memory.entity_resolution_apply),
      'bindings',(SELECT count(*) FROM memory.observation_entity_binding),
      'claims',(SELECT count(*) FROM memory.claim),
      'projection_plans',(SELECT count(*) FROM memory.projection_plan)
    )::text)"
}

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
  "ALTER ROLE brains_app PASSWORD 'clone_only_brains_password';" \
  | run_sql
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

head=$(git -C "$repo_root" rev-parse HEAD)
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$runner" plan \
  --manifest "$reviews/manifest.json" --review-root "$reviews" \
  --output "$plan"

jq --arg owner "$other_owner" '.owner_user_id=$owner' \
  "$reviews/manifest.json" >"$cross_manifest"
chmod 0600 "$cross_manifest"
if POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$runner" plan \
  --manifest "$cross_manifest" --review-root "$reviews" \
  --output "$reviews/cross-owner-plan.json" >/dev/null 2>&1; then
  echo 'cross-owner exact V5.2 entity plan unexpectedly passed' >&2
  exit 1
fi

/opt/chat-memory/venv/bin/python "$fixture" \
  --plan "$plan" --output "$authorization" --head "$head"
MEMORY_V1_V5_2_ENTITY_RESOLUTION_BATCH_APPLY=authorized \
  POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$runner" apply \
  --plan "$plan" --authorization "$authorization" \
  --review-root "$reviews" \
  --confirm RECONCILE_REVIEW_AND_APPLY_OWNER_V5_2_ENTITY_RESOLUTIONS_ONLY \
  --output "$apply_report"

[[ "$(jq -er '.database_rows_created' "$apply_report")" == 6 ]]
[[ "$(jq -er '.bindings_created' "$apply_report")" == 4 ]]
[[ "$(jq -er '.item_count' "$apply_report")" == 1 ]]
[[ "$(jq -er '.applied[0].operation' "$apply_report")" == auto_apply ]]
[[ "$(jq -er '.applied[0].review_id == null' "$apply_report")" == true ]]
[[ "$(jq -er '.applied[0].bindings_created' "$apply_report")" == 4 ]]
[[ "$(jq -er '.replayed[0].apply_outcome' "$apply_report")" == replayed ]]
[[ "$(jq -er '.replayed[0].bindings_created' "$apply_report")" == 0 ]]
[[ "$(jq -er '.checks.apply_replay_rows_written' "$apply_report")" == 0 ]]
[[ "$(jq -er '.checks.external_model_calls' "$apply_report")" == 0 ]]
[[ "$(jq -er '.checks.qdrant_calls' "$apply_report")" == 0 ]]

[[ "$(scalar "SELECT count(*) FROM memory.relational_operation_request
  WHERE owner_user_id='$target_owner'::uuid")" == "$((before_requests + 1))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.entity
  WHERE owner_user_id='$target_owner'::uuid")" == "$before_entities" ]]
[[ "$(scalar "SELECT count(*) FROM memory.entity_resolution_review
  WHERE owner_user_id='$target_owner'::uuid")" == "$before_reviews" ]]
[[ "$(scalar "SELECT count(*) FROM memory.entity_resolution_apply
  WHERE owner_user_id='$target_owner'::uuid")" == "$((before_applies + 1))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.observation_entity_binding
  WHERE owner_user_id='$target_owner'::uuid")" == "$((before_bindings + 4))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$target_owner'::uuid")" == "$before_claims" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_plan
  WHERE owner_user_id='$target_owner'::uuid")" == "$before_projections" ]]
[[ "$(scalar "SELECT count(*) FROM memory.entity_resolution_apply
  WHERE owner_user_id='$target_owner'::uuid
    AND resolution_id='$resolution_id'::uuid")" == 1 ]]
[[ "$(scalar "SELECT count(*) FROM memory.observation_entity_binding AS binding
  JOIN memory.observation AS observation
    ON observation.owner_user_id=binding.owner_user_id
   AND observation.observation_id=binding.observation_id
  WHERE binding.owner_user_id='$target_owner'::uuid
    AND observation.evidence_id='$evidence_id'::uuid")" == 4 ]]

applied_entity=$(jq -er '.applied[0].applied_entity_id' "$apply_report")
[[ "$(scalar "SELECT count(*) FROM memory.entity
  WHERE owner_user_id='$target_owner'::uuid
    AND entity_id='$applied_entity'::uuid
    AND entity_type='self' AND status='active'")" == 1 ]]

[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(production_signature)" == "$production_before" ]]
[[ "$(git -C /opt/chat-memory rev-parse HEAD)" == "$production_head_before" ]]

plan_sha=$(sha256sum "$plan" | awk '{print $1}')
apply_sha=$(sha256sum "$apply_report" | awk '{print $1}')
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$head" \
  --arg owner_user_id "$target_owner" \
  --arg resolution_id "$resolution_id" \
  --arg applied_entity_id "$applied_entity" \
  --arg plan_sha256 "$plan_sha" \
  --arg apply_report_sha256 "$apply_sha" \
  --arg qdrant_sha256 "$qdrant_before" \
  '{contract_version:"memory_v1_v5_2_entity_resolution_exact_clone_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    owner_user_id:$owner_user_id,resolution_id:$resolution_id,
    applied_entity_id:$applied_entity_id,database_rows_created:6,
    bindings_created:4,plan_sha256:$plan_sha256,
    apply_report_sha256:$apply_report_sha256,
    checks:{production_clone:true,owner_self_link:true,
      cross_owner_rejected:true,zero_write_replay:true,
      no_review_or_entity_creation:true,claims_unchanged:true,
      projection_unchanged:true,production_unchanged:true,
      qdrant_unchanged:true,external_model_calls:0},
    qdrant_sha256:$qdrant_sha256,
    hard_stop:"before_live_entity_resolution_apply"}' >"$clone_report"
chmod 0600 "$clone_report"
sha256sum "$clone_report" >"$clone_report.sha256"
chmod 0600 "$clone_report.sha256"

printf '%s\n' \
  'memory_v1_v5_2_entity_resolution_exact_production_clone: PASS' \
  "report=$clone_report" \
  'clone_rows_created=6' \
  'bindings_created=4' \
  'same_run_replay_rows=0' \
  'production_writes=0' \
  'qdrant_writes=0' \
  'external_model_calls=0'
