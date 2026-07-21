#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_1_STAGE_BATCH_CLONE_PORT:-55439}
export MEMORY_V1_V5_1_STAGE_BATCH_CLONE_PORT="$port"
# The shared Compose file intentionally retains its established variable name.
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v51stagebatchclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
staging_migration=ops/sql/20260715_memory_v1_relational_staging_v5.sql
writer_migration=ops/sql/20260715_memory_v1_relational_writer_v5.sql
component_migration=ops/sql/20260717_memory_v1_v5_project_components.sql
preflight_migration=ops/sql/20260716_memory_v1_v5_stage_preflight_api.sql
preflight_rollback=ops/sql/20260716_memory_v1_v5_stage_preflight_api_rollback.sql
preflight_test=tests/memory_v1_v5_stage_preflight_api.sql
source_id_migration=ops/sql/20260718_memory_v1_v5_stage_source_id_compat.sql
source_id_rollback=ops/sql/20260718_memory_v1_v5_stage_source_id_compat_rollback.sql
source_id_test=tests/memory_v1_v5_stage_source_id_compat.sql
v5_1_migration=ops/sql/20260721_memory_v1_relational_stage_v5_1.sql
v5_1_rollback=ops/sql/20260721_memory_v1_relational_stage_v5_1_rollback.sql
v5_1_security_test=tests/memory_v1_v5_1_stage_preflight_api.sql
seed_sql=tests/memory_v1_v5_stage_batch_seed.sql
runner=scripts/memory_v1_v5_1_stage_batch.py
fixture=tests/memory_v1_v5_1_stage_batch_fixture.py
unit_test=tests/test_memory_v1_v5_1_stage_batch.py
review_unit_test=tests/test_memory_v1_v5_1_review_local_packet.py
backup=$(mktemp /tmp/memory-v1-stage-batch.XXXXXX.dump)
role_sql=$(mktemp /tmp/memory-v1-stage-batch-roles.XXXXXX.sql)
work=$(mktemp -d /tmp/memory-v1-stage-batch.XXXXXX)
reviews="$work/reviews"
plan="$reviews/plan.json"
authorization="$reviews/authorization.json"
report="$reviews/apply-report.json"
forged_plan="$reviews/forged-component-plan.json"
forged_authorization="$reviews/forged-component-authorization.json"
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$role_sql"
  rm -rf "$work"
}
trap cleanup EXIT
chmod 0600 "$backup" "$role_sql"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory "$@"
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
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
" >"$role_sql"
[[ -s "$role_sql" ]]
"${compose[@]}" up -d --wait postgres
run_sql <"$role_sql"
printf '%s\n' \
  "ALTER ROLE brains_app PASSWORD 'clone_only_brains_password';" \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"
run_sql <"$staging_migration"
run_sql <"$writer_migration"
run_sql <"$staging_migration"
run_sql <"$writer_migration"
run_sql <"$component_migration"
run_sql <"$component_migration"
run_sql <"$preflight_migration"
run_sql <"$preflight_migration"
run_sql <"$preflight_test"
run_sql <"$source_id_migration"
run_sql <"$source_id_migration"
run_sql <"$v5_1_migration"
run_sql <"$v5_1_migration"
run_sql <"$v5_1_security_test"
run_sql <"$seed_sql"
run_sql \
  -v target_owner=11111111-1111-4111-8111-111111111111 \
  -v other_owner=22222222-2222-4222-8222-222222222222 \
  -v evidence_id=aeeeeeee-1111-4111-8111-111111111112 \
  -v source_id=aeeeeeee-1111-4111-8111-111111111112 \
  -v source_sha256=3f55b194db3ae03b7100b9e8f572ab254f43bed87b151c3a5e0015163374d2aa \
  -v source_recorded_at=2026-07-16T12:01:00Z <"$source_id_test"

PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$unit_test"
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$review_unit_test"
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
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$runner" plan \
  --manifest "$reviews/forged-component-manifest.json" \
  --review-root "$reviews" \
  --output "$forged_plan"
[[ "$(scalar "
  SELECT count(*) FROM memory.relational_stage_batch
  WHERE owner_user_id IN (
    '11111111-1111-4111-8111-111111111111'::uuid,
    '22222222-2222-4222-8222-222222222222'::uuid
  )")" == "0" ]]

/opt/chat-memory/venv/bin/python "$fixture" authorize \
  --plan "$plan" --output "$authorization" --head "$head"
/opt/chat-memory/venv/bin/python "$fixture" authorize \
  --plan "$forged_plan" --output "$forged_authorization" --head "$head"

