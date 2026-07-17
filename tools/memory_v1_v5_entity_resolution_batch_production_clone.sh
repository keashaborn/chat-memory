#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_ENTITY_BATCH_CLONE_PORT:-55443}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1entitybatchclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
runner=scripts/memory_v1_v5_entity_resolution_batch.py
fixture=tests/memory_v1_v5_entity_resolution_batch_fixture.py
manifest=/home/ubuntu/memory-v1-reviews/v5-10-v5-13-entity-resolution-batch-manifest.json
review_root=/home/ubuntu/memory-v1-reviews
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
backup=$(mktemp /tmp/memory-v1-entity-batch.XXXXXX.dump)
work=$(mktemp -d "$review_root/.entity-batch-clone.XXXXXX")
authorization="$work/authorization.json"
plan="$work/plan.json"
report="$work/apply-report.json"
baseline="$work/unchanged-before.tsv"
post="$work/unchanged-after.tsv"
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup"
  rm -rf "$work"
}
trap cleanup EXIT
chmod 0600 "$backup"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 -U sage -d memory
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

table_state() {
  local table=$1
  scalar "
    SELECT count(*)::text || E'\\t' ||
           encode(digest(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
             'sha256'),'hex')
    FROM (SELECT to_jsonb(t)::text row_json FROM memory.\"$table\" t) rows"
}

capture_unchanged() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    printf '%s\t%s\n' "$table" "$(table_state "$table")" >>"$output"
  done < <(scalar "
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema='memory' AND table_type='BASE TABLE'
      AND table_name NOT IN (
        'entity','entity_alias_observation','entity_resolution_review',
        'entity_resolution_apply','observation_entity_binding',
        'relational_operation_request'
      )
    ORDER BY table_name")
  chmod 0600 "$output"
}

other_owner_signature() {
  scalar "
    SELECT encode(digest(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
      'sha256'),'hex')
    FROM (
      SELECT 'entity|'||to_jsonb(t)::text row_json FROM memory.entity t
        WHERE owner_user_id<>'${owner}'::uuid
      UNION ALL SELECT 'alias|'||to_jsonb(t)::text
        FROM memory.entity_alias_observation t WHERE owner_user_id<>'${owner}'::uuid
      UNION ALL SELECT 'review|'||to_jsonb(t)::text
        FROM memory.entity_resolution_review t WHERE owner_user_id<>'${owner}'::uuid
      UNION ALL SELECT 'apply|'||to_jsonb(t)::text
        FROM memory.entity_resolution_apply t WHERE owner_user_id<>'${owner}'::uuid
      UNION ALL SELECT 'binding|'||to_jsonb(t)::text
        FROM memory.observation_entity_binding t WHERE owner_user_id<>'${owner}'::uuid
      UNION ALL SELECT 'request|'||to_jsonb(t)::text
        FROM memory.relational_operation_request t WHERE owner_user_id<>'${owner}'::uuid
    ) rows"
}

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc --no-owner --no-privileges >"$backup"
[[ -s "$backup" ]]
"${compose[@]}" up -d --wait postgres
printf '%s\n' \
  "CREATE ROLE brains_app LOGIN PASSWORD 'clone_only_brains_password' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;" \
  'CREATE ROLE memory_v5_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_trace_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"

run_sql <ops/sql/20260715_memory_v1_relational_staging_v5.sql
run_sql <ops/sql/20260715_memory_v1_relational_writer_v5.sql
run_sql <ops/sql/20260716_memory_v1_self_role_compat_v5.sql
run_sql <ops/sql/20260716_memory_v1_trusted_self_apply_v5.sql
run_sql <ops/sql/20260717_memory_v1_temporal_contract_compat_v5.sql

before_entities=$(scalar "SELECT count(*) FROM memory.entity")
before_reviews=$(scalar "SELECT count(*) FROM memory.entity_resolution_review")
before_applies=$(scalar "SELECT count(*) FROM memory.entity_resolution_apply")
before_aliases=$(scalar "SELECT count(*) FROM memory.entity_alias_observation")
before_bindings=$(scalar "SELECT count(*) FROM memory.observation_entity_binding")
before_requests=$(scalar "SELECT count(*) FROM memory.relational_operation_request")
before_other=$(other_owner_signature)
capture_unchanged "$baseline"

head=$(git -C "$repo_root" rev-parse HEAD)
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$runner" plan \
  --manifest "$manifest" --review-root "$review_root" --output "$plan"
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$fixture" \
  --plan "$plan" --output "$authorization" --head "$head"
MEMORY_V1_V5_ENTITY_RESOLUTION_BATCH_APPLY=authorized POSTGRES_DSN="$dsn" \
  PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$runner" apply \
  --plan "$plan" --authorization "$authorization" \
  --review-root "$review_root" \
  --confirm REVIEW_AND_APPLY_OWNER_V5_ENTITY_RESOLUTIONS_ONLY \
  --output "$report"

[[ "$(jq -r '.database_rows_created' "$report")" == "61" ]]
[[ "$(jq -r '.bindings_created' "$report")" == "21" ]]
[[ "$(jq -r '.item_count' "$report")" == "8" ]]
[[ "$(jq '[.replayed[].bindings_created] | add' "$report")" == "0" ]]
[[ "$(jq -r '[.replayed[].apply_outcome] | unique | join(",")' "$report")" == "replayed" ]]
[[ "$(jq -r '.checks.qdrant_calls' "$report")" == "0" ]]

[[ "$(scalar "SELECT count(*) FROM memory.entity")" == "$((before_entities + 6))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.entity_resolution_review")" == "$((before_reviews + 6))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.entity_resolution_apply")" == "$((before_applies + 8))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.entity_alias_observation")" == "$((before_aliases + 6))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.observation_entity_binding")" == "$((before_bindings + 21))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.relational_operation_request")" == "$((before_requests + 14))" ]]
[[ "$(scalar "
  SELECT count(*) FROM memory.entity_resolution_apply
  WHERE owner_user_id='${owner}'::uuid
    AND resolution_id IN (
      'c926122b-9b0f-44e3-aca6-2f65bc882873'::uuid,
      '62fcb028-ee13-4b0c-94ae-81b915ad283b'::uuid,
      'fa0e7209-67c4-4cf3-b808-e8d8c2dc35ef'::uuid
    )")" == "0" ]]
[[ "$(scalar "
  SELECT count(*) FROM memory.entity
  WHERE owner_user_id='${owner}'::uuid
    AND metadata->>'resolution_id' IN (
      'e6fb27aa-1042-4b05-b2cf-d2beca8389cb',
      '4d8a18b5-022c-46b7-8bab-b7a57c2011c0',
      'b32177b9-abb6-41f0-9c65-9db360eaa27e',
      'bb5ee263-dfa6-41d0-aa76-f1ade28be312',
      'e6f39c3a-4140-450b-86b8-cbe24e660d70',
      '1d2f5752-8a46-4113-aa07-4890dfba43a4'
    )")" == "6" ]]

capture_unchanged "$post"
cmp -s "$baseline" "$post"
[[ "$(other_owner_signature)" == "$before_other" ]]

echo "memory_v1_v5_entity_resolution_batch_production_clone: PASS"
