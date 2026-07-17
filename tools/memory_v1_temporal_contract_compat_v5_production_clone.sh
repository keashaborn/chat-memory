#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_TEMPORAL_COMPAT_CLONE_PORT:-55442}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1temporalcompatclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
migration=ops/sql/20260717_memory_v1_temporal_contract_compat_v5.sql
rollback=ops/sql/20260717_memory_v1_temporal_contract_compat_v5_rollback.sql
runner=scripts/memory_v1_v5_stage_batch.py
fixture=tests/memory_v1_v5_stage_batch_fixture.py
manifest=/home/ubuntu/memory-v1-reviews/v5-10-v5-13-stage-batch-manifest-d9596ee.json
review_root=/home/ubuntu/memory-v1-reviews
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
backup=$(mktemp /tmp/memory-v1-temporal-compat.XXXXXX.dump)
work=$(mktemp -d /tmp/memory-v1-temporal-compat.XXXXXX)
plan="$work/plan.json"
authorization="$work/authorization.json"
report="$work/apply-report.json"
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

other_owner_signature() {
  scalar "
    SELECT encode(digest(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
      'sha256'),'hex')
    FROM (
      SELECT 'mention|'||to_jsonb(t)::text AS row_json
        FROM memory.entity_mention t WHERE owner_user_id<>'${owner}'::uuid
      UNION ALL SELECT 'resolution|'||to_jsonb(t)::text
        FROM memory.entity_resolution_plan t WHERE owner_user_id<>'${owner}'::uuid
      UNION ALL SELECT 'candidate|'||to_jsonb(t)::text
        FROM memory.entity_resolution_candidate t WHERE owner_user_id<>'${owner}'::uuid
      UNION ALL SELECT 'observation|'||to_jsonb(t)::text
        FROM memory.observation t WHERE owner_user_id<>'${owner}'::uuid
      UNION ALL SELECT 'temporal|'||to_jsonb(t)::text
        FROM memory.observation_temporal t WHERE owner_user_id<>'${owner}'::uuid
      UNION ALL SELECT 'batch|'||to_jsonb(t)::text
        FROM memory.relational_stage_batch t WHERE owner_user_id<>'${owner}'::uuid
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

run_sql <"$migration"
run_sql <"$migration"
[[ "$(scalar "
  SELECT (
    convalidated
    AND pg_get_constraintdef(oid) LIKE '%partial_absolute%'
    AND pg_get_constraintdef(oid) LIKE '%open_interval%'
  )::int
  FROM pg_constraint
  WHERE conrelid='memory.observation_temporal'::regclass
    AND conname='observation_temporal_check'")" == "1" ]]

before_other=$(other_owner_signature)
head=$(git -C "$repo_root" rev-parse HEAD)
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$runner" plan \
  --manifest "$manifest" --review-root "$review_root" --output "$plan"
/opt/chat-memory/venv/bin/python "$fixture" authorize \
  --plan "$plan" --output "$authorization" --head "$head"
MEMORY_V1_V5_STAGE_BATCH_APPLY=authorized POSTGRES_DSN="$dsn" \
  PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$runner" apply \
  --plan "$plan" --authorization "$authorization" \
  --review-root "$review_root" \
  --confirm STAGE_REVIEWED_OWNER_V5_PACKETS_ONLY --output "$report"

[[ "$(jq -r '.database_rows_created' "$report")" == "94" ]]
[[ "$(jq -r '[.applied[].outcome] | sort | join(",")' "$report")" \
  == "applied,applied" ]]
[[ "$(jq -r '[.replayed[].outcome] | sort | join(",")' "$report")" \
  == "replayed,replayed" ]]
[[ "$(jq '[.replayed[].counts[]] | add' "$report")" == "0" ]]
[[ "$(jq -r '.checks.qdrant_calls' "$report")" == "0" ]]

[[ "$(scalar "
  SELECT (
    (SELECT count(*) FROM memory.relational_stage_batch
      WHERE owner_user_id='${owner}'::uuid
        AND evidence_id IN (
          '7877ebf3-1c5a-4bd2-8b43-9514a88a0e12'::uuid,
          'aaaa0d55-0563-4197-8c8e-a657240e6cc1'::uuid))=2
    AND (SELECT count(*) FROM memory.observation
      WHERE owner_user_id='${owner}'::uuid
        AND evidence_id IN (
          '7877ebf3-1c5a-4bd2-8b43-9514a88a0e12'::uuid,
          'aaaa0d55-0563-4197-8c8e-a657240e6cc1'::uuid))=33
    AND (SELECT count(*) FROM memory.observation_temporal temporal
      JOIN memory.observation observation USING(owner_user_id,observation_id)
      WHERE observation.owner_user_id='${owner}'::uuid
        AND observation.evidence_id IN (
          '7877ebf3-1c5a-4bd2-8b43-9514a88a0e12'::uuid,
          'aaaa0d55-0563-4197-8c8e-a657240e6cc1'::uuid))=33
    AND (SELECT count(*) FROM memory.observation_temporal temporal
      JOIN memory.observation observation USING(owner_user_id,observation_id)
      WHERE observation.owner_user_id='${owner}'::uuid
        AND observation.evidence_id IN (
          '7877ebf3-1c5a-4bd2-8b43-9514a88a0e12'::uuid,
          'aaaa0d55-0563-4197-8c8e-a657240e6cc1'::uuid)
        AND temporal.basis='none' AND temporal.semantic<>'none')=2
    AND (SELECT count(*) FROM memory.observation_temporal temporal
      JOIN memory.observation observation USING(owner_user_id,observation_id)
      WHERE observation.owner_user_id='${owner}'::uuid
        AND observation.evidence_id IN (
          '7877ebf3-1c5a-4bd2-8b43-9514a88a0e12'::uuid,
          'aaaa0d55-0563-4197-8c8e-a657240e6cc1'::uuid)
        AND temporal.basis='calendar'
        AND temporal.source_form='partial_absolute'
        AND temporal.anchored_to_source_time)=1
    AND (SELECT count(*) FROM memory.observation_temporal temporal
      JOIN memory.observation observation USING(owner_user_id,observation_id)
      WHERE observation.owner_user_id='${owner}'::uuid
        AND observation.evidence_id IN (
          '7877ebf3-1c5a-4bd2-8b43-9514a88a0e12'::uuid,
          'aaaa0d55-0563-4197-8c8e-a657240e6cc1'::uuid)
        AND temporal.basis='relative'
        AND temporal.shape='open_interval')=1
    AND (SELECT count(*) FROM memory.observation_entity_binding binding
      JOIN memory.observation observation USING(owner_user_id,observation_id)
      WHERE observation.owner_user_id='${owner}'::uuid
        AND observation.evidence_id IN (
          '7877ebf3-1c5a-4bd2-8b43-9514a88a0e12'::uuid,
          'aaaa0d55-0563-4197-8c8e-a657240e6cc1'::uuid))=0
  )::int")" == "1" ]]

after_other=$(other_owner_signature)
[[ "$after_other" == "$before_other" ]]
run_sql <"$migration"
if run_sql <"$rollback" >/dev/null 2>&1; then
  echo "temporal compatibility rollback unexpectedly accepted V5.1 rows" >&2
  exit 1
fi

echo "memory_v1_temporal_contract_compat_v5_production_clone: PASS"
