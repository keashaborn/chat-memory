#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_1_ENTITY_BATCH_CLONE_PORT:-55472}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v51entitybatchclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
migration=ops/sql/20260721_memory_v1_entity_resolution_reconciliation_v5_1.sql
base_writer=ops/sql/20260715_memory_v1_relational_writer_v5.sql
trusted_self_apply=ops/sql/20260716_memory_v1_trusted_self_apply_v5.sql
runner=scripts/memory_v1_v5_1_entity_resolution_batch.py
fixture=tests/memory_v1_v5_1_entity_resolution_batch_fixture.py
unit_test=tests/test_memory_v1_v5_1_entity_resolution_batch.py
review_root=/home/ubuntu/memory-v1-reviews
manifest="$review_root/v5_1-family-20260721-39cd8f0/real-family-entity-resolution-manifest.json"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
backup=$(mktemp /tmp/memory-v1-v5-1-entity-batch.XXXXXX.dump)
roles=$(mktemp /tmp/memory-v1-v5-1-entity-batch-roles.XXXXXX.sql)
work=$(mktemp -d "$review_root/.v5-1-entity-batch-clone.XXXXXX")
plan="$work/plan.json"
authorization="$work/authorization.json"
report="$work/apply-report.json"
cross_manifest="$work/cross-owner-manifest.json"
cross_plan="$work/cross-owner-plan.json"
before="$work/before.tsv"
after="$work/after.tsv"
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$roles"
  rm -rf "$work"
}
trap cleanup EXIT
chmod 0600 "$backup" "$roles"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory "$@"
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

capture_memory() {
  local output=$1 table state
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    state=$(scalar "
      SELECT count(*)::text || E'\\t' || encode(public.digest(convert_to(
        coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
        'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value
      ) AS rows")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done < <(scalar "
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name")
  chmod 0600 "$output"
}

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
" >"$roles"
[[ -s "$roles" ]]

"${compose[@]}" up -d --wait postgres
run_sql <"$roles"
printf '%s\n' "ALTER ROLE brains_app PASSWORD 'clone_only_brains_password';" \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"
run_sql <"$base_writer"
run_sql <"$trusted_self_apply"
run_sql <"$migration"
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$unit_test"

capture_memory "$before"
head=$(git -C "$repo_root" rev-parse HEAD)
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$runner" plan \
  --manifest "$manifest" --review-root "$review_root" --output "$plan"
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$fixture" \
  --plan "$plan" --output "$authorization" --head "$head"

jq --arg owner "$other_owner" '.owner_user_id=$owner' "$manifest" \
  >"$cross_manifest"
chmod 0600 "$cross_manifest"
if POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$runner" plan \
  --manifest "$cross_manifest" --review-root "$review_root" \
  --output "$cross_plan" >/dev/null 2>&1; then
  echo 'cross-owner V5.1 plan unexpectedly passed' >&2
  exit 1
fi

if PGPASSWORD=clone_only_brains_password psql "$dsn" -X -v ON_ERROR_STOP=1 \
  -c "SELECT set_config('app.user_id','$owner',false);
      SELECT * FROM memory.preflight_entity_resolution_apply_v5_1(
        '76f10e7e-b370-4edf-ba73-3148d0a6fb81'::uuid,NULL
      );" >/dev/null 2>&1; then
  echo 'legacy V5 resolution unexpectedly crossed the V5.1 wrapper' >&2
  exit 1
fi

MEMORY_V1_V5_1_ENTITY_RESOLUTION_BATCH_APPLY=authorized \
  POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$runner" apply \
  --plan "$plan" --authorization "$authorization" \
  --review-root "$review_root" \
  --confirm RECONCILE_REVIEW_AND_APPLY_OWNER_V5_1_ENTITY_RESOLUTIONS_ONLY \
  --output "$report"

[[ "$(jq -r '.database_rows_created' "$report")" == 15 ]]
[[ "$(jq -r '.bindings_created' "$report")" == 3 ]]
[[ "$(jq -r '.item_count' "$report")" == 3 ]]
[[ "$(jq -r '[.replayed[].apply_outcome] | unique | join(",")' "$report")" \
  == replayed ]]
[[ "$(jq -r '[.replayed[].reconciliation_outcome // empty] | unique | join(",")' "$report")" \
  == replayed ]]

[[ "$(scalar "SELECT count(*) FROM memory.entity_resolution_reconciliation_v5_1
  WHERE owner_user_id='$owner'::uuid")" == 1 ]]
[[ "$(scalar "SELECT count(*) FROM memory.entity_resolution_plan
  WHERE owner_user_id='$owner'::uuid
    AND resolution_id='227c428d-aacf-5a8b-b938-28efed348860'::uuid
    AND predicate_registry_version='memory_predicate_registry_v5_1'
    AND action='link_existing' AND decision_state='manual_review_required'
    AND selected_entity_id='3cf07024-5b6b-4ae0-b453-b6cae4860720'::uuid")" == 1 ]]
[[ "$(scalar "SELECT count(*) FROM memory.entity_resolution_apply
  WHERE owner_user_id='$owner'::uuid
    AND resolution_id IN (
      'af73eab2-c33c-4dd5-befb-75c78c197825'::uuid,
      'ef71a6f2-4e5b-4afb-bf6b-17e0b5c40125'::uuid,
      '227c428d-aacf-5a8b-b938-28efed348860'::uuid
    )")" == 3 ]]
[[ "$(scalar "SELECT count(*) FROM memory.observation_entity_binding
  WHERE owner_user_id='$owner'::uuid
    AND observation_id IN (
      '2d797843-b0c4-43fb-a03c-fc4beb658058'::uuid,
      '5954f2cb-0229-4dd5-bf23-0a98e860c1ec'::uuid,
      'a6d021e2-5d7d-4344-a355-2af15bb10fef'::uuid
    )")" == 3 ]]
[[ "$(scalar "SELECT count(*) FROM memory.entity
  WHERE owner_user_id='$owner'::uuid")" == "$(docker exec brains-postgres-1 psql -X -A -t -U sage -d memory -c "SELECT count(*) FROM memory.entity WHERE owner_user_id='$owner'::uuid")" ]]
[[ "$(scalar "SELECT count(*) FROM memory.entity_resolution_reconciliation_v5_1
  WHERE owner_user_id<>'$owner'::uuid")" == 0 ]]

capture_memory "$after"
BEFORE="$before" AFTER="$after" python3 - <<'PY'
import os
from pathlib import Path

def load(path):
    rows = {}
    for line in Path(path).read_text().splitlines():
        table, count, digest = line.split("\t")
        rows[table] = (int(count), digest)
    return rows

before = load(os.environ["BEFORE"])
after = load(os.environ["AFTER"])
expected = {
    "entity_resolution_reconciliation_v5_1": 1,
    "entity_resolution_plan": 1,
    "entity_resolution_candidate": 1,
    "entity_resolution_review": 1,
    "entity_resolution_apply": 3,
    "entity_alias_observation": 1,
    "observation_entity_binding": 3,
    "relational_operation_request": 4,
}
for table, prior in before.items():
    current = after[table]
    delta = current[0] - prior[0]
    wanted = expected.get(table, 0)
    if delta != wanted:
        raise SystemExit(f"unexpected row delta {table}: {delta} != {wanted}")
    if wanted == 0 and current[1] != prior[1]:
        raise SystemExit(f"unexpected mutation in {table}")
if sum(expected.values()) != 15:
    raise SystemExit("test row budget is inconsistent")
PY

echo 'memory_v1_v5_1_entity_resolution_batch_production_clone: PASS'
