#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores current production into a disposable clone,
# installs the legacy applied-stage compatibility contract, proves exact
# owner-scoped admission/planning and rollback, then removes the clone.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source "${MEMORY_V1_ENV_FILE:-/opt/chat-memory/.env}"
set +a

container=brains-postgres-1
production=memory
clone="memory_legacy_stage_compat_${$}"
migration=ops/sql/20260730_memory_v1_v5_legacy_applied_stage_entailment_compat_v1.sql
rollback=ops/sql/20260730_memory_v1_v5_legacy_applied_stage_entailment_compat_v1_rollback.sql
security_test=tests/memory_v1_v5_legacy_applied_stage_entailment_compat_v1_security.sql
backup=$(mktemp /tmp/memory-legacy-stage-compat.XXXXXX.dump)
head=$(git rev-parse HEAD)
role_preexisting=$(
  sudo docker exec "$container" psql -U sage -d "$production" \
    -X -Atqc "
      SELECT EXISTS (
        SELECT 1 FROM pg_roles
        WHERE rolname='memory_v5_legacy_stage_compat_maintainer'
      )
    "
)

qdrant_hash() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

cleanup() {
  rc=$?
  trap - EXIT
  sudo docker exec "$container" dropdb -U sage --if-exists --force "$clone" \
    >/dev/null 2>&1 || true
  if [[ "$role_preexisting" == f ]]; then
    sudo docker exec "$container" psql -U sage -d "$production" \
      -X -v ON_ERROR_STOP=1 -q -c \
      'DROP ROLE IF EXISTS memory_v5_legacy_stage_compat_maintainer' \
      >/dev/null 2>&1 || true
  fi
  rm -f "$backup"
  exit "$rc"
}
trap cleanup EXIT

for file in "$migration" "$rollback" "$security_test"; do
  [[ -s "$file" ]]
done
[[ "$head" =~ ^[0-9a-f]{40}$ ]]
chmod 0600 "$backup"

qdrant_before=$(qdrant_hash)
sudo docker exec "$container" pg_dump -U sage -d "$production" -Fc \
  >"$backup"
test -s "$backup"
sudo docker exec "$container" createdb -U sage -T template0 "$clone"
sudo docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --exit-on-error <"$backup"

protected_before=$(
  sudo docker exec "$container" psql -U sage -d "$clone" \
    -X -Atqc "
      SELECT jsonb_build_object(
        'claims',(SELECT count(*) FROM memory.claim),
        'claim_revisions',(SELECT count(*) FROM memory.claim_revision),
        'claim_links',(SELECT count(*) FROM memory.claim_observation),
        'entities',(SELECT count(*) FROM memory.entity),
        'observations',(SELECT count(*) FROM memory.observation),
        'bindings',(SELECT count(*) FROM memory.observation_entity_binding),
        'assessments',(SELECT count(*) FROM memory.v5_local_entailment_assessment),
        'entailments',(SELECT count(*) FROM memory.observation_entailment_v5),
        'packets',(SELECT count(*) FROM memory.evidence_extraction_packet_v5_local),
        'stage_admissions',(SELECT count(*) FROM memory.v5_local_packet_stage_admission),
        'operation_requests',(SELECT count(*) FROM memory.relational_operation_request)
      )::text
    "
)

sudo docker exec -i "$container" psql -U sage -d "$clone" \
  -X -v ON_ERROR_STOP=1 <"$migration" >/dev/null
sudo docker exec -i "$container" psql -U sage -d "$clone" \
  -X -v ON_ERROR_STOP=1 <"$migration" >/dev/null

