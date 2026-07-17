#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_STAGE_BATCH_CLONE_PORT:-55439}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v5stagebatchclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
staging_migration=ops/sql/20260715_memory_v1_relational_staging_v5.sql
writer_migration=ops/sql/20260715_memory_v1_relational_writer_v5.sql
preflight_migration=ops/sql/20260716_memory_v1_v5_stage_preflight_api.sql
preflight_rollback=ops/sql/20260716_memory_v1_v5_stage_preflight_api_rollback.sql
preflight_test=tests/memory_v1_v5_stage_preflight_api.sql
seed_sql=tests/memory_v1_v5_stage_batch_seed.sql
runner=scripts/memory_v1_v5_stage_batch.py
fixture=tests/memory_v1_v5_stage_batch_fixture.py
unit_test=tests/test_memory_v1_v5_stage_batch.py
backup=$(mktemp /tmp/memory-v1-stage-batch.XXXXXX.dump)
work=$(mktemp -d /tmp/memory-v1-stage-batch.XXXXXX)
reviews="$work/reviews"
plan="$reviews/plan.json"
authorization="$reviews/authorization.json"
report="$reviews/apply-report.json"
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup"
  rm -rf "$work"
}
trap cleanup EXIT
chmod 0600 "$backup"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
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
run_sql <"$staging_migration"
run_sql <"$writer_migration"
run_sql <"$staging_migration"
run_sql <"$writer_migration"
run_sql <"$preflight_migration"
run_sql <"$preflight_migration"
run_sql <"$preflight_test"
run_sql <"$seed_sql"

PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$unit_test"
/opt/chat-memory/venv/bin/python "$fixture" prepare --review-root "$reviews"

head=$(git -C "$repo_root" rev-parse HEAD)
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$runner" plan \
  --manifest "$reviews/manifest.json" \
  --review-root "$reviews" \
  --output "$plan"

if POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$runner" plan \
  --manifest "$reviews/cross-owner-manifest.json" \
  --review-root "$reviews" \
  --output "$reviews/cross-owner-plan.json" >/dev/null 2>&1; then
  echo "cross-owner bundle unexpectedly passed plan validation" >&2
  exit 1
fi
[[ "$(scalar "
  SELECT count(*) FROM memory.relational_stage_batch
  WHERE owner_user_id IN (
    '11111111-1111-4111-8111-111111111111'::uuid,
    '22222222-2222-4222-8222-222222222222'::uuid
  )")" == "0" ]]

/opt/chat-memory/venv/bin/python "$fixture" authorize \
  --plan "$plan" --output "$authorization" --head "$head"

cp "$reviews/owner-a-name.json" "$reviews/owner-a-name.json.original"
printf ' ' >>"$reviews/owner-a-name.json"
if MEMORY_V1_V5_STAGE_BATCH_APPLY=authorized POSTGRES_DSN="$dsn" \
  PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$runner" apply \
  --plan "$plan" \
  --authorization "$authorization" \
  --review-root "$reviews" \
  --confirm STAGE_REVIEWED_OWNER_V5_PACKETS_ONLY \
  --output "$reviews/tampered-apply.json" >/dev/null 2>&1; then
  echo "tampered bundle unexpectedly passed apply validation" >&2
  exit 1
fi
mv "$reviews/owner-a-name.json.original" "$reviews/owner-a-name.json"
[[ "$(scalar "
  SELECT count(*) FROM memory.relational_stage_batch
  WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid")" == "0" ]]

MEMORY_V1_V5_STAGE_BATCH_APPLY=authorized POSTGRES_DSN="$dsn" \
  PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$runner" apply \
  --plan "$plan" \
  --authorization "$authorization" \
  --review-root "$reviews" \
  --confirm STAGE_REVIEWED_OWNER_V5_PACKETS_ONLY \
  --output "$report"

[[ "$(jq -r '.database_rows_created' "$report")" == "9" ]]
[[ "$(jq -r '[.applied[].outcome] | sort | join(",")' "$report")" \
    == "applied,applied" ]]
[[ "$(jq -r '[.replayed[].outcome] | sort | join(",")' "$report")" \
    == "replayed,replayed" ]]
[[ "$(jq '[.replayed[].counts[]] | add' "$report")" == "0" ]]

[[ "$(scalar "
  SELECT (
    (SELECT count(*) FROM memory.relational_stage_batch
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=2
    AND (SELECT count(*) FROM memory.relational_operation_request
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid
        AND operation='stage_packet')=2
    AND (SELECT count(*) FROM memory.entity_mention
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=1
    AND (SELECT count(*) FROM memory.entity_resolution_plan
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=1
    AND (SELECT count(*) FROM memory.entity_resolution_candidate
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=1
    AND (SELECT count(*) FROM memory.observation
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=1
    AND (SELECT count(*) FROM memory.observation_temporal
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=1
    AND (SELECT count(*) FROM memory.entity_resolution_apply
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=0
    AND (SELECT count(*) FROM memory.observation_entity_binding
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=0
    AND (SELECT count(*) FROM memory.projection_plan
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=0
    AND (SELECT count(*) FROM memory.entity
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=1
    AND (SELECT count(*) FROM memory.relational_stage_batch
      WHERE owner_user_id='22222222-2222-4222-8222-222222222222'::uuid)=0
    AND (SELECT count(*) FROM memory.relational_operation_request
      WHERE owner_user_id='22222222-2222-4222-8222-222222222222'::uuid)=0
    AND (SELECT count(*) FROM memory.entity
      WHERE owner_user_id='22222222-2222-4222-8222-222222222222'::uuid)=1
    AND (SELECT count(*) FROM memory.relational_stage_batch
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid
        AND evidence_id='aeeeeeee-1111-4111-8111-111111111111'::uuid
        AND mention_count=0 AND observation_count=0)=1
  )::int")" == "1" ]]

run_sql <"$preflight_rollback"
[[ "$(scalar "
  SELECT (
    to_regprocedure(
      'memory.preflight_relational_stage_bundle_v5(uuid,text,text,timestamptz)'
    ) IS NULL
    AND (SELECT count(*) FROM memory.relational_stage_batch
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=2
  )::int")" == "1" ]]

echo "memory_v1_v5_stage_batch_production_clone: PASS"
