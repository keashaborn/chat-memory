#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_RELATIONSHIP_PROJECTION_CLONE_PORT:-55473}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1relationshipprojectionclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
migration=ops/sql/20260721_memory_v1_relationship_claim_projection_v5_1.sql
rollback=ops/sql/20260721_memory_v1_relationship_claim_projection_v5_1_rollback.sql
backup=$(mktemp /tmp/memory-v1-relationship-projection.XXXXXX.dump)
roles=$(mktemp /tmp/memory-v1-relationship-projection-roles.XXXXXX.sql)
review_dir=$(mktemp -d /home/ubuntu/memory-v1-reviews/relationship-projection-clone.XXXXXX)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$roles"
  rm -rf "$review_dir"
}
trap cleanup EXIT
chmod 0600 "$backup" "$roles"
chmod 0700 "$review_dir"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory "$@"
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

app_sql() {
  PGPASSWORD=clone_only_brains_password psql -X -v ON_ERROR_STOP=1 \
    -h 127.0.0.1 -p "$port" -U brains_app -d memory "$@"
}

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc --no-owner >"$backup"
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
" >"$roles"
[[ -s "$roles" ]]

"${compose[@]}" up -d --wait postgres
run_sql <"$roles"
printf '%s\n' "ALTER ROLE brains_app PASSWORD 'clone_only_brains_password';" \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner <"$backup"

run_sql <"$migration"
run_sql <"$migration"
[[ "$(scalar "SELECT pg_get_constraintdef(oid) LIKE '%memory_predicate_registry_v5_1%'
  FROM pg_constraint WHERE conrelid='memory.projection_plan'::regclass
    AND conname='projection_plan_predicate_registry_version_check'")" == t ]]
[[ "$(scalar "SELECT pg_get_constraintdef(oid) LIKE '%memory_predicate_registry_v5_1%'
  FROM pg_constraint WHERE conrelid='memory.projection_plan_item'::regclass
    AND conname='projection_plan_item_predicate_registry_version_check'")" == t ]]
[[ "$(scalar "SELECT has_function_privilege(
  'brains_app','memory.preflight_relationship_claim_source_v5_1(uuid)',
  'EXECUTE')::int")" == 1 ]]
[[ "$(scalar "SELECT has_function_privilege(
  'brains_app','memory.stage_relationship_claim_plan_v5_1(uuid,text,text)',
  'EXECUTE')::int")" == 1 ]]

