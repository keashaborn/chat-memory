#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_2_PROJECTION_CLONE_PORT:-55474}
export MEMORY_V1_V5_2_PROJECTION_CLONE_PORT="$port"
# The shared Compose file intentionally retains its established variable name.
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v52projectionclone
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
entailment_migration=ops/sql/20260716_memory_v1_observation_entailment_v5_1.sql
v5_2_migration=ops/sql/20260722_memory_v1_relational_stage_v5_2.sql
v5_2_rollback=ops/sql/20260722_memory_v1_relational_stage_v5_2_rollback.sql
v5_2_security_test=tests/memory_v1_v5_2_stage_preflight_api.sql
registry_installer=scripts/memory_v1_predicate_registry_v5_2.py
entity_migration=ops/sql/20260722_memory_v1_entity_resolution_reconciliation_v5_2.sql
entity_rollback=ops/sql/20260722_memory_v1_entity_resolution_reconciliation_v5_2_rollback.sql
seed_sql=tests/memory_v1_v5_stage_batch_seed.sql
stage_runner=scripts/memory_v1_v5_2_stage_batch.py
stage_fixture=tests/memory_v1_v5_2_stage_batch_fixture.py
stage_unit_test=tests/test_memory_v1_v5_2_stage_batch.py
review_unit_test=tests/test_memory_v1_v5_1_review_local_packet.py
profile_unit_test=tests/test_memory_v1_v5_2_review_profile.py
entity_runner=scripts/memory_v1_v5_2_entity_resolution_batch.py
entity_fixture=tests/memory_v1_v5_2_entity_resolution_batch_fixture.py
entity_unit_test=tests/test_memory_v1_v5_2_entity_resolution_batch.py
projection_apply_migration=ops/sql/20260715_memory_v1_projection_apply_v5.sql
projection_migration=ops/sql/20260722_memory_v1_projection_dispatch_v5_2.sql
projection_unit_test=tests/test_memory_v1_v5_2_projection_dispatch.py
projection_clone=tests/memory_v1_v5_2_projection_dispatch_clone.py
backup=$(mktemp /tmp/memory-v1-stage-batch.XXXXXX.dump)
role_sql=$(mktemp /tmp/memory-v1-stage-batch-roles.XXXXXX.sql)
work=$(mktemp -d /tmp/memory-v1-stage-batch.XXXXXX)
reviews="$work/reviews"
plan="$reviews/plan.json"
authorization="$reviews/authorization.json"
report="$reviews/apply-report.json"
registry_sql="$work/predicate-registry-v5-2.sql"
entity_manifest="$reviews/entity-manifest.json"
entity_plan="$reviews/entity-plan.json"
entity_authorization="$reviews/entity-authorization.json"
entity_report="$reviews/entity-apply-report.json"
entity_cross_manifest="$reviews/entity-cross-owner-manifest.json"
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

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

