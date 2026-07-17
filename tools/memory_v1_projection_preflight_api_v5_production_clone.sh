#!/usr/bin/env bash
set -euo pipefail

compose=(docker compose -p memoryv1projectionpreflightapi -f docker-compose.ci.yml)
migration=ops/sql/20260716_memory_v1_projection_preflight_api_v5.sql
rollback=ops/sql/20260716_memory_v1_projection_preflight_api_v5_rollback.sql
test_sql=tests/memory_v1_projection_preflight_api_v5.sql
backup=$(mktemp /tmp/memory-v1-projection-preflight-api.XXXXXX.dump)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup"
}
trap cleanup EXIT
chmod 0600 "$backup"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory
}

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc --no-owner --no-privileges >"$backup"
[[ -s "$backup" ]]
"${compose[@]}" up -d --wait postgres
printf '%s\n' \
  'CREATE ROLE brains_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"

run_sql <<'SQL'
ALTER FUNCTION memory.current_actor_user_id() OWNER TO memory_v5_writer;
ALTER FUNCTION memory.require_v5_writer_context() OWNER TO memory_v5_writer;
ALTER FUNCTION memory.v5_jsonb_exact_keys(jsonb,text[])
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.v5_digest_text(text) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.v5_canonical_json_text(jsonb) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.v5_projection_semantic_key_sha256(
  uuid,memory.projection_lane_v5,uuid,text,text,uuid,text,
  memory.observation_polarity,memory.observation_modality,jsonb
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.v5_projection_owner_manifest_sha256(uuid,text)
  OWNER TO memory_v5_writer;
GRANT USAGE ON SCHEMA memory TO brains_app,memory_v5_writer;
GRANT USAGE ON TYPE
  memory.projection_lane_v5,
  memory.observation_polarity,
  memory.observation_modality
TO memory_v5_writer;
GRANT SELECT ON
  memory.observation,
  memory.observation_entity_binding,
  memory.observation_temporal,
  memory.entity,
  memory.evidence,
  memory.claim,
  memory.projection_plan
TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_v5_writer;
SQL

run_sql <"$migration"
run_sql <"$migration"
run_sql <"$test_sql"
owner_state=$("${compose[@]}" exec -T postgres psql -X -A -t -F '|' \
  -U sage -d memory -c "
    SELECT
      pg_get_userbyid((SELECT proowner FROM pg_proc
        WHERE oid='memory.preflight_projection_source_v5(uuid)'::regprocedure)),
      pg_get_userbyid((SELECT proowner FROM pg_proc
        WHERE oid='memory.preflight_projection_packet_v5(uuid,text)'::regprocedure));
  ")
[[ "$owner_state" == 'memory_v5_writer|memory_v5_writer' ]]
run_sql <"$rollback"
[[ $("${compose[@]}" exec -T postgres psql -X -A -t -U sage -d memory \
  -c "SELECT (to_regprocedure('memory.preflight_projection_source_v5(uuid)') IS NULL AND to_regprocedure('memory.preflight_projection_packet_v5(uuid,text)') IS NULL)::int") == 1 ]]
echo 'memory_v1_projection_preflight_api_v5_production_clone: PASS'
