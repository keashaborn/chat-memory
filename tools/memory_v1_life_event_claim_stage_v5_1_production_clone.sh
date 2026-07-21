#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the death-event projection compatibility layer
# on a disposable current-production clone and rolls all functional writes back.

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_LIFE_EVENT_STAGE_CLONE_PORT:-55475}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1lifeeventstageclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
migration=ops/sql/20260721_memory_v1_life_event_claim_projection_v5_1.sql
rollback=ops/sql/20260721_memory_v1_life_event_claim_projection_v5_1_rollback.sql
backup=$(mktemp /tmp/memory-v1-life-event-projection.XXXXXX.dump)
roles=$(mktemp /tmp/memory-v1-life-event-projection-roles.XXXXXX.sql)
review_dir=$(mktemp -d /home/ubuntu/memory-v1-reviews/life-event-stage-clone.XXXXXX)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$roles"
  rm -rf "$review_dir"
}
trap cleanup EXIT
chmod 0600 "$backup" "$roles"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory "$@"
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

docker exec brains-postgres-1 pg_dump -U sage -d memory -Fc >"$backup"
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
" >"$roles"
[[ -s "$roles" ]]

"${compose[@]}" up -d --wait postgres
run_sql <"$roles"
printf '%s\n' "ALTER ROLE brains_app PASSWORD 'clone_only_brains_password';" \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists <"$backup"

before_claims=$(scalar 'SELECT count(*) FROM memory.claim')
before_plans=$(scalar 'SELECT count(*) FROM memory.projection_plan')
before_entailments=$(scalar 'SELECT count(*) FROM memory.observation_entailment_v5')
before_requests=$(scalar 'SELECT count(*) FROM memory.relational_operation_request')

run_sql <"$migration"
run_sql <"$migration"
[[ "$(scalar "SELECT (
  to_regprocedure('memory.preflight_claim_projection_source_v5_1_base(uuid)') IS NOT NULL
  AND to_regprocedure('memory.preflight_claim_projection_packet_v5_1_base(uuid,text)') IS NOT NULL
  AND pg_get_userbyid((SELECT proowner FROM pg_proc WHERE oid=
    'memory.preflight_claim_projection_source_v5_1(uuid)'::regprocedure))='memory_v5_writer'
  AND has_function_privilege('brains_app',
    'memory.preflight_claim_projection_source_v5_1(uuid)','EXECUTE')
  AND has_function_privilege('brains_app',
    'memory.preflight_claim_projection_packet_v5_1(uuid,text)','EXECUTE')
  AND NOT has_function_privilege('public',
    'memory.preflight_claim_projection_packet_v5_1(uuid,text)','EXECUTE')
)::int")" == 1 ]]
[[ "$(scalar "SELECT memory.render_life_event_claim_text_v5_1(
  'animal','Neko','{}'::jsonb,'life_event.died',
  '{\"kind\":\"literal\",\"datatype\":\"boolean\",\"value\":true,\"unit\":null,\"approximate\":false}'::jsonb
)")" == 'Neko died.' ]]
[[ "$(scalar "SELECT memory.render_life_event_claim_text_v5_1(
  'person','mother','{\"identity_state\":\"role_only\",\"relationship_role\":\"family:mother\"}'::jsonb,
  'life_event.died',
  '{\"kind\":\"literal\",\"datatype\":\"boolean\",\"value\":true,\"unit\":null,\"approximate\":false}'::jsonb
)")" == "The user's mother died." ]]

PYTHONPATH="$repo_root/scripts" /opt/chat-memory/venv/bin/python \
  -m unittest -v tests.test_memory_v1_life_event_claim_projection_v5_1

POSTGRES_DSN="postgresql://brains_app:clone_only_brains_password@127.0.0.1:$port/memory" \
PYTHONPATH="$repo_root/scripts" /opt/chat-memory/venv/bin/python - <<'PY'
import asyncio
import os
import uuid

import asyncpg

from memory_v1_life_event_claim_projection_v5_1 import (
    build_packet,
    build_projection,
    load_source,
    owner_manifest,
    stable_json,
    validate_packet,
)

OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OTHER = "557ea042-cb82-48f8-9429-472e96c957ef"
OBSERVATION = uuid.UUID("324a30da-5c4f-4a00-9c09-57ced6eb26be")
PLAN = uuid.UUID("e58586f0-268e-5a8b-873b-30d03043db88")
ENTAILMENT_REQUEST = uuid.UUID("d7233593-a093-5b22-a017-32287d9ece4f")
SPANS = '[{"end":15,"start":0,"span_sha256":"acd95a13984a6dd531533e51dce8d24368c7b8ac97c87707b909c7fe822db4f0"}]'


async def rejected(conn, call):
    savepoint = conn.transaction()
    await savepoint.start()
    try:
        await call()
    except asyncpg.PostgresError:
        await savepoint.rollback()
        return
    await savepoint.rollback()
    raise AssertionError("unauthorized or stale life-event source was accepted")


async def main():
    conn = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    try:
        tx = conn.transaction()
        await tx.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", OWNER)
        await rejected(conn, lambda: load_source(conn, OBSERVATION))
        preflight = await conn.fetchrow(
            """SELECT * FROM memory.preflight_observation_entailment_v5(
              $1,'accepted'::memory.observation_entailment_decision_v5,
              'predicate_entailment_v5_1_accepted',$2::jsonb,
              'system','memory_v1_life_event_projection_clone'
            )""",
            OBSERVATION, SPANS,
        )
        accepted = await conn.fetchrow(
            """SELECT * FROM memory.record_observation_entailment_v5(
              $1,$2,'accepted'::memory.observation_entailment_decision_v5,
              'predicate_entailment_v5_1_accepted',$3::jsonb,
              'system','memory_v1_life_event_projection_clone',$4
            )""",
            ENTAILMENT_REQUEST, OBSERVATION, SPANS,
            preflight["authorization_manifest_sha256"],
        )
        assert accepted["outcome"] == "applied" and accepted["rows_written"] == 2
        source = await load_source(conn, OBSERVATION)
        assert source["canonical_text"] == "Neko died."
        projection = build_projection(OWNER, source)
        packet = build_packet(projection, source["predicate_registry_version"])
        validate_packet(packet, OWNER)
        packet_text = stable_json(packet)
        checked = await conn.fetchrow(
            "SELECT * FROM memory.preflight_claim_projection_packet_v5_1($1,$2)",
            PLAN, packet_text,
        )
        manifest = owner_manifest(OWNER, packet)
        assert checked["owner_manifest_sha256"] == manifest
        assert checked["existing_claims"] == 0 and checked["existing_plans"] == 0
        staged = await conn.fetchrow(
            "SELECT * FROM memory.stage_claim_projection_plan_v5_1($1,$2,$3)",
            PLAN, packet_text, manifest,
        )
        assert staged["outcome"] == "applied" and staged["rows_written"] == 4
        replay = await conn.fetchrow(
            "SELECT * FROM memory.stage_claim_projection_plan_v5_1($1,$2,$3)",
            PLAN, packet_text, manifest,
        )
        assert replay["outcome"] == "replayed" and replay["rows_written"] == 0
        await conn.execute("SELECT set_config('app.user_id',$1,true)", OTHER)
        await rejected(conn, lambda: load_source(conn, OBSERVATION))
        await tx.rollback()
    finally:
        await conn.close()


asyncio.run(main())
PY

[[ "$(scalar 'SELECT count(*) FROM memory.claim')" == "$before_claims" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.projection_plan')" == "$before_plans" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.observation_entailment_v5')" == "$before_entailments" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.relational_operation_request')" == "$before_requests" ]]

run_sql <"$rollback"
[[ "$(scalar "SELECT (
  to_regprocedure('memory.preflight_claim_projection_source_v5_1_base(uuid)') IS NULL
  AND to_regprocedure('memory.preflight_claim_projection_packet_v5_1_base(uuid,text)') IS NULL
  AND to_regprocedure('memory.render_life_event_claim_text_v5_1(text,text,jsonb,text,jsonb)') IS NULL
)::int")" == 1 ]]
run_sql <"$migration"

head=$(git -C "$repo_root" rev-parse HEAD)
clone_dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:$port/memory"
bundle="$review_dir/bundle.json"
preflight="$review_dir/preflight.json"
apply="$review_dir/apply.json"
replay="$review_dir/replay.json"
review_manifest="$review_dir/review-manifest.json"

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root/scripts" \
  /opt/chat-memory/venv/bin/python - <<'PY'