qdrant_before=$(qdrant_signature)
production_memory_rows_before=$(docker exec brains-postgres-1 psql -X -A -t \
  -U sage -d memory -c "
    SELECT count(*) FROM memory.relational_stage_batch
    UNION ALL SELECT count(*) FROM memory.relational_operation_request
    UNION ALL SELECT count(*) FROM memory.entity_mention
    UNION ALL SELECT count(*) FROM memory.entity_resolution_plan
    UNION ALL SELECT count(*) FROM memory.observation
    UNION ALL SELECT count(*) FROM memory.observation_temporal
    ORDER BY 1" | tr -d '[:space:]')

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
run_sql <"$entailment_migration"
run_sql <"$entailment_migration"
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$registry_installer" \
  --emit-install-sql >"$registry_sql"
run_sql <"$registry_sql"
run_sql <"$registry_sql"
run_sql <"$v5_2_migration"
run_sql <"$v5_2_migration"
run_sql <"$v5_2_security_test"
run_sql <"$entity_migration"
run_sql <"$entity_migration"
run_sql <"$seed_sql"
run_sql <<'SQL'
INSERT INTO memory.evidence(
  evidence_id,owner_user_id,kind,source_system,external_id,
  content,content_sha256,observed_at,recorded_at,sensitivity,status
) VALUES (
  'aeeeeeee-1111-4111-8111-111111111115',
  '11111111-1111-4111-8111-111111111111',
  'user_statement','public.chat_log',
  'aeeeeeee-1111-4111-8111-111111111115',
  'I think expert consensus should be treated as evidence, not absolute fact.',
  '2c0c1fbeeab2c9fe88ecbaa42d22fd80bbf2d5bc776cceb37c8313a73a133c1c',
  '2026-07-16T12:04:00Z','2026-07-16T12:04:00Z','medium','active'
);
SQL
run_sql \
  -v target_owner=11111111-1111-4111-8111-111111111111 \
  -v other_owner=22222222-2222-4222-8222-222222222222 \
  -v evidence_id=aeeeeeee-1111-4111-8111-111111111112 \
  -v source_id=aeeeeeee-1111-4111-8111-111111111112 \
  -v source_sha256=3f55b194db3ae03b7100b9e8f572ab254f43bed87b151c3a5e0015163374d2aa \
  -v source_recorded_at=2026-07-16T12:01:00Z <"$source_id_test"

PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$stage_unit_test"
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$review_unit_test"
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$profile_unit_test"
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$entity_unit_test"
PYTHONPATH="$repo_root/scripts:$repo_root" \
  /opt/chat-memory/venv/bin/python "$projection_unit_test"
/opt/chat-memory/venv/bin/python "$stage_fixture" prepare --review-root "$reviews"

head=$(git -C "$repo_root" rev-parse HEAD)
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$stage_runner" plan \
  --manifest "$reviews/manifest.json" \
  --review-root "$reviews" \
  --output "$plan"

if POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$stage_runner" plan \
  --manifest "$reviews/cross-owner-manifest.json" \
  --review-root "$reviews" \
  --output "$reviews/cross-owner-plan.json" >/dev/null 2>&1; then
  echo "cross-owner bundle unexpectedly passed plan validation" >&2
  exit 1
fi
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$stage_runner" plan \
  --manifest "$reviews/forged-component-manifest.json" \
  --review-root "$reviews" \
  --output "$forged_plan"
[[ "$(scalar "
  SELECT count(*) FROM memory.relational_stage_batch
  WHERE owner_user_id IN (
    '11111111-1111-4111-8111-111111111111'::uuid,
    '22222222-2222-4222-8222-222222222222'::uuid
  )")" == "0" ]]

/opt/chat-memory/venv/bin/python "$stage_fixture" authorize \
  --plan "$plan" --output "$authorization" --head "$head"
/opt/chat-memory/venv/bin/python "$stage_fixture" authorize \
  --plan "$forged_plan" --output "$forged_authorization" --head "$head"

if MEMORY_V1_V5_2_STAGE_BATCH_APPLY=authorized POSTGRES_DSN="$dsn" \
  PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$stage_runner" apply \
  --plan "$forged_plan" \
  --authorization "$forged_authorization" \
  --review-root "$reviews" \
  --confirm STAGE_REVIEWED_OWNER_V5_2_PACKETS_ONLY \
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
if MEMORY_V1_V5_2_STAGE_BATCH_APPLY=authorized POSTGRES_DSN="$dsn" \
  PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$stage_runner" apply \
  --plan "$plan" \
  --authorization "$authorization" \
  --review-root "$reviews" \
  --confirm STAGE_REVIEWED_OWNER_V5_2_PACKETS_ONLY \
  --output "$reviews/tampered-apply.json" >/dev/null 2>&1; then
  echo "tampered bundle unexpectedly passed apply validation" >&2
  exit 1
fi
mv "$reviews/owner-a-name.json.original" "$reviews/owner-a-name.json"
[[ "$(scalar "
  SELECT count(*) FROM memory.relational_stage_batch
  WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid")" == "0" ]]

MEMORY_V1_V5_2_STAGE_BATCH_APPLY=authorized POSTGRES_DSN="$dsn" \
  PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$stage_runner" apply \
  --plan "$plan" \
  --authorization "$authorization" \
  --review-root "$reviews" \
  --confirm STAGE_REVIEWED_OWNER_V5_2_PACKETS_ONLY \
  --output "$report"

[[ "$(jq -r '.database_rows_created' "$report")" == "22" ]]
[[ "$(jq -r '[.applied[].outcome] | sort | join(",")' "$report")" \
    == "applied,applied,applied,applied" ]]
[[ "$(jq -r '[.replayed[].outcome] | sort | join(",")' "$report")" \
    == "replayed,replayed,replayed,replayed" ]]
[[ "$(jq '[.replayed[].counts[]] | add' "$report")" == "0" ]]

[[ "$(scalar "
  SELECT (
    (SELECT count(*) FROM memory.relational_stage_batch
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=4
    AND (SELECT count(*) FROM memory.relational_operation_request
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid
        AND operation='stage_packet')=4
    AND (SELECT count(*) FROM memory.entity_mention
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=3
    AND (SELECT count(*) FROM memory.entity_resolution_plan
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=3
    AND (SELECT count(*) FROM memory.entity_resolution_plan
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid
        AND predicate_registry_version='memory_predicate_registry_v5_2')=3
    AND (SELECT count(*) FROM memory.entity_resolution_candidate
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=2
    AND (SELECT count(*) FROM memory.observation
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=3
    AND (SELECT count(*) FROM memory.observation_temporal
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=3
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
    AND (SELECT count(*) FROM memory.observation
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid
        AND evidence_id='aeeeeeee-1111-4111-8111-111111111115'::uuid
        AND predicate='stance.reported'
        AND modality='reported_belief'
        AND projection_class='reported_stance'
        AND surface_policy='relevant_recall_or_explicit_recall')=1
  )::int")" == "1" ]]

name_resolution=$(scalar "
  SELECT plan.resolution_id
  FROM memory.entity_resolution_plan AS plan
  WHERE plan.owner_user_id='11111111-1111-4111-8111-111111111111'::uuid
    AND plan.evidence_id='aeeeeeee-1111-4111-8111-111111111112'::uuid
    AND plan.predicate_registry_version='memory_predicate_registry_v5_2'")
stance_resolution=$(scalar "
  SELECT plan.resolution_id
  FROM memory.entity_resolution_plan AS plan
  WHERE plan.owner_user_id='11111111-1111-4111-8111-111111111111'::uuid
    AND plan.evidence_id='aeeeeeee-1111-4111-8111-111111111115'::uuid
    AND plan.predicate_registry_version='memory_predicate_registry_v5_2'")
[[ -n "$name_resolution" && -n "$stance_resolution" ]]

/opt/chat-memory/venv/bin/python - \
  "$entity_manifest" "$name_resolution" "$stance_resolution" <<'PY'
import json
import os
from pathlib import Path
import sys

path = Path(sys.argv[1])
value = {
    "contract_version": "memory_v1_v5_2_entity_resolution_batch_manifest_v1",
    "target_server": "seebx",
    "owner_user_id": "11111111-1111-4111-8111-111111111111",
    "expected_total_bindings": 2,
    "expected_new_rows": 6,
    "items": [
        {
            "resolution_id": sys.argv[2],
            "operation": "auto_apply",
            "expected_action": "link_existing",
            "expected_decision_state": "auto_link_eligible",
            "review_reason": None,
        },
        {
            "resolution_id": sys.argv[3],
            "operation": "auto_apply",
            "expected_action": "link_existing",
            "expected_decision_state": "auto_link_eligible",
            "review_reason": None,
        },
    ],
}
path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
os.chmod(path, 0o600)
PY

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$entity_runner" plan \
  --manifest "$entity_manifest" --review-root "$reviews" \
  --output "$entity_plan"
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$entity_fixture" \
  --plan "$entity_plan" --output "$entity_authorization" --head "$head"

jq --arg owner '22222222-2222-4222-8222-222222222222' \
  '.owner_user_id=$owner' "$entity_manifest" >"$entity_cross_manifest"
chmod 0600 "$entity_cross_manifest"
if POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$entity_runner" plan \
  --manifest "$entity_cross_manifest" --review-root "$reviews" \
  --output "$reviews/entity-cross-owner-plan.json" >/dev/null 2>&1; then
  echo 'cross-owner V5.2 entity plan unexpectedly passed' >&2
  exit 1
fi

legacy_owner=$(scalar "
  SELECT owner_user_id FROM memory.entity_resolution_plan
  WHERE predicate_registry_version='memory_predicate_registry_v5_1'
  ORDER BY created_at,resolution_id LIMIT 1")
legacy_resolution=$(scalar "
  SELECT resolution_id FROM memory.entity_resolution_plan
  WHERE predicate_registry_version='memory_predicate_registry_v5_1'
  ORDER BY created_at,resolution_id LIMIT 1")
if [[ -n "$legacy_owner" && -n "$legacy_resolution" ]] && \
  PGPASSWORD=clone_only_brains_password psql "$dsn" -X -v ON_ERROR_STOP=1 \
    -c "SELECT set_config('app.user_id','$legacy_owner',false);
        SELECT * FROM memory.preflight_entity_resolution_apply_v5_2(
          '$legacy_resolution'::uuid,NULL
        );" >/dev/null 2>&1; then
  echo 'V5.1 resolution unexpectedly crossed the V5.2 wrapper' >&2
  exit 1
fi

MEMORY_V1_V5_2_ENTITY_RESOLUTION_BATCH_APPLY=authorized \
  POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$entity_runner" apply \
  --plan "$entity_plan" --authorization "$entity_authorization" \
  --review-root "$reviews" \
  --confirm RECONCILE_REVIEW_AND_APPLY_OWNER_V5_2_ENTITY_RESOLUTIONS_ONLY \
  --output "$entity_report"

[[ "$(jq -r '.database_rows_created' "$entity_report")" == 6 ]]
[[ "$(jq -r '.bindings_created' "$entity_report")" == 2 ]]
[[ "$(jq -r '.item_count' "$entity_report")" == 2 ]]
[[ "$(jq -r '[.replayed[].apply_outcome] | unique | join(",")' \
  "$entity_report")" == replayed ]]
[[ "$(scalar "
  SELECT (
    (SELECT count(*) FROM memory.entity_resolution_apply
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid
        AND resolution_id IN ('$name_resolution'::uuid,'$stance_resolution'::uuid))=2
    AND (SELECT count(*) FROM memory.observation_entity_binding
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid
        AND observation_id IN (
          SELECT observation_id FROM memory.observation
          WHERE evidence_id IN (
            'aeeeeeee-1111-4111-8111-111111111112'::uuid,
            'aeeeeeee-1111-4111-8111-111111111115'::uuid
          )
        ))=2
    AND (SELECT count(*) FROM memory.claim
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=0
    AND (SELECT count(*) FROM memory.projection_plan
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=0
  )::int")" == 1 ]]

run_sql <"$projection_apply_migration"
run_sql <"$projection_apply_migration"
# Reapply the staging ACL after restore/apply compatibility has recreated
# restricted functions and tables without their production object ownership.
run_sql <"$staging_migration"
projection_acl_state=$(scalar "
  SELECT concat_ws('|',
    has_table_privilege(
      'memory_v5_writer','memory.projection_review','SELECT,INSERT'
    ),
    has_table_privilege(
      'memory_v5_writer','memory.projection_plan_item','SELECT'
    ),
    (
      SELECT pg_get_userbyid(p.proowner)
      FROM pg_proc AS p
      JOIN pg_namespace AS n ON n.oid=p.pronamespace
      WHERE n.nspname='memory'
        AND p.proname='preflight_projection_review_v5'
      LIMIT 1
    )
  )")
echo "projection_acl_state=$projection_acl_state"
[[ "$projection_acl_state" == "true|true|memory_v5_writer" ]]
run_sql <"$projection_migration"
run_sql <"$projection_migration"
name_observation=$(scalar "
  SELECT observation_id FROM memory.observation
  WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid
    AND evidence_id='aeeeeeee-1111-4111-8111-111111111112'::uuid
    AND predicate_registry_version='memory_predicate_registry_v5_2'")
stance_observation=$(scalar "
  SELECT observation_id FROM memory.observation
  WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid
    AND evidence_id='aeeeeeee-1111-4111-8111-111111111115'::uuid
    AND predicate_registry_version='memory_predicate_registry_v5_2'")
[[ -n "$name_observation" && -n "$stance_observation" ]]
POSTGRES_DSN="$dsn" \
  V5_2_NAME_OBSERVATION_ID="$name_observation" \
  V5_2_STANCE_OBSERVATION_ID="$stance_observation" \
  PYTHONPATH="$repo_root/scripts:$repo_root" \
  /opt/chat-memory/venv/bin/python "$projection_clone"

[[ "$(scalar "
  SELECT (
    (SELECT count(*) FROM memory.projection_plan
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid
        AND predicate_registry_version='memory_predicate_registry_v5_2')=2
    AND (SELECT count(*) FROM memory.projection_plan_item
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid
        AND predicate_registry_version='memory_predicate_registry_v5_2')=2
    AND (SELECT count(*) FROM memory.projection_claim_payload
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid
        AND claim_class='reported_stance')=1
    AND (SELECT count(*) FROM memory.observation_entailment_v5
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid
        AND observation_id IN ('$name_observation'::uuid,'$stance_observation'::uuid)
        AND decision='accepted')=2
    AND (SELECT count(*) FROM memory.claim
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid
        AND status='supported')=2
    AND (SELECT count(*) FROM memory.claim_revision
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=4
    AND (SELECT count(*) FROM memory.claim_observation
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=2
    AND (SELECT count(*) FROM memory.projection_review
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid
        AND decision='authorized')=2
    AND (SELECT count(*) FROM memory.projection_apply_event
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=2
    AND (SELECT count(*) FROM memory.claim_assessment
      WHERE owner_user_id='11111111-1111-4111-8111-111111111111'::uuid)=2
    AND (SELECT count(*) FROM memory.projection_plan
      WHERE owner_user_id='22222222-2222-4222-8222-222222222222'::uuid)=0
    AND (SELECT count(*) FROM memory.claim
      WHERE owner_user_id='22222222-2222-4222-8222-222222222222'::uuid)=0
  )::int")" == "1" ]]

[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(docker exec brains-postgres-1 psql -X -A -t \
  -U sage -d memory -c "
    SELECT count(*) FROM memory.relational_stage_batch
    UNION ALL SELECT count(*) FROM memory.relational_operation_request
    UNION ALL SELECT count(*) FROM memory.entity_mention
    UNION ALL SELECT count(*) FROM memory.entity_resolution_plan
    UNION ALL SELECT count(*) FROM memory.observation
    UNION ALL SELECT count(*) FROM memory.observation_temporal
    ORDER BY 1" | tr -d '[:space:]')" == "$production_memory_rows_before" ]]

echo "memory_v1_v5_2_projection_dispatch_production_clone: PASS"
