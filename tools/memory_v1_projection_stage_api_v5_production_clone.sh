#!/usr/bin/env bash
set -euo pipefail

compose=(docker compose -p memoryv1projectionstageapi -f docker-compose.ci.yml)
migration=ops/sql/20260716_memory_v1_projection_stage_api_v5.sql
rollback=ops/sql/20260716_memory_v1_projection_stage_api_v5_rollback.sql
test_sql=tests/memory_v1_projection_stage_api_v5.sql
backup=$(mktemp /tmp/memory-v1-projection-stage-api.XXXXXX.dump)

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
ALTER FUNCTION memory.preflight_projection_packet_v5(uuid,text)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.v5_sha256_valid(text) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.guard_projection_actor_v5() OWNER TO memory_v5_writer;
ALTER FUNCTION memory.guard_projection_plan_complete_v5()
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.guard_projection_item_complete_v5()
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.guard_v5_append_only() OWNER TO memory_v5_writer;
GRANT USAGE ON SCHEMA memory TO brains_app,memory_v5_writer;
GRANT USAGE ON TYPE
  memory.projection_lane_v5,
  memory.projection_target_action_v5,
  memory.projection_review_state_v5,
  memory.projection_observation_stance_v5,
  memory.projection_temporal_materialization_v5,
  memory.observation_polarity,
  memory.observation_modality,
  memory.observation_surface_policy
TO memory_v5_writer;
GRANT SELECT,INSERT ON
  memory.projection_plan,
  memory.projection_plan_item,
  memory.projection_claim_payload,
  memory.projection_plan_observation
TO memory_v5_writer;
GRANT SELECT ON
  memory.projection_preference_payload,
  memory.projection_project_payload,
  memory.projection_plan_relation,
  memory.projection_review,
  memory.projection_apply_event,
  memory.preference_revision_observation,
  memory.project_knowledge_revision_observation,
  memory.observation,
  memory.observation_entity_binding,
  memory.observation_temporal,
  memory.entity,
  memory.evidence,
  memory.claim
TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.guard_projection_actor_v5()
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.guard_projection_plan_complete_v5()
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.guard_projection_item_complete_v5()
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.guard_v5_append_only()
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.preflight_projection_packet_v5(uuid,text)
  TO brains_app;
SQL
run_sql <"$migration"
run_sql <"$migration"
run_sql <"$test_sql"
run_sql <"$rollback"
[[ $("${compose[@]}" exec -T postgres psql -X -A -t -U sage -d memory \
  -c "SELECT (to_regprocedure('memory.stage_projection_plan_v5(uuid,text,text)') IS NULL)::int") == 1 ]]
echo 'memory_v1_projection_stage_api_v5_production_clone: PASS'
