#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Proves the generic transition from a reviewed named
# person to an older deferred family-role mention on a disposable production
# clone. Jerry is the acceptance fixture; no production state is changed.

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_NAMED_FAMILY_ROLE_CLONE_PORT:-55491}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose -p memoryv1namedfamilyroleclone
  -f docker-compose.ci.yml -f docker-compose.stage-batch-clone.yml
)
backup=$(mktemp /tmp/memory-v1-named-family-role.XXXXXX.dump)
roles=$(mktemp /tmp/memory-v1-named-family-role-roles.XXXXXX.sql)
review_dir=$(mktemp -d /home/ubuntu/memory-v1-reviews/named-family-role-clone.XXXXXX)

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
      'entity_alias_observation',(SELECT count(*) FROM memory.entity_alias_observation WHERE owner_user_id='$owner'::uuid),
      'entity_resolution_plan',(SELECT count(*) FROM memory.entity_resolution_plan WHERE owner_user_id='$owner'::uuid),
      'entity_resolution_candidate',(SELECT count(*) FROM memory.entity_resolution_candidate WHERE owner_user_id='$owner'::uuid),
      'entity_resolution_reconciliation_v5_2',(SELECT count(*) FROM memory.entity_resolution_reconciliation_v5_2 WHERE owner_user_id='$owner'::uuid),
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
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef

PYTHONPATH="$repo_root/scripts" /opt/chat-memory/venv/bin/python \
  "$repo_root/tests/test_memory_v1_role_only_family_link_existing_v5_1.py"

before=$(owner_state "$owner")
other_before=$(owner_state "$other")

named_manifest="$review_dir/named-manifest.json"
named_plan="$review_dir/named-plan.json"
named_auth="$review_dir/named-authorization.json"
named_report="$review_dir/named-report.json"
jq -n --arg owner "$owner" '{
  contract_version:"memory_v1_v5_2_entity_resolution_batch_manifest_v1",
  target_server:"seebx", owner_user_id:$owner,
  expected_total_bindings:2, expected_new_rows:10,
  items:[{
    resolution_id:"c20eef21-eef8-44c0-ba70-b14662502638",
    operation:"reconcile_existing_and_apply",
    expected_action:"create_new",
    expected_decision_state:"manual_review_required",
    expected_entity_id:"3cf07024-5b6b-4ae0-b453-b6cae4860720",
    successor_resolution_id:"29342d99-85cd-4e20-9e6f-934b23e5b67c",
    review_reason:"Reconcile the reviewed named family member to the unique owner-scoped family-role entity and bind only its staged observations."
  }]
}' >"$named_manifest"
chmod 0600 "$named_manifest"

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python \
  "$repo_root/scripts/memory_v1_v5_2_entity_resolution_batch.py" plan \
  --manifest "$named_manifest" --output "$named_plan" --review-root "$review_dir"
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python \
  "$repo_root/tests/memory_v1_v5_2_entity_resolution_batch_fixture.py" \
  --plan "$named_plan" --output "$named_auth" --head "$head"
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
MEMORY_V1_V5_2_ENTITY_RESOLUTION_BATCH_APPLY=authorized \
  /opt/chat-memory/venv/bin/python \
  "$repo_root/scripts/memory_v1_v5_2_entity_resolution_batch.py" apply \
  --plan "$named_plan" --authorization "$named_auth" --output "$named_report" \
  --review-root "$review_dir" \
  --confirm RECONCILE_REVIEW_AND_APPLY_OWNER_V5_2_ENTITY_RESOLUTIONS_ONLY

[[ "$(jq -er '.database_rows_created' "$named_report")" == 10 ]]
[[ "$(jq -er '.bindings_created' "$named_report")" == 2 ]]
jerry_entity=$(jq -er '.applied[0].applied_entity_id' "$named_report")
uuid_pattern='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
[[ "$jerry_entity" =~ $uuid_pattern ]]

after_named=$(owner_state "$owner")
BEFORE="$before" AFTER="$after_named" python3 - <<'PY'
import json, os
before, after = json.loads(os.environ["BEFORE"]), json.loads(os.environ["AFTER"])
expected = {
    "entity": 0,
    "entity_alias_observation": 1,
    "entity_resolution_plan": 1,
    "entity_resolution_candidate": 1,
    "entity_resolution_reconciliation_v5_2": 1,
    "entity_resolution_review": 1,
    "entity_resolution_apply": 1,
    "entity_role_resolution_v5_1": 0,
    "observation_entity_binding": 2,
    "relational_operation_request": 2,
}
for table, delta in expected.items():
    if after[table] - before[table] != delta:
        raise SystemExit(f"unexpected named-person {table} delta")
PY
[[ "$(owner_state "$other")" == "$other_before" ]]

role_manifest="$review_dir/role-manifest.json"
role_preflight="$review_dir/role-preflight.json"
role_apply="$review_dir/role-apply.json"
role_replay="$review_dir/role-replay.json"
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root/scripts" \
  /opt/chat-memory/venv/bin/python \
  "$repo_root/scripts/memory_v1_role_only_family_manifest_v5_1.py" \
  --owner "$owner" \
  --source-resolution-id fa0e7209-67c4-4cf3-b808-e8d8c2dc35ef \
  --successor-resolution-id 4a6f1e7b-60c8-4a72-873c-61d8fdb4e682 \
  --reconcile-request-id 79d3ff99-0e5f-4a1d-a4fb-51ce951e8f83 \
  --review-request-id bd4f73c7-bf4b-465e-8865-c9cba57f8345 \
  --apply-request-id 38b55ba2-b70d-4a6e-a385-141c86ffbf62 \
  --reconcile-reason 'Reconcile a deferred owner-scoped family role to the unique later reviewed named person with the same role.' \
  --review-reason 'Approve the unique owner-scoped family:father link without creating a duplicate person.' \
  --expected-role family:father --expected-successor-action link_existing \
  --expected-bindings 5 --required-head "$head" --output "$role_manifest"

