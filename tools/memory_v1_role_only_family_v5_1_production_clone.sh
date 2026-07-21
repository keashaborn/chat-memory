#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Exercises the operational parent-role runner on a
# disposable clone of current production.

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_ROLE_ONLY_FAMILY_APPLY_CLONE_PORT:-55476}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose -p memoryv1rolefamilyapplyclone
  -f docker-compose.ci.yml -f docker-compose.stage-batch-clone.yml
)
backup=$(mktemp /tmp/memory-v1-role-family-apply.XXXXXX.dump)
roles=$(mktemp /tmp/memory-v1-role-family-apply-roles.XXXXXX.sql)
review_dir=$(mktemp -d /home/ubuntu/memory-v1-reviews/role-family-apply-clone.XXXXXX)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$roles"
  rm -rf "$review_dir"
}
trap cleanup EXIT
chmod 0600 "$backup" "$roles"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 -U sage -d memory "$@"
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

owner_state() {
  local owner=$1
  scalar "
    SELECT jsonb_build_object(
      'entity',(SELECT count(*) FROM memory.entity WHERE owner_user_id='$owner'::uuid),
      'entity_resolution_plan',(SELECT count(*) FROM memory.entity_resolution_plan WHERE owner_user_id='$owner'::uuid),
      'entity_resolution_review',(SELECT count(*) FROM memory.entity_resolution_review WHERE owner_user_id='$owner'::uuid),
      'entity_resolution_apply',(SELECT count(*) FROM memory.entity_resolution_apply WHERE owner_user_id='$owner'::uuid),
      'entity_role_resolution_v5_1',(SELECT count(*) FROM memory.entity_role_resolution_v5_1 WHERE owner_user_id='$owner'::uuid),
      'observation_entity_binding',(SELECT count(*) FROM memory.observation_entity_binding WHERE owner_user_id='$owner'::uuid),
      'relational_operation_request',(SELECT count(*) FROM memory.relational_operation_request WHERE owner_user_id='$owner'::uuid)
    )::text"
}

docker exec brains-postgres-1 pg_dump -U sage -d memory -Fc >"$backup"
[[ -s "$backup" ]]
docker exec brains-postgres-1 psql -X -A -t -U sage -d memory -c "
  SELECT format(
    'CREATE ROLE %I %s %s NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;',
    rolname, CASE WHEN rolcanlogin THEN 'LOGIN' ELSE 'NOLOGIN' END,
    CASE WHEN rolinherit THEN 'INHERIT' ELSE 'NOINHERIT' END)
  FROM pg_roles
  WHERE rolname NOT LIKE 'pg\\_%' ESCAPE '\\'
    AND rolname NOT IN ('sage','postgres') ORDER BY rolname
" >"$roles"
[[ -s "$roles" ]]

"${compose[@]}" up -d --wait postgres
run_sql <"$roles"
printf '%s\n' "ALTER ROLE brains_app PASSWORD 'clone_only_brains_password';" | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory --clean --if-exists <"$backup"

clone_dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:$port/memory"
head=$(git -C "$repo_root" rev-parse HEAD)
manifest="$review_dir/manifest.json"
preflight="$review_dir/preflight.json"
apply="$review_dir/apply.json"
replay="$review_dir/replay.json"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root/scripts" \
  /opt/chat-memory/venv/bin/python \
  "$repo_root/scripts/memory_v1_role_only_family_manifest_v5_1.py" \
  --owner "$owner" \
  --source-resolution-id 62fcb028-ee13-4b0c-94ae-81b915ad283b \
  --successor-resolution-id eaf253ec-b34f-46a5-8f8c-a543d2679d4b \
  --reconcile-request-id 58099a0c-409d-4c56-b3e5-3ff400ae4f6c \
  --review-request-id a2918c60-bf1f-4f73-92c0-52d9d724e90e \
  --apply-request-id 733e33f2-fea3-4850-a64f-b1324b4b0e45 \
  --reconcile-reason 'Create the unique reviewed owner-scoped family:mother role entity.' \
  --review-reason 'Approved because family:mother is a closed non-repeatable owner role and no owner-local mother entity exists.' \
  --expected-role family:mother --expected-bindings 4 \
  --required-head "$head" --output "$manifest"

