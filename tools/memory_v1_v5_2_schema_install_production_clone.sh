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
protected_tables=$(mktemp /tmp/memory-v5-2-protected-tables.XXXXXX.txt)
chmod 0600 "$backup" "$registry_one" "$registry_two" \
  "$protected_before" "$protected_after" "$protected_tables"

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists "$clone" \
    >/dev/null 2>&1 || true
  rm -f "$backup" "$registry_one" "$registry_two" \
    "$protected_before" "$protected_after" "$protected_tables"
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

capture_protected_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    state=$(clone_scalar "
      SELECT count(*)::text || E'\\t' ||
             encode(public.digest(coalesce(string_agg(row_json,E'\\n'
               ORDER BY row_json),''),'sha256'),'hex')
      FROM (SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value) AS rows")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$protected_tables"
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

clone_scalar "
  SELECT table_name
  FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
    AND table_name NOT IN (
      'predicate_registry_version','predicate_registry_seed','predicate_contract'
    )
  ORDER BY table_name
" >"$protected_tables"
capture_protected_state "$protected_before"
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
  [[ "$test_file" != tests/memory_v1_governed_claim_reader_v2.sql ]] || continue
  clone_sql <"$test_file" >/dev/null
done

fixture=$(clone_scalar "
  SELECT concat_ws('|',claim.owner_user_id::text,claim.claim_id::text,
    claim.subject_entity_id::text,subject_entity.entity_type,
    coalesce(claim.object_entity_id::text,''),
    coalesce(object_entity.entity_type,''),
    coalesce((SELECT other.claim_id::text FROM memory.claim AS other
      WHERE other.owner_user_id<>claim.owner_user_id
      ORDER BY other.owner_user_id,other.claim_id LIMIT 1),'')
  )
  FROM memory.claim AS claim
  JOIN memory.entity AS subject_entity
    ON subject_entity.owner_user_id=claim.owner_user_id
   AND subject_entity.entity_id=claim.subject_entity_id
  LEFT JOIN memory.entity AS object_entity
    ON object_entity.owner_user_id=claim.owner_user_id
   AND object_entity.entity_id=claim.object_entity_id
  WHERE claim.status::text IN ('supported','uncertain','disputed')
    AND EXISTS (SELECT 1 FROM memory.projection_apply_event AS event
      WHERE event.owner_user_id=claim.owner_user_id
        AND event.resulting_claim_id=claim.claim_id AND event.outcome='applied')
  ORDER BY claim.owner_user_id,claim.claim_id LIMIT 1
")
[[ -n "$fixture" ]]
IFS='|' read -r target_owner target_claim subject_id subject_type \
  object_id object_type other_claim <<<"$fixture"
{
  printf '%s\n' 'SET SESSION AUTHORIZATION brains_app;'
  cat tests/memory_v1_governed_claim_reader_v2.sql
} | clone_sql \
  -v target_owner_user_id="$target_owner" \
  -v target_claim_id="$target_claim" \
  -v subject_entity_id="$subject_id" \
  -v subject_entity_type="$subject_type" \
  -v object_entity_id="$object_id" \
  -v object_entity_type="$object_type" \
  -v other_owner_claim_id="$other_claim" >/dev/null

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
  )::integer
")" == 1 ]]

capture_protected_state "$protected_after"
cmp -s "$protected_before" "$protected_after"
[[ "$(production_signature)" == "$production_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(production_scalar "
  SELECT count(*) FROM memory.predicate_registry_version
  WHERE registry_version='memory_predicate_registry_v5_2'
")" == "$registry_before" ]]

printf '%s\n' 'memory_v1_v5_2_schema_install_production_clone: PASS'