[[ "$(scalar "
  SELECT count(*) FROM (
    SELECT memory.render_relationship_claim_text_v5_1(
      CASE WHEN (contract#>'{relationship_policy,subject_entity_types}') ? 'self' THEN 'self'
        ELSE contract#>>'{relationship_policy,subject_entity_types,0}' END,
      CASE WHEN (contract#>'{relationship_policy,subject_entity_types}') ? 'self' THEN 'Self'
        ELSE 'Alex' END,
      predicate,
      contract#>>'{relationship_policy,object_entity_types,0}',
      CASE WHEN contract#>>'{relationship_policy,object_entity_types,0}'='animal' THEN 'Koda'
        WHEN contract#>>'{relationship_policy,object_entity_types,0}'='self' THEN 'Self'
        ELSE 'Jordan' END
    ) AS rendered
    FROM memory.predicate_contract
    WHERE registry_version='memory_predicate_registry_v5_1'
      AND (predicate LIKE 'relationship.%' OR predicate LIKE 'social.%')
      AND contract ? 'relationship_policy'
  ) AS rendered
  WHERE btrim(rendered)<>''")" == "$(scalar "SELECT count(*)
    FROM memory.predicate_contract
    WHERE registry_version='memory_predicate_registry_v5_1'
      AND (predicate LIKE 'relationship.%' OR predicate LIKE 'social.%')
      AND contract ? 'relationship_policy'")" ]]

source_output=$(app_sql -X -A -t -F '|' <<'SQL'
BEGIN READ ONLY;
SELECT set_config('app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true);
SELECT predicate,predicate_registry_version,subject_canonical_name,
       object_entity_type,canonical_text
FROM memory.preflight_relationship_claim_source_v5_1(
  '2d797843-b0c4-43fb-a03c-fc4beb658058'::uuid
);
ROLLBACK;
SQL
)
[[ "$source_output" == *"relationship.parent_of|memory_predicate_registry_v5_1|dad|self|dad is the user's parent."* ]]

if app_sql >/dev/null 2>&1 <<'SQL'; then
BEGIN READ ONLY;
SELECT set_config('app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true);
SELECT * FROM memory.preflight_relationship_claim_source_v5_1(
  '2d797843-b0c4-43fb-a03c-fc4beb658058'::uuid
);
ROLLBACK;
SQL
  echo 'cross-owner relationship source was visible' >&2
  exit 1
fi

if app_sql >/dev/null 2>&1 <<'SQL'; then
BEGIN READ ONLY;
SELECT set_config('app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true);
SELECT * FROM memory.preflight_relationship_claim_source_v5_1(
  '5954f2cb-0229-4dd5-bf23-0a98e860c1ec'::uuid
);
ROLLBACK;
SQL
  echo 'health observation entered relationship projection' >&2
  exit 1
fi

run_sql <"$rollback"
[[ "$(scalar "SELECT to_regprocedure(
  'memory.stage_relationship_claim_plan_v5_1(uuid,text,text)') IS NULL")" == t ]]
[[ "$(scalar "SELECT pg_get_constraintdef(oid) NOT LIKE '%memory_predicate_registry_v5_1%'
  FROM pg_constraint WHERE conrelid='memory.projection_plan'::regclass
    AND conname='projection_plan_predicate_registry_version_check'")" == t ]]
run_sql <"$migration"

head=$(git -C "$repo_root" rev-parse HEAD)
plan_id=3bd83fa1-9965-55f6-9a73-493664f94525
bundle="$review_dir/bundle.json"
preflight_result="$review_dir/preflight.json"
apply_result="$review_dir/apply.json"
replay_result="$review_dir/replay.json"
clone_dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:$port/memory"
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root/scripts" \
  python3 "$repo_root/scripts/memory_v1_relationship_claim_bundle_v5_1.py" \
  --owner 1240822d-ac9a-4096-95aa-e2b24d36ef50 \
  --observation-id 2d797843-b0c4-43fb-a03c-fc4beb658058 \
  --plan-id "$plan_id" --required-head "$head" --output "$bundle"
POSTGRES_DSN="$clone_dsn" MEMORY_V1_REQUIRED_HEAD="$head" \
  PYTHONPATH="$repo_root/scripts" \
  python3 "$repo_root/scripts/memory_v1_relationship_claim_stage_v5_1.py" \
  --mode preflight --bundle "$bundle" --output "$preflight_result"
POSTGRES_DSN="$clone_dsn" MEMORY_V1_REQUIRED_HEAD="$head" \
  MEMORY_V1_RELATIONSHIP_CLAIM_STAGE_APPLY=authorized \
  PYTHONPATH="$repo_root/scripts" \
  python3 "$repo_root/scripts/memory_v1_relationship_claim_stage_v5_1.py" \
  --mode apply --bundle "$bundle" --output "$apply_result"
POSTGRES_DSN="$clone_dsn" MEMORY_V1_REQUIRED_HEAD="$head" \
  PYTHONPATH="$repo_root/scripts" \
  python3 "$repo_root/scripts/memory_v1_relationship_claim_stage_v5_1.py" \
  --mode replay --bundle "$bundle" --output "$replay_result"
[[ "$(jq -er '.rows_written' "$preflight_result")" == 0 ]]
[[ "$(jq -er '.rows_written' "$apply_result")" == 4 ]]
[[ "$(jq -er '.rows_written' "$replay_result")" == 0 ]]
[[ "$(scalar "SELECT
  (SELECT count(*) FROM memory.projection_plan WHERE plan_id='$plan_id'::uuid)::text
  || ':' ||
  (SELECT count(*) FROM memory.projection_plan_item WHERE plan_id='$plan_id'::uuid)::text
  || ':' ||
  (SELECT count(*) FROM memory.projection_claim_payload WHERE plan_id='$plan_id'::uuid)::text
  || ':' ||
  (SELECT count(*) FROM memory.projection_plan_observation WHERE plan_id='$plan_id'::uuid)::text
  || ':' ||
  (SELECT count(*) FROM memory.claim WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
     AND predicate='relationship.parent_of')::text")" == '1:1:1:1:0' ]]

echo 'memory_v1_relationship_claim_projection_v5_1_schema_clone: PASS'
