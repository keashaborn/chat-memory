#!/usr/bin/env bash
set -euo pipefail

compose=(docker compose -p memoryv1v5trustedselfprodclone -f docker-compose.ci.yml)
migration=ops/sql/20260716_memory_v1_trusted_self_apply_v5.sql
rollback=ops/sql/20260716_memory_v1_trusted_self_apply_v5_rollback.sql
test_sql=tests/memory_v1_trusted_self_apply_v5.sql
backup=$(mktemp /tmp/memory-v1-trusted-self-clone.XXXXXX.dump)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup"
}
trap cleanup EXIT
chmod 0600 "$backup"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 -U sage -d memory
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

# The clone intentionally strips production ACLs and ownership. Restore only
# the already-installed relational-writer security boundary needed by this
# test so the call runs as brains_app and the SECURITY DEFINER functions run
# as the restricted memory_v5_writer role, matching production.
run_sql <<'SQL'
ALTER FUNCTION memory.v5_digest_text(text) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.normalize_entity_name_v5(text) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.require_v5_writer_context() OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_entity_resolution_apply_v5(uuid, uuid)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.apply_entity_resolution_v5(uuid, uuid, uuid, text)
  OWNER TO memory_v5_writer;

GRANT USAGE ON SCHEMA memory TO brains_app, memory_v5_writer;
GRANT USAGE ON TYPE
  memory.entity_mention_kind,
  memory.entity_resolution_action,
  memory.entity_resolution_state,
  memory.entity_review_decision,
  memory.observation_polarity,
  memory.observation_modality,
  memory.observation_projection_class,
  memory.observation_surface_policy,
  memory.temporal_semantic,
  memory.temporal_shape,
  memory.temporal_basis,
  memory.temporal_source_form,
  memory.temporal_certainty,
  memory.temporal_precision,
  memory.evidence_kind,
  memory.record_status,
  memory.sensitivity_level
TO memory_v5_writer;

GRANT SELECT ON
  memory.predicate,
  memory.predicate_registry_version,
  memory.predicate_contract,
  memory.evidence,
  memory.entity,
  memory.entity_alias,
  memory.entity_mention,
  memory.entity_resolution_plan,
  memory.entity_resolution_candidate,
  memory.entity_resolution_review,
  memory.entity_resolution_apply,
  memory.entity_alias_observation,
  memory.observation,
  memory.observation_temporal,
  memory.observation_entity_binding,
  memory.relational_stage_batch,
  memory.relational_operation_request
TO memory_v5_writer;

GRANT INSERT ON
  memory.entity,
  memory.entity_mention,
  memory.entity_resolution_plan,
  memory.entity_resolution_candidate,
  memory.entity_resolution_review,
  memory.entity_resolution_apply,
  memory.entity_alias_observation,
  memory.observation,
  memory.observation_temporal,
  memory.observation_entity_binding,
  memory.relational_stage_batch,
  memory.relational_operation_request
TO memory_v5_writer;

GRANT UPDATE ON
  memory.evidence,
  memory.entity,
  memory.entity_resolution_plan,
  memory.entity_resolution_review
TO memory_v5_writer;

GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.guard_v5_resolution_apply()
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.guard_v5_observation_binding()
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.guard_v5_append_only()
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.preflight_entity_resolution_apply_v5(uuid, uuid)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.apply_entity_resolution_v5(uuid, uuid, uuid, text)
  TO brains_app;
SQL

run_sql < "$migration"
run_sql < "$migration"
run_sql < "$test_sql"

owner=$("${compose[@]}" exec -T postgres psql -X -A -t -U sage -d memory \
  -c "SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE oid='memory.apply_entity_resolution_v5(uuid,uuid,uuid,text)'::regprocedure")
[[ "$owner" == 'memory_v5_writer' ]]

run_sql < "$rollback"
definition=$("${compose[@]}" exec -T postgres psql -X -A -t -U sage -d memory \
  -c "SELECT pg_get_functiondef('memory.apply_entity_resolution_v5(uuid,uuid,uuid,text)'::regprocedure)")
[[ "$definition" == *"entity_row.entity_key <> 'self'"* ]]
[[ "$definition" != *"trusted_owner_self"* ]]

echo 'memory_v1_trusted_self_apply_v5_production_clone: PASS'
