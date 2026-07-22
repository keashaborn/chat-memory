#!/usr/bin/env bash
set -euo pipefail

# Server: seebx backend. Restores a fresh production backup into a disposable
# database, rehearses the schema-only V5.2 install, and proves production and
# Qdrant remain read-only.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

container=brains-postgres-1
production=memory
clone="memory_v5_2_schema_install_${$}"
generator=scripts/memory_v1_predicate_registry_v5_2.py
backup=$(mktemp /tmp/memory-v5-2-schema-install.XXXXXX.dump)
registry_one=$(mktemp /tmp/memory-v5-2-registry-one.XXXXXX.sql)
registry_two=$(mktemp /tmp/memory-v5-2-registry-two.XXXXXX.sql)
protected_before=$(mktemp /tmp/memory-v5-2-protected-before.XXXXXX.dump)
protected_after=$(mktemp /tmp/memory-v5-2-protected-after.XXXXXX.dump)
chmod 0600 "$backup" "$registry_one" "$registry_two" \
  "$protected_before" "$protected_after"

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists "$clone" \
    >/dev/null 2>&1 || true
  rm -f "$backup" "$registry_one" "$registry_two" \
    "$protected_before" "$protected_after"
}
trap cleanup EXIT

clone_sql() {
  docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
    -U sage -d "$clone" "$@"
}

clone_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$clone" -c "$1"
}

production_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$production" -c "$1"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

production_signature() {
  production_scalar "
    SELECT encode(public.digest(coalesce(string_agg(
      table_name||E'\\t'||row_count::text||E'\\t'||row_sha,E'\\n'
      ORDER BY table_name),''),'sha256'),'hex')
    FROM (
      SELECT table_name,
        (xpath('/row/count/text()',query_to_xml(format(
          'SELECT count(*) AS count FROM memory.%I',table_name
        ),false,true,'')))[1]::text::bigint AS row_count,
        encode(public.digest(convert_to(table_name,'UTF8'),'sha256'),'hex') AS row_sha
      FROM information_schema.tables
      WHERE table_schema='memory' AND table_type='BASE TABLE'
    ) AS state
  "
}

protected_dump() {
  local output=$1
  docker exec "$container" pg_dump -U sage -d "$clone" -a -n memory \
    --no-owner --no-privileges \
    --exclude-table=memory.predicate_registry_version \
    --exclude-table=memory.predicate_registry_seed \
    --exclude-table=memory.predicate_contract \
    --exclude-table=memory.predicate_registry_source_binding_v5_2 \
    --exclude-table=memory.relationship_predicate_contract_v5_2 \
    >"$output"
  sed -i \
    -e '/^\\restrict /d' -e '/^\\unrestrict /d' \
    -e '/^-- Dumped from database version /d' \
    -e '/^-- Dumped by pg_dump version /d' \
    -e '/^-- Started on /d' -e '/^-- Completed on /d' "$output"
}

migrations=(
  ops/sql/20260722_memory_v1_predicate_runtime_v5_2_local_persistence.sql
  ops/sql/20260722_memory_v1_governed_claim_reader_v2.sql
  ops/sql/20260722_memory_v1_relational_stage_v5_2.sql
  ops/sql/20260722_memory_v1_entity_resolution_reconciliation_v5_2.sql
  ops/sql/20260722_memory_v1_projection_dispatch_v5_2.sql
)
tests=(
  tests/memory_v1_predicate_registry_v5_2.sql
  tests/memory_v1_predicate_runtime_v5_2_local_persistence.sql
  tests/memory_v1_governed_claim_reader_v2.sql
  tests/memory_v1_v5_2_stage_preflight_api.sql
  tests/memory_v1_v5_2_schema_install_security.sql
)
for file in "$generator" "${migrations[@]}" "${tests[@]}"; do
  [[ -f "$file" ]]
done

production_before=$(production_signature)
qdrant_before=$(qdrant_signature)
registry_before=$(production_scalar "
  SELECT count(*) FROM memory.predicate_registry_version
  WHERE registry_version='memory_predicate_registry_v5_2'
")

docker exec "$container" pg_dump -U sage -d "$production" -Fc >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --exit-on-error <"$backup"

protected_dump "$protected_before"
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$generator" \
  --emit-install-sql >"$registry_one"
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$generator" \
  --emit-install-sql >"$registry_two"
cmp -s "$registry_one" "$registry_two"
clone_sql <"$registry_one" >/dev/null
clone_sql <"$registry_two" >/dev/null

for migration in "${migrations[@]}"; do
  clone_sql <"$migration" >/dev/null
  clone_sql <"$migration" >/dev/null
done
for test_file in "${tests[@]}"; do
  clone_sql <"$test_file" >/dev/null
done

[[ "$(clone_scalar "
  SELECT (
    (SELECT count(*) FROM memory.predicate_contract
      WHERE registry_version='memory_predicate_registry_v5_2')=85
    AND (SELECT count(*) FROM memory.relationship_predicate_contract_v5_2)=41
    AND (SELECT count(*) FROM memory.predicate_registry_source_binding_v5_2)=1
    AND NOT EXISTS (SELECT 1 FROM memory.observation
      WHERE predicate_registry_version='memory_predicate_registry_v5_2')
    AND NOT EXISTS (SELECT 1 FROM memory.entity_resolution_plan
      WHERE predicate_registry_version='memory_predicate_registry_v5_2')
    AND NOT EXISTS (SELECT 1 FROM memory.projection_plan
      WHERE predicate_registry_version='memory_predicate_registry_v5_2')
    AND NOT EXISTS (SELECT 1 FROM memory.entity_resolution_reconciliation_v5_2)
    AND NOT EXISTS (SELECT 1 FROM memory.claim_relation_v5)
    AND NOT EXISTS (SELECT 1 FROM memory.preference_relation_v5)
    AND NOT EXISTS (SELECT 1 FROM memory.project_knowledge_relation_v5)
    AND NOT EXISTS (SELECT 1 FROM memory.projection_dispatch_v5)
  )::integer
")" == 1 ]]

protected_dump "$protected_after"
cmp -s "$protected_before" "$protected_after"
[[ "$(production_signature)" == "$production_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(production_scalar "
  SELECT count(*) FROM memory.predicate_registry_version
  WHERE registry_version='memory_predicate_registry_v5_2'
")" == "$registry_before" ]]

printf '%s\n' 'memory_v1_v5_2_schema_install_production_clone: PASS'
