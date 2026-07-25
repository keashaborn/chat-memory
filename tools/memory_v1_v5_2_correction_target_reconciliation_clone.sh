#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores production into a disposable clone, installs the
# correction-target compatibility schema, and exercises the exact Neko
# correction through successor resolution, review, apply, and observation
# binding. Production Postgres and Qdrant are read-only throughout.

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_2_CORRECTION_TARGET_CLONE_PORT:-55508}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v52correctiontargetclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
migration=ops/sql/20260725_memory_v1_v5_2_correction_target_reconciliation.sql
security_test=tests/memory_v1_v5_2_correction_target_reconciliation_security.sql
writer_migration=ops/sql/20260715_memory_v1_relational_writer_v5.sql
backup=$(mktemp /tmp/memory-v1-v5-2-correction-target.XXXXXX.dump)
role_sql=$(mktemp /tmp/memory-v1-v5-2-correction-target-roles.XXXXXX.sql)
work=$(mktemp -d /tmp/memory-v1-v5-2-correction-target.XXXXXX)
report=${1:-"$work/report.json"}
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"

owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
source_resolution=80b0821a-b9a5-41e5-8008-9db72b2ebfe6
successor_resolution=cc52a6bc-1538-58d9-a935-28b09d0ab674
observation=93024235-89a8-49d5-88fa-7e4a143b68f3
target_entity=09308a2b-3019-4f59-8fc3-bb1fe1408a0d
reconcile_request=8c6807dd-277c-57ee-a49d-dd1ad4a85c7c
review_request=0b0871be-2a4b-5bb6-868b-a0c188981c16
apply_request=bf1be5c4-0b63-50d5-bf21-5095589411af
reconcile_reason='Exact owner-local corrected pet name uniquely identifies Neko; manual review remains required.'
review_reason='Approve the exact canonical-name correction target Neko for this owner-scoped observation.'

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$role_sql"
  rm -rf "$work"
}
trap cleanup EXIT

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory "$@"
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1" | sed -n '1p'
}