if MEMORY_V1_V5_1_STAGE_BATCH_APPLY=authorized POSTGRES_DSN="$dsn" \
  PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$runner" apply \
  --plan "$forged_plan" \
  --authorization "$forged_authorization" \
  --review-root "$reviews" \
  --confirm STAGE_REVIEWED_OWNER_V5_1_PACKETS_ONLY \
  --output "$reviews/forged-component-apply.json" >/dev/null 2>&1; then
  echo "cross-owner component unexpectedly passed durable staging" >&2
  exit 1
fi
[[ "$(scalar "
  SELECT (
    (SELECT count(*) FROM memory.relational_stage_batch
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid
        AND evidence_id='aeeeeeee-1111-4111-8111-111111111114'::uuid)=0
    AND (SELECT count(*) FROM memory.relational_operation_request
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid
        AND target_key='aeeeeeee-1111-4111-8111-111111111114')=0
    AND (SELECT count(*) FROM memory.entity_mention
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid
        AND evidence_id='aeeeeeee-1111-4111-8111-111111111114'::uuid)=0
    AND (SELECT count(*) FROM memory.entity_resolution_plan
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid
        AND evidence_id='aeeeeeee-1111-4111-8111-111111111114'::uuid)=0
    AND (SELECT count(*) FROM memory.observation
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid
        AND evidence_id='aeeeeeee-1111-4111-8111-111111111114'::uuid)=0
  )::int")" == "1" ]]

cp "$reviews/owner-a-name.json" "$reviews/owner-a-name.json.original"
printf ' ' >>"$reviews/owner-a-name.json"
if MEMORY_V1_V5_1_STAGE_BATCH_APPLY=authorized POSTGRES_DSN="$dsn" \
  PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$runner" apply \
  --plan "$plan" \
  --authorization "$authorization" \
  --review-root "$reviews" \
  --confirm STAGE_REVIEWED_OWNER_V5_1_PACKETS_ONLY \
  --output "$reviews/tampered-apply.json" >/dev/null 2>&1; then
  echo "tampered bundle unexpectedly passed apply validation" >&2
  exit 1
fi
mv "$reviews/owner-a-name.json.original" "$reviews/owner-a-name.json"
[[ "$(scalar "
  SELECT count(*) FROM memory.relational_stage_batch
  WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid")" == "0" ]]

MEMORY_V1_V5_1_STAGE_BATCH_APPLY=authorized POSTGRES_DSN="$dsn" \
  PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$runner" apply \
  --plan "$plan" \
  --authorization "$authorization" \
  --review-root "$reviews" \
  --confirm STAGE_REVIEWED_OWNER_V5_1_PACKETS_ONLY \
  --output "$report"

[[ "$(jq -r '.database_rows_created' "$report")" == "15" ]]
[[ "$(jq -r '[.applied[].outcome] | sort | join(",")' "$report")" \
    == "applied,applied,applied" ]]
[[ "$(jq -r '[.replayed[].outcome] | sort | join(",")' "$report")" \
    == "replayed,replayed,replayed" ]]
[[ "$(jq '[.replayed[].counts[]] | add' "$report")" == "0" ]]

