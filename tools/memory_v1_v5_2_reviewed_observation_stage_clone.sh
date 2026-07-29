#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_2_REVIEWED_OBSERVATION_CLONE_PORT:-55479}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v52reviewedobservationclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
migration=ops/sql/20260729_memory_v1_v5_2_reviewed_observation_stage.sql
rollback=ops/sql/20260729_memory_v1_v5_2_reviewed_observation_stage_rollback.sql
test_sql=tests/memory_v1_v5_2_reviewed_observation_stage.sql
backup=$(mktemp /tmp/memory-v1-v5-2-reviewed-observation.XXXXXX.dump)
role_sql=$(mktemp /tmp/memory-v1-v5-2-reviewed-observation-roles.XXXXXX.sql)
work=$(mktemp -d /tmp/memory-v1-v5-2-reviewed-observation.XXXXXX)
private_entailment=${RUN_PRIVATE_ENTAILMENT:-0}

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

production_scalar() {
  docker exec brains-postgres-1 psql -X -A -t -v ON_ERROR_STOP=1 \
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

protected_signature_sql="
  SELECT md5(jsonb_build_object(
    'entities',(SELECT count(*) FROM memory.entity),
    'observations',(SELECT count(*) FROM memory.observation),
    'claims',(SELECT count(*) FROM memory.claim),
    'claim_revisions',(SELECT count(*) FROM memory.claim_revision),
    'projections',(SELECT count(*) FROM memory.projection_apply_event)
  )::text)
"

qdrant_before=$(qdrant_signature)
production_before=$(production_scalar "$protected_signature_sql")
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
  ORDER BY rolname
" >"$role_sql"
[[ -s "$role_sql" ]]

"${compose[@]}" up -d --wait postgres
run_sql <"$role_sql"
printf '%s\n' \
  "ALTER ROLE brains_app PASSWORD 'clone_only_brains_password';" \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner <"$backup"

clone_before=$(scalar "$protected_signature_sql")

run_sql <"$migration"
[[ "$(scalar "
  SELECT relrowsecurity::text||':'||relforcerowsecurity::text
  FROM pg_class
  WHERE oid='memory.v5_2_reviewed_observation_stage_admission'::regclass
")" == "true:true" ]]
[[ "$(scalar "
  SELECT rolsuper::text||':'||rolcanlogin::text||':'||
         rolbypassrls::text||':'||rolinherit::text
  FROM pg_roles
  WHERE rolname='memory_v5_2_reviewed_observation_stage_maintainer'
")" == "false:false:false:false" ]]
run_sql <"$rollback"
[[ "$(scalar "
  SELECT to_regclass(
    'memory.v5_2_reviewed_observation_stage_admission'
  ) IS NULL
")" == t ]]

run_sql <"$migration"
run_sql <"$test_sql"

[[ "$(scalar "
  SELECT count(*)
  FROM memory.v5_2_reviewed_observation_stage_admission
  WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
    AND admission_id=ANY(ARRAY[
      '10000000-0000-4000-8000-000000000001'::uuid,
      '20000000-0000-4000-8000-000000000002'::uuid,
      '30000000-0000-4000-8000-000000000003'::uuid
    ])
")" == 3 ]]
[[ "$(scalar "
  SELECT count(*)
  FROM memory.v5_local_packet_stage_admission
  WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
    AND decision IN (
      'v5_2_atom_reviewed_stage','v5_2_reviewed_route_stage'
    )
")" == 3 ]]
[[ "$(scalar "
  SELECT count(*)
  FROM memory.v5_2_reviewed_observation_stage_admission
  WHERE owner_user_id<>'1240822d-ac9a-4096-95aa-e2b24d36ef50'
")" == 0 ]]
[[ "$(scalar "
  SELECT count(*)
  FROM memory.v5_local_entailment_assessment
  WHERE observation_id=ANY(ARRAY[
    '917ab793-6f03-4af4-847b-c87f5632fa91'::uuid,
    'c0194481-bed5-438f-9407-07e398f14e50'::uuid,
    '14e21c6b-1728-439b-9613-7d9b933d33b8'::uuid
  ])
")" == 0 ]]

if [[ "$private_entailment" == 1 ]]; then
  dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"
  for sequence in 1 2 3; do
    unit="memory-v1-v5-2-reviewed-observation-clone-${sequence}-$$"
    systemd-run --quiet --wait --pipe --collect --unit="$unit" \
      -p User=ubuntu -p Group=ubuntu \
      -p WorkingDirectory="$repo_root" \
      -p LoadCredential=local_api_key:/etc/memory-v1-local-inference/api-key \
      -E POSTGRES_DSN="$dsn" \
      -E PYTHONPATH="$repo_root" \
      -E MEMORY_V1_V5_LOCAL_ENTAILMENT_APPLY=memory_v1_v5_local_entailment_apply_v1 \
      /opt/chat-memory/venv/bin/python \
      scripts/memory_v1_v5_local_entailment_scheduler.py \
      --owner-user-id 1240822d-ac9a-4096-95aa-e2b24d36ef50 \
      --max-output-tokens 128 --timeout-seconds 300 --apply \
      >"$work/entailment-${sequence}.json"
    jq -e '
      .apply==true
      and .outcome=="observation_entailment_recorded"
      and .local_model_calls==1
      and .external_model_calls==0
      and .zero_write_replay_proved==true
      and .write_counts.claims==0
      and .write_counts.projections==0
      and .write_counts.qdrant==0
      and .write_counts.prompt_influence==0
    ' "$work/entailment-${sequence}.json" >/dev/null
  done
  [[ "$(scalar "
    SELECT count(*)
    FROM memory.v5_local_entailment_assessment
    WHERE observation_id=ANY(ARRAY[
      '917ab793-6f03-4af4-847b-c87f5632fa91'::uuid,
      'c0194481-bed5-438f-9407-07e398f14e50'::uuid,
      '14e21c6b-1728-439b-9613-7d9b933d33b8'::uuid
    ])
  ")" == 3 ]]
  [[ "$(scalar "
    SELECT count(*)
    FROM memory.observation_entailment_v5
    WHERE observation_id=ANY(ARRAY[
      '917ab793-6f03-4af4-847b-c87f5632fa91'::uuid,
      'c0194481-bed5-438f-9407-07e398f14e50'::uuid,
      '14e21c6b-1728-439b-9613-7d9b933d33b8'::uuid
    ])
  ")" == 3 ]]
  entailment_summary=$(scalar "
    SELECT jsonb_object_agg(governed_decision::text,count_value)::text
    FROM (
      SELECT governed_decision,count(*) AS count_value
      FROM memory.v5_local_entailment_assessment
      WHERE observation_id=ANY(ARRAY[
        '917ab793-6f03-4af4-847b-c87f5632fa91'::uuid,
        'c0194481-bed5-438f-9407-07e398f14e50'::uuid,
        '14e21c6b-1728-439b-9613-7d9b933d33b8'::uuid
      ])
      GROUP BY governed_decision
    ) AS counts
  ")
else
  entailment_summary='{}'
fi
clone_after=$(scalar "$protected_signature_sql")
[[ "$clone_before" == "$clone_after" ]]

qdrant_after=$(qdrant_signature)
production_after=$(production_scalar "$protected_signature_sql")
production_head_after=$(git -C /opt/chat-memory rev-parse HEAD)
[[ "$qdrant_before" == "$qdrant_after" ]]
[[ "$production_before" == "$production_after" ]]
[[ "$production_head_before" == "$production_head_after" ]]

printf '%s\n' \
  "REVIEWED_OBSERVATION_STAGE_CLONE=PASS" \
  "PRODUCTION_HEAD=$production_head_after" \
  "MIGRATION_SHA256=$(sha256sum "$migration" | awk '{print $1}')" \
  "ROLLBACK_SHA256=$(sha256sum "$rollback" | awk '{print $1}')" \
  "TARGET_STAGE_ADMISSIONS=3" \
  "TARGET_ENTAILMENT_ASSESSMENTS=$(scalar "
    SELECT count(*)
    FROM memory.v5_local_entailment_assessment
    WHERE observation_id=ANY(ARRAY[
      '917ab793-6f03-4af4-847b-c87f5632fa91'::uuid,
      'c0194481-bed5-438f-9407-07e398f14e50'::uuid,
      '14e21c6b-1728-439b-9613-7d9b933d33b8'::uuid
    ])
  ")" \
  "TARGET_ENTAILMENT_DECISIONS=$entailment_summary" \
  "PROTECTED_SIGNATURE=$clone_after" \
  "QDRANT_SIGNATURE=$qdrant_after"
