#!/usr/bin/env bash
set -euo pipefail

# Server: seebx backend. Production is read-only. Every V5.2 write occurs in
# a disposable database restored from a fresh production backup.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

container=brains-postgres-1
production=memory
clone="memory_predicate_runtime_v5_2_${$}"
generator=scripts/memory_v1_predicate_registry_v5_2.py
registry=specs/memory_v1_predicate_registry_v5_2.json
registry_rollback=ops/sql/20260722_memory_v1_predicate_registry_v5_2_rollback.sql
registry_test=tests/memory_v1_predicate_registry_v5_2.sql
persistence=ops/sql/20260722_memory_v1_predicate_runtime_v5_2_local_persistence.sql
persistence_rollback=ops/sql/20260722_memory_v1_predicate_runtime_v5_2_local_persistence_rollback.sql
persistence_test=tests/memory_v1_predicate_runtime_v5_2_local_persistence.sql

generator_sha=433fa293ca7cf97057069a401fa8948ffed40c1d9b0e96b24c77cdd242727c55
registry_sha=e6ac5dfe7d7939aac23223ae76272b2e4f67777decde814d9bf0b8eee82b277e
registry_rollback_sha=6800e8128c00978a0cc28083cd81dbd413cf83f5e2468b57ec32bd83a1d31774
registry_test_sha=8e4bfe193940fd8d425de2eca51a9c222b2eec7331fdfc143ad29de9e19e5b67
persistence_sha=88efc9c634834ca87dcb8bd68805b9883b70d496c26adc8ea994021dfe6c77f6
persistence_rollback_sha=0fa6e4102f0935eeceb13dd9ef1f8042cfc843e632788b3c21bb686c45f6a46b
persistence_test_sha=e5b1a69a28cb584afb84a0244f8f215713daa1c969cc4fb5123ed267048299aa

backup=$(mktemp /tmp/memory-v5-2-runtime.XXXXXX.dump)
install_one=$(mktemp /tmp/memory-v5-2-install-one.XXXXXX.sql)
install_two=$(mktemp /tmp/memory-v5-2-install-two.XXXXXX.sql)
data_before=$(mktemp /tmp/memory-v5-2-before.XXXXXX.data)
data_after=$(mktemp /tmp/memory-v5-2-after.XXXXXX.data)
schema_before=$(mktemp /tmp/memory-v5-2-before.XXXXXX.schema)
schema_after=$(mktemp /tmp/memory-v5-2-after.XXXXXX.schema)
chmod 0600 "$backup" "$install_one" "$install_two" \
  "$data_before" "$data_after" "$schema_before" "$schema_after"

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists "$clone" \
    >/dev/null 2>&1 || true
  rm -f "$backup" "$install_one" "$install_two" \
    "$data_before" "$data_after" "$schema_before" "$schema_after"
}
trap cleanup EXIT

clone_sql() {
  docker exec -i "$container" psql -U sage -d "$clone" -X \
    -v ON_ERROR_STOP=1 "$@"
}

clone_scalar() {
  docker exec "$container" psql -U sage -d "$clone" -X -A -t \
    -v ON_ERROR_STOP=1 -c "$1"
}

