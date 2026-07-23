#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Verifies the employment canary audit query against a
# disposable production clone. It has no inference or model-call path.

repo=/opt/chat-memory
container=brains-postgres-1
port=${MEMORY_V1_V5_2_VERIFY_CLONE_PORT:-55467}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(docker compose -p memoryv1v52employmentverify
  -f "$repo/docker-compose.ci.yml"
  -f "$repo/docker-compose.stage-batch-clone.yml")
backup=$(mktemp /tmp/memory-v1-v5-2-employment-verify.XXXXXX.dump)
checks=$(mktemp /tmp/memory-v1-v5-2-employment-checks.XXXXXX.json)
clone_started=0

cleanup() {
  code=$?
  if [[ "$clone_started" -eq 1 ]]; then
    "${compose[@]}" down -v >/dev/null 2>&1 || code=1
  fi
  rm -f "$backup" "$checks"
  exit "$code"
}
trap cleanup EXIT

production_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

clone_scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

clone_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory
}

cd "$repo"
docker exec "$container" pg_dump -U sage -d memory \
  -Fc >"$backup"
[[ -s "$backup" ]]
"${compose[@]}" up -d --wait postgres
clone_started=1

role_sql=$(production_scalar "
  SELECT format(
    'CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;',
    rolname)
  FROM pg_roles
  WHERE (rolname LIKE 'memory\\_%' ESCAPE '\\'
         OR rolname='lifeswitch_training_observation_owner')
  ORDER BY rolname")
printf '%s\n' \
  "CREATE ROLE brains_app LOGIN PASSWORD 'clone_only_brains_password' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;" \
  "$role_sql" | clone_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists <"$backup"

[[ "$(clone_scalar "
  SELECT count(*)
  FROM information_schema.columns
  WHERE table_schema='memory'
    AND table_name='evidence_extraction_packet_v5_local'
    AND column_name='normalized_packet'")" == 1 ]]

clone_scalar "
  WITH packet AS (
    SELECT '{
      \"contract_version\":\"memory_v1_relational_extraction_v5_2\",
      \"predicate_registry_version\":\"memory_predicate_registry_v5_2\",
      \"entity_mentions\":[
        {\"entity_ref\":\"e00\",\"entity_type\":\"self\",
         \"mention_kind\":\"self_reference\"},
        {\"entity_ref\":\"e01\",\"entity_type\":\"organization\",
         \"mention_kind\":\"named_entity\",
         \"name_text\":\"Wisconsin Early Autism Project\"}
      ],
      \"observations\":[{
        \"predicate\":\"employment.worked_for\",
        \"subject_entity_ref\":\"e00\",
        \"object\":{\"kind\":\"entity\",\"entity_ref\":\"e01\"},
        \"temporal\":{\"semantic\":\"state_validity\",\"shape\":\"none\",
          \"certainty\":\"unknown\",\"instant\":null,\"calendar_range\":null,
          \"instant_range\":null,\"relative_offset\":null,\"recurrence\":null,
          \"anchored_to_source_time\":false}
      }],
      \"comparison_hints\":[],\"deferrals\":[],\"packet_findings\":[]
    }'::jsonb AS normalized_packet
  ),
  self_entity AS (
    SELECT item->>'entity_ref' AS entity_ref
    FROM packet,jsonb_array_elements(
      normalized_packet->'entity_mentions') AS item
    WHERE item->>'entity_type'='self'
      AND item->>'mention_kind'='self_reference'
  ),
  employer AS (
    SELECT item->>'entity_ref' AS entity_ref
    FROM packet,jsonb_array_elements(
      normalized_packet->'entity_mentions') AS item
    WHERE item->>'entity_type'='organization'
      AND item->>'name_text'='Wisconsin Early Autism Project'
  ),
  observation AS (
    SELECT item
    FROM packet,jsonb_array_elements(
      normalized_packet->'observations') AS item
  )
  SELECT jsonb_build_object(
    'entity_mentions',jsonb_array_length(
      normalized_packet->'entity_mentions'),
    'observations',jsonb_array_length(
      normalized_packet->'observations'),
    'comparison_hints',jsonb_array_length(
      normalized_packet->'comparison_hints'),
    'deferrals',jsonb_array_length(normalized_packet->'deferrals'),
    'packet_findings',jsonb_array_length(
      normalized_packet->'packet_findings'),
    'self_entity',(SELECT count(*)=1 FROM self_entity),
    'organization_entity',(SELECT count(*)=1 FROM employer),
    'organization_name_exact',(SELECT count(*)=1 FROM employer),
    'employment_link',(
      SELECT count(*)=1
      FROM observation o
      WHERE o.item->>'predicate'='employment.worked_for'
        AND o.item->>'subject_entity_ref'=(
          SELECT entity_ref FROM self_entity)
        AND o.item->'object'->>'kind'='entity'
        AND o.item->'object'->>'entity_ref'=(
          SELECT entity_ref FROM employer)),
    'observation_predicates',(
      SELECT jsonb_agg(item->>'predicate' ORDER BY item->>'predicate')
      FROM observation),
    'no_current_employment_invention',(
      SELECT count(*)=1
      FROM observation o
      WHERE o.item->>'predicate'='employment.worked_for'
        AND o.item->'temporal'->>'semantic'='state_validity'
        AND o.item->'temporal'->>'shape'='none'
        AND o.item->'temporal'->>'certainty'='unknown'),
    'no_date_invention',(
      SELECT count(*)=1
      FROM observation o
      WHERE o.item->>'predicate'='employment.worked_for'
        AND o.item->'temporal'->'instant'='null'::jsonb
        AND o.item->'temporal'->'calendar_range'='null'::jsonb
        AND o.item->'temporal'->'instant_range'='null'::jsonb
        AND o.item->'temporal'->'relative_offset'='null'::jsonb
        AND o.item->'temporal'->'recurrence'='null'::jsonb
        AND o.item->'temporal'->>'anchored_to_source_time'='false'))
  FROM packet" >"$checks"

jq -e '
  .entity_mentions==2 and .observations==1 and
  .comparison_hints==0 and .deferrals==0 and .packet_findings==0 and
  .self_entity==true and .organization_entity==true and
  .organization_name_exact==true and .employment_link==true and
  .observation_predicates==["employment.worked_for"] and
  .no_current_employment_invention==true and
  .no_date_invention==true
' "$checks" >/dev/null

"${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
  -U sage -d memory \
  <"$repo/tests/memory_v1_v5_2_employment_compiler_v6.sql" >/dev/null

printf '%s\n' \
  'memory_v1_v5_2_employment_harness_zero_call_clone: PASS' \
  'local_model_calls=0' \
  'external_model_calls=0'