[[ "$(scalar "
  SELECT (
    (SELECT count(*) FROM memory.relational_stage_batch
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=3
    AND (SELECT count(*) FROM memory.relational_operation_request
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid
        AND operation='stage_packet')=3
    AND (SELECT count(*) FROM memory.entity_mention
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=2
    AND (SELECT count(*) FROM memory.entity_resolution_plan
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=2
    AND (SELECT count(*) FROM memory.entity_resolution_plan
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid
        AND predicate_registry_version='memory_predicate_registry_v5_1')=2
    AND (SELECT count(*) FROM memory.entity_resolution_candidate
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=1
    AND (SELECT count(*) FROM memory.observation
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=2
    AND (SELECT count(*) FROM memory.observation_temporal
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=2
    AND (SELECT count(*) FROM memory.entity_resolution_apply
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=0
    AND (SELECT count(*) FROM memory.observation_entity_binding
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=0
    AND (SELECT count(*) FROM memory.projection_plan
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=0
    AND (SELECT count(*) FROM memory.v5_local_packet_review_artifact
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=0
    AND (SELECT count(*) FROM memory.v5_local_packet_stage_admission
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
    AND (SELECT count(*) FROM memory.entity_mention
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid
        AND evidence_id='aeeeeeee-1111-4111-8111-111111111113'::uuid
        AND entity_type='project'
        AND mention_kind='named'
        AND name_text='Memory V1')=1
    AND (SELECT count(*) FROM memory.entity_resolution_plan
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid
        AND evidence_id='aeeeeeee-1111-4111-8111-111111111113'::uuid
        AND action='defer'
        AND decision_state='deferred')=1
    AND (SELECT count(*) FROM memory.observation
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid
        AND evidence_id='aeeeeeee-1111-4111-8111-111111111113'::uuid
        AND project_scope=jsonb_build_object(
          'state','resolved',
          'project_key','verbal-sage',
          'component_key','memory-v1',
          'binding_source','trusted_component_registry'
        ))=1
  )::int")" == "1" ]]

if [[ -n "${MEMORY_V1_V5_1_FAMILY_MANIFEST:-}" ]]; then
  family_manifest=$(realpath "$MEMORY_V1_V5_1_FAMILY_MANIFEST")
  family_root=$(dirname "$family_manifest")
  family_plan="$family_root/real-family-clone-plan-${head:0:7}.json"
  family_authorization="$family_root/real-family-clone-authorization-${head:0:7}.json"
  family_report="$family_root/real-family-clone-apply-report-${head:0:7}.json"

  POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
    /opt/chat-memory/venv/bin/python "$runner" plan \
    --manifest "$family_manifest" \
    --review-root "$family_root" \
    --output "$family_plan"
  /opt/chat-memory/venv/bin/python "$fixture" authorize \
    --plan "$family_plan" --output "$family_authorization" --head "$head"
  MEMORY_V1_V5_1_STAGE_BATCH_APPLY=authorized POSTGRES_DSN="$dsn" \
    PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$runner" apply \
    --plan "$family_plan" \
    --authorization "$family_authorization" \
    --review-root "$family_root" \
    --confirm STAGE_REVIEWED_OWNER_V5_1_PACKETS_ONLY \
    --output "$family_report"

  [[ "$(jq -r '.bundle_count' "$family_report")" == "2" ]]
  [[ "$(jq -r '.database_rows_created' "$family_report")" == "18" ]]
  [[ "$(jq -r '[.applied[].outcome] | unique | join(",")' "$family_report")" \
      == "applied" ]]
  [[ "$(jq -r '[.replayed[].outcome] | unique | join(",")' "$family_report")" \
      == "replayed" ]]
  [[ "$(jq '[.replayed[].counts[]] | add' "$family_report")" == "0" ]]
  [[ "$(scalar "
    SELECT (
      (SELECT count(*) FROM memory.entity_resolution_plan
        WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
          AND evidence_id IN (
            '1cf82631-7be9-5ea7-8ac5-a6f2ecbede23'::uuid,
            '4c37f741-4dd5-5734-8236-24b670168ba1'::uuid
          )
          AND predicate_registry_version='memory_predicate_registry_v5_1')=3
      AND (SELECT count(*) FROM memory.observation
        WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
          AND evidence_id IN (
            '1cf82631-7be9-5ea7-8ac5-a6f2ecbede23'::uuid,
            '4c37f741-4dd5-5734-8236-24b670168ba1'::uuid
          )
          AND predicate_registry_version='memory_predicate_registry_v5_1')=3
      AND (SELECT count(*) FROM memory.entity_resolution_apply
        WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
          AND resolution_id IN (
            SELECT resolution_id FROM memory.entity_resolution_plan
            WHERE evidence_id IN (
              '1cf82631-7be9-5ea7-8ac5-a6f2ecbede23'::uuid,
              '4c37f741-4dd5-5734-8236-24b670168ba1'::uuid
            )))=0
      AND (SELECT count(*) FROM memory.v5_local_packet_review_artifact
        WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
          AND evidence_id IN (
            '1cf82631-7be9-5ea7-8ac5-a6f2ecbede23'::uuid,
            '4c37f741-4dd5-5734-8236-24b670168ba1'::uuid
          ))=0
      AND (SELECT count(*) FROM memory.v5_local_packet_stage_admission
        WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
          AND evidence_id IN (
            '1cf82631-7be9-5ea7-8ac5-a6f2ecbede23'::uuid,
            '4c37f741-4dd5-5734-8236-24b670168ba1'::uuid
          ))=0
    )::int")" == "1" ]]
fi

run_sql <"$v5_1_rollback"
[[ "$(scalar "
  SELECT (
    to_regprocedure(
      'memory.preflight_relational_stage_bundle_v5_1(uuid,text,text,timestamptz)'
    ) IS NULL
    AND to_regprocedure(
      'memory.preflight_relational_stage_bundle_v5(uuid,text,text,timestamptz)'
    ) IS NOT NULL
    AND (SELECT count(*) FROM memory.relational_stage_batch
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=3
  )::int")" == "1" ]]

echo "memory_v1_v5_1_stage_batch_production_clone: PASS"