production_scalar() {
  docker exec "$container" psql -U sage -d "$production" -X -A -t \
    -v ON_ERROR_STOP=1 -c "$1"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

normalized_dump() {
  docker exec "$container" pg_dump -U sage -d "$clone" "$@" \
    | sed -e '/^\\restrict /d' -e '/^\\unrestrict /d' \
      -e '/^-- Dumped from database version /d' \
      -e '/^-- Dumped by pg_dump version /d' \
      -e '/^-- Started on /d' -e '/^-- Completed on /d'
}

check_sha() {
  local path=$1
  local expected=$2
  [[ "$(sha256sum "$path" | awk '{print $1}')" == "$expected" ]]
}

for required in "$generator" "$registry" "$registry_rollback" \
  "$registry_test" "$persistence" "$persistence_rollback" \
  "$persistence_test"; do
  [[ -f "$required" ]]
done
check_sha "$generator" "$generator_sha"
check_sha "$registry" "$registry_sha"
check_sha "$registry_rollback" "$registry_rollback_sha"
check_sha "$registry_test" "$registry_test_sha"
check_sha "$persistence" "$persistence_sha"
check_sha "$persistence_rollback" "$persistence_rollback_sha"
check_sha "$persistence_test" "$persistence_test_sha"

/opt/chat-memory/venv/bin/python "$generator" --emit-install-sql >"$install_one"
/opt/chat-memory/venv/bin/python "$generator" --emit-install-sql >"$install_two"
cmp -s "$install_one" "$install_two"
rg -q "memory_predicate_registry_v5_2" "$install_one"

production_registry_before=$(production_scalar \
  "SELECT count(*) FROM memory.predicate_registry_version WHERE registry_version='memory_predicate_registry_v5_2'")
production_observations_before=$(production_scalar 'SELECT count(*) FROM memory.observation')
production_claims_before=$(production_scalar 'SELECT count(*) FROM memory.claim')
production_packets_before=$(production_scalar 'SELECT count(*) FROM memory.evidence_extraction_packet_v5_local')
qdrant_before=$(qdrant_signature)

docker exec "$container" pg_dump -U sage -d "$production" -Fc >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --exit-on-error <"$backup"

normalized_dump -a -n memory --no-owner --no-privileges >"$data_before"
normalized_dump -s -n memory --no-owner --no-privileges >"$schema_before"

clone_observations_before=$(clone_scalar 'SELECT count(*) FROM memory.observation')
clone_claims_before=$(clone_scalar 'SELECT count(*) FROM memory.claim')
clone_packets_before=$(clone_scalar 'SELECT count(*) FROM memory.evidence_extraction_packet_v5_local')
v5_function_before=$(clone_scalar "
  SELECT encode(public.digest(convert_to(pg_get_functiondef(
    'memory.persist_owner_v5_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure
  ),'UTF8'),'sha256'),'hex')
")
v5_1_function_before=$(clone_scalar "
  SELECT encode(public.digest(convert_to(pg_get_functiondef(
    'memory.persist_owner_v5_1_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure
  ),'UTF8'),'sha256'),'hex')
")

clone_sql <"$install_one" >/dev/null
clone_sql <"$install_one" >/dev/null
clone_sql <"$registry_test" >/dev/null
[[ "$(clone_scalar "SELECT count(*) FROM memory.predicate_contract WHERE registry_version='memory_predicate_registry_v5_2'")" == 85 ]]
[[ "$(clone_scalar 'SELECT count(*) FROM memory.relationship_predicate_contract_v5_2')" == 41 ]]

clone_sql <"$persistence" >/dev/null
clone_sql <"$persistence" >/dev/null
clone_sql <"$persistence_test" >/dev/null

[[ "$(clone_scalar "SELECT (
  to_regprocedure(
    'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'
  ) IS NOT NULL
  AND has_function_privilege(
    'brains_app',
    'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)',
    'EXECUTE'
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_proc AS procedure
    CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
    WHERE procedure.oid=
      'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure
      AND acl.grantee=0 AND acl.privilege_type='EXECUTE'
  )
  AND EXISTS (
    SELECT 1 FROM pg_proc
    WHERE oid=
      'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure
      AND prosecdef
      AND proowner='memory_v5_local_inference_maintainer'::regrole
      AND EXISTS (
        SELECT 1 FROM unnest(proconfig) AS setting
        WHERE setting LIKE 'search_path=%'
      )
  )
)::integer")" == 1 ]]

[[ "$(clone_scalar 'SELECT count(*) FROM memory.observation')" == "$clone_observations_before" ]]
[[ "$(clone_scalar 'SELECT count(*) FROM memory.claim')" == "$clone_claims_before" ]]
[[ "$(clone_scalar 'SELECT count(*) FROM memory.evidence_extraction_packet_v5_local')" == "$clone_packets_before" ]]
[[ "$(clone_scalar "
  SELECT encode(public.digest(convert_to(pg_get_functiondef(
    'memory.persist_owner_v5_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure
  ),'UTF8'),'sha256'),'hex')
")" == "$v5_function_before" ]]
[[ "$(clone_scalar "
  SELECT encode(public.digest(convert_to(pg_get_functiondef(
    'memory.persist_owner_v5_1_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure
  ),'UTF8'),'sha256'),'hex')
")" == "$v5_1_function_before" ]]

clone_sql <"$persistence_rollback" >/dev/null
clone_sql <"$registry_rollback" >/dev/null
[[ "$(clone_scalar "SELECT (
  to_regprocedure('memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)') IS NULL
  AND NOT EXISTS (
    SELECT 1 FROM memory.predicate_registry_version
    WHERE registry_version='memory_predicate_registry_v5_2'
  )
  AND to_regclass('memory.relationship_predicate_contract_v5_2') IS NULL
  AND to_regclass('memory.predicate_registry_source_binding_v5_2') IS NULL
)::integer")" == 1 ]]

normalized_dump -a -n memory --no-owner --no-privileges >"$data_after"
normalized_dump -s -n memory --no-owner --no-privileges >"$schema_after"
cmp -s "$data_before" "$data_after"
cmp -s "$schema_before" "$schema_after"

[[ "$(production_scalar "SELECT count(*) FROM memory.predicate_registry_version WHERE registry_version='memory_predicate_registry_v5_2'")" == "$production_registry_before" ]]
[[ "$(production_scalar 'SELECT count(*) FROM memory.observation')" == "$production_observations_before" ]]
[[ "$(production_scalar 'SELECT count(*) FROM memory.claim')" == "$production_claims_before" ]]
[[ "$(production_scalar 'SELECT count(*) FROM memory.evidence_extraction_packet_v5_local')" == "$production_packets_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

printf '%s\n' 'memory_v1_predicate_runtime_v5_2_production_clone: PASS'
