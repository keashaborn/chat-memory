#!/usr/bin/env bash
set -euo pipefail

# Server: seebx backend. Production is read-only. All registry writes occur in
# a disposable database restored from a fresh production backup.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source .env
set +a

container=brains-postgres-1
production=memory
clone="memory_predicate_registry_v5_1_${$}"
generator=scripts/memory_v1_predicate_registry_v5_1.py
integration=specs/memory_v1_predicate_registry_v5_1_integration.json
relationship_spec=specs/memory_v1_relationship_registry_v5_1.json
rollback=ops/sql/20260720_memory_v1_predicate_registry_v5_1_rollback.sql
test_sql=tests/memory_v1_predicate_registry_v5_1.sql
backup=$(mktemp /tmp/memory-predicate-v5-1.XXXXXX.dump)
install_one=$(mktemp /tmp/memory-predicate-v5-1-install.XXXXXX.sql)
install_two=$(mktemp /tmp/memory-predicate-v5-1-install.XXXXXX.sql)
data_before=$(mktemp /tmp/memory-predicate-v5-1-before.XXXXXX.data)
data_after=$(mktemp /tmp/memory-predicate-v5-1-after.XXXXXX.data)
schema_before=$(mktemp /tmp/memory-predicate-v5-1-before.XXXXXX.schema)
schema_after=$(mktemp /tmp/memory-predicate-v5-1-after.XXXXXX.schema)
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

for required in "$generator" "$integration" "$relationship_spec" \
  "$rollback" "$test_sql"; do
  [[ -f "$required" ]]
done
[[ -z "$(git status --porcelain)" ]]
[[ "$(sha256sum "$integration" | awk '{print $1}')" == \
  "12e132f03554adb041090fa74ed99c178a20c5625cade85dc1bfb52b43a61eaf" ]]
[[ "$(sha256sum "$relationship_spec" | awk '{print $1}')" == \
  "01d0045cf5607b55eb7d2b97611c5810b81c6098c6118435a2015d5cc8102a4f" ]]

venv/bin/python "$generator" --emit-install-sql >"$install_one"
venv/bin/python "$generator" --emit-install-sql >"$install_two"
cmp -s "$install_one" "$install_two"
rg -q "memory_predicate_registry_v5_1" "$install_one"

production_registry_before=$(docker exec "$container" psql -U sage -d \
  "$production" -X -A -t -c \
  "SELECT count(*) FROM memory.predicate_registry_version WHERE registry_version='memory_predicate_registry_v5_1'")
production_observations_before=$(docker exec "$container" psql -U sage -d \
  "$production" -X -A -t -c 'SELECT count(*) FROM memory.observation')
production_claims_before=$(docker exec "$container" psql -U sage -d \
  "$production" -X -A -t -c 'SELECT count(*) FROM memory.claim')
qdrant_before=$(qdrant_signature)

docker exec "$container" pg_dump -U sage -d "$production" -Fc >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --exit-on-error <"$backup"

normalized_dump -a -n memory --no-owner --no-privileges >"$data_before"
normalized_dump -s -n memory --no-owner --no-privileges >"$schema_before"

base_registry_before=$(clone_scalar \
  "SELECT row_to_json(value)::text FROM (SELECT * FROM memory.predicate_registry_version WHERE registry_version='memory_predicate_registry_v5') value")
base_contract_before=$(clone_scalar \
  "SELECT count(*)::text || ':' || md5(coalesce(string_agg(predicate || ':' || contract_sha256, ',' ORDER BY predicate),'')) FROM memory.predicate_contract WHERE registry_version='memory_predicate_registry_v5'")
clone_observations_before=$(clone_scalar 'SELECT count(*) FROM memory.observation')
clone_claims_before=$(clone_scalar 'SELECT count(*) FROM memory.claim')

clone_sql <"$install_one" >/dev/null
clone_sql <"$install_one" >/dev/null
clone_sql <"$test_sql" >/dev/null

[[ "$(clone_scalar "SELECT runtime_active::integer FROM memory.predicate_registry_version WHERE registry_version='memory_predicate_registry_v5_1'")" == "0" ]]
[[ "$(clone_scalar "SELECT count(*) FROM memory.predicate_contract WHERE registry_version='memory_predicate_registry_v5_1'")" == "82" ]]
[[ "$(clone_scalar 'SELECT count(*) FROM memory.relationship_predicate_contract_v5_1')" == "41" ]]
[[ "$(clone_scalar 'SELECT count(*) FROM memory.observation')" == "$clone_observations_before" ]]
[[ "$(clone_scalar 'SELECT count(*) FROM memory.claim')" == "$clone_claims_before" ]]
[[ "$(clone_scalar "SELECT row_to_json(value)::text FROM (SELECT * FROM memory.predicate_registry_version WHERE registry_version='memory_predicate_registry_v5') value")" == "$base_registry_before" ]]
[[ "$(clone_scalar "SELECT count(*)::text || ':' || md5(coalesce(string_agg(predicate || ':' || contract_sha256, ',' ORDER BY predicate),'')) FROM memory.predicate_contract WHERE registry_version='memory_predicate_registry_v5'")" == "$base_contract_before" ]]

clone_sql <"$rollback" >/dev/null
[[ "$(clone_scalar "SELECT count(*) FROM memory.predicate_registry_version WHERE registry_version='memory_predicate_registry_v5_1'")" == "0" ]]
[[ "$(clone_scalar "SELECT (to_regclass('memory.relationship_predicate_contract_v5_1') IS NULL AND to_regclass('memory.predicate_registry_source_binding_v5_1') IS NULL AND to_regprocedure('memory.reject_predicate_registry_v5_1_mutation()') IS NULL)::integer")" == "1" ]]

normalized_dump -a -n memory --no-owner --no-privileges >"$data_after"
normalized_dump -s -n memory --no-owner --no-privileges >"$schema_after"
cmp -s "$data_before" "$data_after"
cmp -s "$schema_before" "$schema_after"

[[ "$(docker exec "$container" psql -U sage -d "$production" -X -A -t -c \
  "SELECT count(*) FROM memory.predicate_registry_version WHERE registry_version='memory_predicate_registry_v5_1'")" == "$production_registry_before" ]]
[[ "$(docker exec "$container" psql -U sage -d "$production" -X -A -t -c \
  'SELECT count(*) FROM memory.observation')" == "$production_observations_before" ]]
[[ "$(docker exec "$container" psql -U sage -d "$production" -X -A -t -c \
  'SELECT count(*) FROM memory.claim')" == "$production_claims_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

printf '%s\n' 'memory_v1_predicate_registry_v5_1_production_clone: PASS'