protected_after_migration=$(
  sudo docker exec "$container" psql -U sage -d "$clone" \
    -X -Atqc "
      SELECT jsonb_build_object(
        'claims',(SELECT count(*) FROM memory.claim),
        'claim_revisions',(SELECT count(*) FROM memory.claim_revision),
        'claim_links',(SELECT count(*) FROM memory.claim_observation),
        'entities',(SELECT count(*) FROM memory.entity),
        'observations',(SELECT count(*) FROM memory.observation),
        'bindings',(SELECT count(*) FROM memory.observation_entity_binding),
        'assessments',(SELECT count(*) FROM memory.v5_local_entailment_assessment),
        'entailments',(SELECT count(*) FROM memory.observation_entailment_v5),
        'packets',(SELECT count(*) FROM memory.evidence_extraction_packet_v5_local),
        'stage_admissions',(SELECT count(*) FROM memory.v5_local_packet_stage_admission),
        'operation_requests',(SELECT count(*) FROM memory.relational_operation_request)
      )::text
    "
)
[[ "$protected_after_migration" == "$protected_before" ]]
[[ "$(
  sudo docker exec "$container" psql -U sage -d "$clone" \
    -X -Atqc "
      SELECT count(*)
      FROM pg_trigger
      WHERE tgrelid =
        'memory.v5_local_legacy_stage_admission_observation'::regclass
        AND tgname =
          'v5_local_legacy_stage_admission_observation_append_only_guard'
        AND NOT tgisinternal
    "
)" == 1 ]]
[[ "$(
  sudo docker exec "$container" psql -U sage -d "$clone" \
    -X -Atqc "
      SELECT
        NOT has_table_privilege(
          'memory_v5_legacy_stage_compat_maintainer',
          'memory.v5_local_legacy_stage_admission_observation',
          'UPDATE'
        )
        AND NOT has_table_privilege(
          'memory_v5_legacy_stage_compat_maintainer',
          'memory.v5_local_legacy_stage_admission_observation',
          'DELETE'
        )
    "
)" == t ]]

clone_dsn=$(
  /opt/chat-memory/venv/bin/python -c \
    'import sys; from urllib.parse import urlsplit,urlunsplit; u=urlsplit(sys.argv[1]); print(urlunsplit((u.scheme,u.netloc,"/"+sys.argv[2],u.query,u.fragment)))' \
    "$POSTGRES_DSN" "$clone"
)
psql "$clone_dsn" -X -v ON_ERROR_STOP=1 \
  -v repository_commit="$head" <"$security_test" >/dev/null

protected_after_tests=$(
  sudo docker exec "$container" psql -U sage -d "$clone" \
    -X -Atqc "
      SELECT jsonb_build_object(
        'claims',(SELECT count(*) FROM memory.claim),
        'claim_revisions',(SELECT count(*) FROM memory.claim_revision),
        'claim_links',(SELECT count(*) FROM memory.claim_observation),
        'entities',(SELECT count(*) FROM memory.entity),
        'observations',(SELECT count(*) FROM memory.observation),
        'bindings',(SELECT count(*) FROM memory.observation_entity_binding),
        'assessments',(SELECT count(*) FROM memory.v5_local_entailment_assessment),
        'entailments',(SELECT count(*) FROM memory.observation_entailment_v5),
        'packets',(SELECT count(*) FROM memory.evidence_extraction_packet_v5_local),
        'stage_admissions',(SELECT count(*) FROM memory.v5_local_packet_stage_admission),
        'operation_requests',(SELECT count(*) FROM memory.relational_operation_request)
      )::text
    "
)
[[ "$protected_after_tests" == "$protected_before" ]]

sudo docker exec -i "$container" psql -U sage -d "$clone" \
  -X -v ON_ERROR_STOP=1 <"$rollback" >/dev/null

[[ "$(
  sudo docker exec "$container" psql -U sage -d "$clone" \
    -X -Atqc "
      SELECT has_function_privilege(
        'brains_app',
        'memory.register_owner_v5_legacy_stage_compat_v1(uuid,uuid,uuid,uuid,uuid,text,text,text,text,text,integer,text,text,text)',
        'EXECUTE'
      )
    "
)" == f ]]
[[ "$(
  sudo docker exec "$container" psql -U sage -d "$clone" \
    -X -Atqc "
      SELECT position(
        'legacy_applied_stage_entailment'
        IN pg_get_functiondef(
          'memory.plan_owner_v5_local_entailment_v1(integer)'::regprocedure
        )
      ) = 0
    "
)" == t ]]

qdrant_after=$(qdrant_hash)
[[ "$qdrant_after" == "$qdrant_before" ]]

printf '%s\n' \
  'LEGACY_STAGE_COMPAT_CLONE=PASS' \
  "HEAD=$head" \
  "QDRANT_SHA256=$qdrant_after" \
  'EXPECTED_ADMISSIONS=2' \
  'EXPECTED_ALLOWED_OBSERVATIONS=20' \
  'MODEL_CALLS=0'