production_scalar() {
  docker exec brains-postgres-1 psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1" | sed -n '1p'
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

relevant_production_signature() {
  production_scalar "
    SELECT md5(jsonb_build_object(
      'source_plans',(SELECT count(*) FROM memory.entity_resolution_plan
        WHERE owner_user_id='$owner'::uuid
          AND resolution_id IN (
            '$source_resolution'::uuid,'$successor_resolution'::uuid
          )),
      'target_candidates',(SELECT count(*) FROM memory.entity_resolution_candidate
        WHERE owner_user_id='$owner'::uuid
          AND resolution_id='$successor_resolution'::uuid),
      'target_reviews',(SELECT count(*) FROM memory.entity_resolution_review
        WHERE owner_user_id='$owner'::uuid
          AND resolution_id='$successor_resolution'::uuid),
      'target_applies',(SELECT count(*) FROM memory.entity_resolution_apply
        WHERE owner_user_id='$owner'::uuid
          AND resolution_id='$successor_resolution'::uuid),
      'target_binding',(SELECT count(*) FROM memory.observation_entity_binding
        WHERE owner_user_id='$owner'::uuid
          AND observation_id='$observation'::uuid),
      'claims',(SELECT count(*) FROM memory.claim
        WHERE owner_user_id='$owner'::uuid),
      'projection_plans',(SELECT count(*) FROM memory.projection_plan
        WHERE owner_user_id='$owner'::uuid)
    )::text)"
}

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
[[ -f "$migration" && -f "$security_test" ]]
chmod 0600 "$backup" "$role_sql"
qdrant_before=$(qdrant_signature)
production_before=$(relevant_production_signature)
production_head_before=$(git -C /opt/chat-memory rev-parse HEAD)

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
  ORDER BY rolname" >"$role_sql"
[[ -s "$role_sql" ]]

"${compose[@]}" up -d --wait postgres
run_sql <"$role_sql"
printf '%s\n' \
  "ALTER ROLE brains_app PASSWORD 'clone_only_brains_password';" \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner <"$backup"

run_sql <"$writer_migration"
run_sql <"$writer_migration"
run_sql <"$migration"
run_sql <"$migration"
run_sql <"$security_test"

before_plans=$(scalar "SELECT count(*) FROM memory.entity_resolution_plan
  WHERE owner_user_id='$owner'::uuid")
before_candidates=$(scalar "SELECT count(*) FROM memory.entity_resolution_candidate
  WHERE owner_user_id='$owner'::uuid")
before_reconciliations=$(scalar "SELECT count(*)
  FROM memory.entity_correction_target_reconciliation_v5_2
  WHERE owner_user_id='$owner'::uuid")
before_reviews=$(scalar "SELECT count(*) FROM memory.entity_resolution_review
  WHERE owner_user_id='$owner'::uuid")
before_applies=$(scalar "SELECT count(*) FROM memory.entity_resolution_apply
  WHERE owner_user_id='$owner'::uuid")
before_bindings=$(scalar "SELECT count(*) FROM memory.observation_entity_binding
  WHERE owner_user_id='$owner'::uuid")
before_requests=$(scalar "SELECT count(*) FROM memory.relational_operation_request
  WHERE owner_user_id='$owner'::uuid")
before_entities=$(scalar "SELECT count(*) FROM memory.entity
  WHERE owner_user_id='$owner'::uuid")
before_claims=$(scalar "SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$owner'::uuid")
before_projections=$(scalar "SELECT count(*) FROM memory.projection_plan
  WHERE owner_user_id='$owner'::uuid")

apply_output="$work/apply.tsv"
run_sql -A -t -F $'\t' \
  -v owner="$owner" \
  -v source_resolution="$source_resolution" \
  -v successor_resolution="$successor_resolution" \
  -v target_entity="$target_entity" \
  -v reconcile_request="$reconcile_request" \
  -v review_request="$review_request" \
  -v apply_request="$apply_request" \
  -v reconcile_reason="$reconcile_reason" \
  -v review_reason="$review_reason" >"$apply_output" <<'SQL'
SET SESSION AUTHORIZATION brains_app;
BEGIN;
SELECT set_config('app.user_id', :'owner', true);

SELECT reconciliation_manifest_sha256
FROM memory.preflight_correction_target_reconciliation_v5_2(
  :'source_resolution'::uuid, :'successor_resolution'::uuid,
  :'target_entity'::uuid, :'reconcile_reason'
)
\gset correction_

SELECT outcome
FROM memory.reconcile_correction_target_v5_2(
  :'reconcile_request'::uuid, :'source_resolution'::uuid,
  :'successor_resolution'::uuid, :'target_entity'::uuid,
  :'reconcile_reason', :'correction_reconciliation_manifest_sha256'
)
\gset reconcile_

SELECT authorization_manifest_sha256
FROM memory.preflight_entity_resolution_review_v5_2(
  :'successor_resolution'::uuid,
  'approved'::memory.entity_review_decision,
  :'review_reason'
)
\gset review_preflight_

SELECT review_id,outcome
FROM memory.review_entity_resolution_v5_2(
  :'review_request'::uuid, :'successor_resolution'::uuid,
  'approved'::memory.entity_review_decision, :'review_reason',
  :'review_preflight_authorization_manifest_sha256'
)
\gset review_

SELECT apply_manifest_sha256
FROM memory.preflight_entity_resolution_apply_v5_2(
  :'successor_resolution'::uuid, :'review_review_id'::uuid
)
\gset apply_preflight_

SELECT applied_entity_id,outcome,bindings_created
FROM memory.apply_entity_resolution_v5_2(
  :'apply_request'::uuid, :'successor_resolution'::uuid,
  :'review_review_id'::uuid, :'apply_preflight_apply_manifest_sha256'
)
\gset apply_

SELECT :'reconcile_outcome', :'review_outcome', :'apply_outcome',
       :'apply_applied_entity_id', :'apply_bindings_created';
COMMIT;

BEGIN;
SELECT set_config('app.user_id', :'owner', true);
SELECT outcome
FROM memory.reconcile_correction_target_v5_2(
  :'reconcile_request'::uuid, :'source_resolution'::uuid,
  :'successor_resolution'::uuid, :'target_entity'::uuid,
  :'reconcile_reason', :'correction_reconciliation_manifest_sha256'
)
\gset replay_reconcile_
SELECT outcome
FROM memory.review_entity_resolution_v5_2(
  :'review_request'::uuid, :'successor_resolution'::uuid,
  'approved'::memory.entity_review_decision, :'review_reason',
  :'review_preflight_authorization_manifest_sha256'
)
\gset replay_review_
SELECT outcome,bindings_created
FROM memory.apply_entity_resolution_v5_2(
  :'apply_request'::uuid, :'successor_resolution'::uuid,
  :'review_review_id'::uuid, :'apply_preflight_apply_manifest_sha256'
)
\gset replay_apply_
SELECT :'replay_reconcile_outcome', :'replay_review_outcome',
       :'replay_apply_outcome', :'replay_apply_bindings_created';
COMMIT;
RESET SESSION AUTHORIZATION;
SQL

mapfile -t outcomes < <(
  grep -E $'^(applied|replayed)\t' "$apply_output"
)
[[ "${#outcomes[@]}" == 2 ]]
[[ "${outcomes[0]}" == $'applied\tapplied\tapplied\t'"$target_entity"$'\t1' ]]
[[ "${outcomes[1]}" == $'replayed\treplayed\treplayed\t0' ]]

[[ "$(scalar "SELECT count(*) FROM memory.entity_resolution_plan
  WHERE owner_user_id='$owner'::uuid")" == "$((before_plans + 1))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.entity_resolution_candidate
  WHERE owner_user_id='$owner'::uuid")" == "$((before_candidates + 1))" ]]
[[ "$(scalar "SELECT count(*)
  FROM memory.entity_correction_target_reconciliation_v5_2
  WHERE owner_user_id='$owner'::uuid")" == "$((before_reconciliations + 1))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.entity_resolution_review
  WHERE owner_user_id='$owner'::uuid")" == "$((before_reviews + 1))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.entity_resolution_apply
  WHERE owner_user_id='$owner'::uuid")" == "$((before_applies + 1))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.observation_entity_binding
  WHERE owner_user_id='$owner'::uuid")" == "$((before_bindings + 1))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.relational_operation_request
  WHERE owner_user_id='$owner'::uuid")" == "$((before_requests + 2))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.entity
  WHERE owner_user_id='$owner'::uuid")" == "$before_entities" ]]
[[ "$(scalar "SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$owner'::uuid")" == "$before_claims" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_plan
  WHERE owner_user_id='$owner'::uuid")" == "$before_projections" ]]

[[ "$(scalar "SELECT count(*)
  FROM memory.observation_entity_binding
  WHERE owner_user_id='$owner'::uuid
    AND observation_id='$observation'::uuid
    AND subject_resolution_id='$successor_resolution'::uuid
    AND subject_entity_id='$target_entity'::uuid
    AND object_resolution_id IS NULL
    AND object_entity_id IS NULL")" == 1 ]]
[[ "$(scalar "SELECT count(*)
  FROM memory.entity_resolution_plan
  WHERE owner_user_id='$owner'::uuid
    AND resolution_id='$successor_resolution'::uuid
    AND predicate_registry_version='memory_predicate_registry_v5_2'
    AND action='link_existing'
    AND decision_state='manual_review_required'
    AND selected_entity_id='$target_entity'::uuid")" == 1 ]]

qdrant_after=$(qdrant_signature)
production_after=$(relevant_production_signature)
production_head_after=$(git -C /opt/chat-memory rev-parse HEAD)
[[ "$qdrant_after" == "$qdrant_before" ]]
[[ "$production_after" == "$production_before" ]]
[[ "$production_head_after" == "$production_head_before" ]]

mkdir -p "$(dirname "$report")"
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git -C "$repo_root" rev-parse HEAD)" \
  --arg owner_user_id "$owner" \
  --arg source_resolution_id "$source_resolution" \
  --arg successor_resolution_id "$successor_resolution" \
  --arg observation_id "$observation" \
  --arg target_entity_id "$target_entity" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{
    contract_version:
      "memory_v1_v5_2_correction_target_reconciliation_clone_report_v1",
    completed_at:$completed_at,
    head_commit:$head_commit,
    owner_user_id:$owner_user_id,
    source_resolution_id:$source_resolution_id,
    successor_resolution_id:$successor_resolution_id,
    observation_id:$observation_id,
    target_entity_id:$target_entity_id,
    database_rows_created:8,
    bindings_created:1,
    checks:{
      production_clone:true,
      exact_canonical_name_correction:true,
      manual_review_required:true,
      cross_owner_rejected:true,
      wrong_target_rejected:true,
      missing_actor_rejected:true,
      zero_write_replay:true,
      entity_count_unchanged:true,
      claims_unchanged:true,
      projection_unchanged:true,
      production_unchanged:true,
      qdrant_unchanged:true,
      external_model_calls:0
    },
    qdrant_sha256:$qdrant_sha256,
    hard_stop:"before_production_schema_install_or_correction_apply"
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

printf '%s\n' \
  'memory_v1_v5_2_correction_target_reconciliation_clone: PASS' \
  "report=$report" \
  'clone_rows_created=8' \
  'bindings_created=1' \
  'same_run_replay_rows=0' \
  'production_writes=0' \
  'qdrant_writes=0' \
  'external_model_calls=0'