before=$(owner_state "$owner")
other_before=$(owner_state "$other")

POSTGRES_DSN="$clone_dsn" MEMORY_V1_REQUIRED_HEAD="$head" \
PYTHONPATH="$repo_root/scripts" /opt/chat-memory/venv/bin/python \
  "$repo_root/scripts/memory_v1_role_only_family_apply_v5_1.py" \
  --mode preflight --manifest "$manifest" --output "$preflight"
[[ "$(jq -er '.rows_written' "$preflight")" == 0 ]]
[[ "$(owner_state "$owner")" == "$before" ]]
[[ "$(owner_state "$other")" == "$other_before" ]]

cross_owner="$review_dir/cross-owner.log"
if PGPASSWORD=clone_only_brains_password psql \
  "postgresql://brains_app@127.0.0.1:${port}/memory" -X -v ON_ERROR_STOP=1 \
  >"$cross_owner" 2>&1 <<'SQL'
BEGIN READ ONLY;
SELECT set_config('app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true);
SELECT * FROM memory.preflight_role_only_family_resolution_v5_1(
  '62fcb028-ee13-4b0c-94ae-81b915ad283b',
  'eaf253ec-b34f-46a5-8f8c-a543d2679d4b',
  'Cross-owner access must fail.'
);
ROLLBACK;
SQL
then
  echo 'cross-owner parent-role preflight was accepted' >&2
  exit 1
fi
rg -q 'owner-scoped deferred role-only resolution not found' "$cross_owner"

POSTGRES_DSN="$clone_dsn" MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_ROLE_ONLY_FAMILY_APPLY=authorized PYTHONPATH="$repo_root/scripts" \
  /opt/chat-memory/venv/bin/python \
  "$repo_root/scripts/memory_v1_role_only_family_apply_v5_1.py" \
  --mode apply --manifest "$manifest" --output "$apply"
[[ "$(jq -er '.rows_written' "$apply")" == 11 ]]
[[ "$(jq -er '.bindings_created' "$apply")" == 4 ]]

BEFORE="$before" AFTER="$(owner_state "$owner")" python3 - <<'PY'
import json, os
before, after = json.loads(os.environ["BEFORE"]), json.loads(os.environ["AFTER"])
expected = {
    "entity": 1, "entity_resolution_plan": 1,
    "entity_resolution_review": 1, "entity_resolution_apply": 1,
    "entity_role_resolution_v5_1": 1, "observation_entity_binding": 4,
    "relational_operation_request": 2,
}
for table, delta in expected.items():
    if after[table] - before[table] != delta:
        raise SystemExit(f"unexpected {table} delta")
PY
[[ "$(owner_state "$other")" == "$other_before" ]]
after=$(owner_state "$owner")

POSTGRES_DSN="$clone_dsn" MEMORY_V1_REQUIRED_HEAD="$head" \
PYTHONPATH="$repo_root/scripts" /opt/chat-memory/venv/bin/python \
  "$repo_root/scripts/memory_v1_role_only_family_apply_v5_1.py" \
  --mode replay --manifest "$manifest" --apply-result "$apply" --output "$replay"
[[ "$(jq -er '.rows_written' "$replay")" == 0 ]]
[[ "$(jq -er '.outcome' "$replay")" == replayed ]]
[[ "$(owner_state "$owner")" == "$after" ]]
[[ "$(owner_state "$other")" == "$other_before" ]]
[[ "$(scalar "SELECT count(*) FROM memory.observation_entity_binding b JOIN memory.observation o USING(owner_user_id,observation_id) WHERE b.owner_user_id='$owner'::uuid AND o.predicate='life_event.died' AND b.subject_entity_id='$(jq -er '.applied_entity_id' "$apply")'::uuid")" == 1 ]]

echo 'memory_v1_role_only_family_v5_1_production_clone: PASS'