POSTGRES_DSN="$clone_dsn" MEMORY_V1_REQUIRED_HEAD="$head" \
PYTHONPATH="$repo_root/scripts" /opt/chat-memory/venv/bin/python \
  "$repo_root/scripts/memory_v1_role_only_family_apply_v5_1.py" \
  --mode preflight --manifest "$role_manifest" --output "$role_preflight"
[[ "$(jq -er '.rows_written' "$role_preflight")" == 0 ]]
[[ "$(owner_state "$owner")" == "$after_named" ]]

cross_owner="$review_dir/cross-owner.log"
if PGPASSWORD=clone_only_brains_password psql \
  "postgresql://brains_app@127.0.0.1:${port}/memory" -X -v ON_ERROR_STOP=1 \
  >"$cross_owner" 2>&1 <<'SQL'
BEGIN READ ONLY;
SELECT set_config('app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true);
SELECT * FROM memory.preflight_role_only_family_resolution_v5_1(
  'fa0e7209-67c4-4cf3-b808-e8d8c2dc35ef',
  '4a6f1e7b-60c8-4a72-873c-61d8fdb4e682',
  'Cross-owner access must fail.'
);
ROLLBACK;
SQL
then
  echo 'cross-owner family-role reconciliation was accepted' >&2
  exit 1
fi
rg -q 'owner-scoped deferred role-only resolution not found' "$cross_owner"

POSTGRES_DSN="$clone_dsn" MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_ROLE_ONLY_FAMILY_APPLY=authorized PYTHONPATH="$repo_root/scripts" \
  /opt/chat-memory/venv/bin/python \
  "$repo_root/scripts/memory_v1_role_only_family_apply_v5_1.py" \
  --mode apply --manifest "$role_manifest" --output "$role_apply"
[[ "$(jq -er '.rows_written' "$role_apply")" == 12 ]]
[[ "$(jq -er '.bindings_created' "$role_apply")" == 5 ]]
[[ "$(jq -er '.applied_entity_id' "$role_apply")" == "$jerry_entity" ]]

after_role=$(owner_state "$owner")
BEFORE="$after_named" AFTER="$after_role" python3 - <<'PY'
import json, os
before, after = json.loads(os.environ["BEFORE"]), json.loads(os.environ["AFTER"])
expected = {
    "entity": 0,
    "entity_alias_observation": 0,
    "entity_resolution_plan": 1,
    "entity_resolution_candidate": 1,
    "entity_resolution_reconciliation_v5_2": 0,
    "entity_resolution_review": 1,
    "entity_resolution_apply": 1,
    "entity_role_resolution_v5_1": 1,
    "observation_entity_binding": 5,
    "relational_operation_request": 2,
}
for table, delta in expected.items():
    if after[table] - before[table] != delta:
        raise SystemExit(f"unexpected role-link {table} delta")
PY

POSTGRES_DSN="$clone_dsn" MEMORY_V1_REQUIRED_HEAD="$head" \
PYTHONPATH="$repo_root/scripts" /opt/chat-memory/venv/bin/python \
  "$repo_root/scripts/memory_v1_role_only_family_apply_v5_1.py" \
  --mode replay --manifest "$role_manifest" --apply-result "$role_apply" \
  --output "$role_replay"
[[ "$(jq -er '.rows_written' "$role_replay")" == 0 ]]
[[ "$(jq -er '.outcome' "$role_replay")" == replayed ]]
[[ "$(owner_state "$owner")" == "$after_role" ]]
[[ "$(owner_state "$other")" == "$other_before" ]]

[[ "$(scalar "SELECT count(*) FROM memory.entity_alias_observation WHERE owner_user_id='$owner'::uuid AND entity_id='$jerry_entity'::uuid AND normalized_alias='jerry'")" == 1 ]]
[[ "$(scalar "SELECT count(*) FROM memory.observation_entity_binding WHERE owner_user_id='$owner'::uuid AND subject_entity_id='$jerry_entity'::uuid AND observation_id IN ('a20409d1-9f60-4916-8597-76a834cacd71','34a09222-5aab-43ca-a199-dd120b62657e','08efee4c-d9a1-4d6c-a391-98909a41f021','52db26ab-e553-44df-847b-e9cbf17269a4','6694a6bc-1196-4317-a1bc-99275b6ac642','1bd5f7a1-12d0-4821-b6ad-51f62ec2948d','fbff7592-9fc8-4a2c-a629-a90ec419fde2')")" == 7 ]]
[[ "$(scalar "SELECT count(*) FROM memory.entity_resolution_apply WHERE owner_user_id='$owner'::uuid AND applied_entity_id='$jerry_entity'::uuid AND resolution_id IN ('29342d99-85cd-4e20-9e6f-934b23e5b67c','4a6f1e7b-60c8-4a72-873c-61d8fdb4e682')")" == 2 ]]

echo 'memory_v1_named_family_role_reconciliation_clone: PASS'