import asyncio
import os
import uuid

import asyncpg

OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OBSERVATION = uuid.UUID("324a30da-5c4f-4a00-9c09-57ced6eb26be")
REQUEST = uuid.UUID("0bfc5b5b-ced0-5ab0-9ce6-1b07b5bcb1ef")
SPANS = '[{"end":15,"start":0,"span_sha256":"acd95a13984a6dd531533e51dce8d24368c7b8ac97c87707b909c7fe822db4f0"}]'


async def main():
    conn = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    try:
        tx = conn.transaction(isolation="serializable")
        await tx.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", OWNER)
        preflight = await conn.fetchrow(
            """SELECT * FROM memory.preflight_observation_entailment_v5(
              $1,'accepted'::memory.observation_entailment_decision_v5,
              'predicate_entailment_v5_1_accepted',$2::jsonb,
              'system','memory_v1_life_event_stage_clone'
            )""",
            OBSERVATION, SPANS,
        )
        result = await conn.fetchrow(
            """SELECT * FROM memory.record_observation_entailment_v5(
              $1,$2,'accepted'::memory.observation_entailment_decision_v5,
              'predicate_entailment_v5_1_accepted',$3::jsonb,
              'system','memory_v1_life_event_stage_clone',$4
            )""",
            REQUEST, OBSERVATION, SPANS,
            preflight["authorization_manifest_sha256"],
        )
        assert result["outcome"] == "applied" and result["rows_written"] == 2
        await tx.commit()
    finally:
        await conn.close()


asyncio.run(main())
PY

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root/scripts" \
  /opt/chat-memory/venv/bin/python \
  "$repo_root/scripts/memory_v1_life_event_claim_bundle_v5_1.py" \
  --owner 1240822d-ac9a-4096-95aa-e2b24d36ef50 \
  --observation-id 324a30da-5c4f-4a00-9c09-57ced6eb26be \
  --plan-id 59add5ad-d708-5fca-9012-ebd71eed2a67 \
  --required-head "$head" --output "$bundle"

POSTGRES_DSN="$clone_dsn" MEMORY_V1_REQUIRED_HEAD="$head" \
PYTHONPATH="$repo_root/scripts" /opt/chat-memory/venv/bin/python \
  "$repo_root/scripts/memory_v1_life_event_claim_stage_v5_1.py" \
  --mode preflight --bundle "$bundle" --output "$preflight"
[[ "$(jq -er '.rows_written' "$preflight")" == 0 ]]

POSTGRES_DSN="$clone_dsn" MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_LIFE_EVENT_CLAIM_STAGE_APPLY=authorized \
PYTHONPATH="$repo_root/scripts" /opt/chat-memory/venv/bin/python \
  "$repo_root/scripts/memory_v1_life_event_claim_stage_v5_1.py" \
  --mode apply --bundle "$bundle" --output "$apply"
[[ "$(jq -er '.rows_written' "$apply")" == 4 ]]

POSTGRES_DSN="$clone_dsn" MEMORY_V1_REQUIRED_HEAD="$head" \
PYTHONPATH="$repo_root/scripts" /opt/chat-memory/venv/bin/python \
  "$repo_root/scripts/memory_v1_life_event_claim_stage_v5_1.py" \
  --mode replay --bundle "$bundle" --output "$replay"
[[ "$(jq -er '.rows_written' "$replay")" == 0 ]]
[[ "$(jq -er '.outcome' "$replay")" == replayed ]]

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root/scripts" \
  /opt/chat-memory/venv/bin/python \
  "$repo_root/scripts/memory_v1_v5_claim_projection_review_manifest.py" \
  --owner 1240822d-ac9a-4096-95aa-e2b24d36ef50 \
  --required-head "$head" --bundle "$bundle" --output "$review_manifest"
[[ "$(jq -er '.items | length' "$review_manifest")" == 1 ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_plan WHERE plan_id='59add5ad-d708-5fca-9012-ebd71eed2a67'::uuid")" == 1 ]]

echo 'memory_v1_life_event_claim_stage_v5_1_production_clone: PASS'
