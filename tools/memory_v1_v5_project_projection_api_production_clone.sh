#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_PROJECT_PROJECTION_CLONE_PORT:-55451}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(docker compose -p memoryv1v5projectprojectionclone \
  -f docker-compose.ci.yml -f docker-compose.stage-batch-clone.yml)
migration=ops/sql/20260718_memory_v1_v5_project_projection_api.sql
rollback=ops/sql/20260718_memory_v1_v5_project_projection_api_rollback.sql
backup=$(mktemp /tmp/memory-v1-v5-project-projection.XXXXXX.dump)

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
scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

docker exec brains-postgres-1 pg_dump -U sage -d memory -Fc >"$backup"
[[ -s "$backup" ]]
"${compose[@]}" up -d --wait postgres
printf '%s\n' \
  "CREATE ROLE brains_app LOGIN PASSWORD 'clone_only_brains_password' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;" \
  'CREATE ROLE memory_evidence_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_review_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_trace_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_intake_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_extraction_queue_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_extraction_worker_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_extraction_retry_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_extraction_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists <"$backup"

run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$migration"

[[ "$(scalar "SELECT count(*) FROM pg_proc WHERE oid IN (
  'memory.preflight_project_projection_source_v5(uuid)'::regprocedure,
  'memory.preflight_project_projection_packet_v5(uuid,text)'::regprocedure,
  'memory.stage_project_projection_plan_v5(uuid,text,text)'::regprocedure
) AND prosecdef AND proowner='memory_v5_writer'::regrole
  AND proconfig @> ARRAY['search_path=\"\"']::text[]")" == 3 ]]
[[ "$(scalar "SELECT (
  has_function_privilege('brains_app','memory.preflight_project_projection_source_v5(uuid)','EXECUTE')
  AND has_function_privilege('brains_app','memory.preflight_project_projection_packet_v5(uuid,text)','EXECUTE')
  AND has_function_privilege('brains_app','memory.stage_project_projection_plan_v5(uuid,text,text)','EXECUTE')
  AND NOT has_function_privilege('public','memory.stage_project_projection_plan_v5(uuid,text,text)','EXECUTE')
)::integer")" == 1 ]]

POSTGRES_DSN="postgresql://brains_app:clone_only_brains_password@127.0.0.1:$port/memory" \
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python - <<'PY'
import asyncio
import json
import os
import uuid

import asyncpg

from scripts.memory_v1_projection_v5_contract_test import (
    owner_manifest_sha256,
    stable_json,
    validate_packet,
)
from scripts.memory_v1_v5_project_projection_preflight import (
    build_packet,
    build_projection,
    load_contract,
    validate_source,
)

OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OTHER = "557ea042-cb82-48f8-9429-472e96c957ef"
OBSERVATION = "258d8d96-2cbd-4296-b878-769c90533fae"
PLAN = "758d8d96-2cbd-4296-b878-769c90533fae"
ENTAILMENT_REQUEST = "29beddb5-e645-52b9-af9b-2dbe74313d9a"
ENTAILMENT_MANIFEST = "6154ba22e45d7f4bc12a27688fb3f5eb38f9cc5cf0fda1c1e2f2141c71a9e089"
ENTAILMENT_SPANS = json.dumps(
    [{
        "start": 47,
        "end": 140,
        "span_sha256": "37333a4515d5b148abb3222e31d7966b56160f112bc20c2ad0847e777b29fafd",
    }],
    separators=(",", ":"),
)


async def main():
    conn = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    try:
        transaction = conn.transaction()
        await transaction.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", OWNER)
        entailment = await conn.fetchrow(
            """SELECT * FROM memory.record_observation_entailment_v5(
                $1,$2,'accepted'::memory.observation_entailment_decision_v5,
                'predicate_entailment_v5_1_accepted',$3::jsonb,
                'system','memory_v1_predicate_entailment_v5_1',$4
            )""",
            uuid.UUID(ENTAILMENT_REQUEST),
            uuid.UUID(OBSERVATION),
            ENTAILMENT_SPANS,
            ENTAILMENT_MANIFEST,
        )
        assert entailment["outcome"] == "applied"
        assert entailment["rows_written"] == 2
        row = await conn.fetchrow(
            "SELECT * FROM memory.preflight_project_projection_source_v5($1)",
            uuid.UUID(OBSERVATION),
        )
        source = dict(row)
        if isinstance(source["object_literal"], str):
            source["object_literal"] = json.loads(source["object_literal"])
        validate_source(source)
        projection = build_projection(
            OWNER, source, "architecture.memory_service"
        )
        packet = build_packet(projection)
        validate_packet(packet, OWNER, load_contract())
        packet_text = stable_json(packet)
        preflight = await conn.fetchrow(
            "SELECT * FROM memory.preflight_project_projection_packet_v5($1,$2)",
            uuid.UUID(PLAN), packet_text,
        )
        manifest = owner_manifest_sha256(OWNER, packet["packet_sha256"])
        assert preflight["owner_manifest_sha256"] == manifest
        assert preflight["existing_aggregates"] == 0
        assert preflight["existing_plans"] == 0
        applied = await conn.fetchrow(
            "SELECT * FROM memory.stage_project_projection_plan_v5($1,$2,$3)",
            uuid.UUID(PLAN), packet_text, manifest,
        )
        assert applied["outcome"] == "applied"
        assert applied["rows_written"] == 4
        replay = await conn.fetchrow(
            "SELECT * FROM memory.stage_project_projection_plan_v5($1,$2,$3)",
            uuid.UUID(PLAN), packet_text, manifest,
        )
        assert replay["outcome"] == "replayed"
        assert replay["rows_written"] == 0
        await transaction.rollback()

        transaction = conn.transaction(readonly=True)
        await transaction.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", OTHER)
        denied = False
        try:
            await conn.fetchrow(
                "SELECT * FROM memory.preflight_project_projection_source_v5($1)",
                uuid.UUID(OBSERVATION),
            )
        except asyncpg.PostgresError:
            denied = True
        await transaction.rollback()
        assert denied
    finally:
        await conn.close()


asyncio.run(main())
PY

[[ "$(scalar "SELECT count(*) FROM memory.projection_plan WHERE projector='memory_v1_deterministic_project_projection_v5'")" == 0 ]]
run_sql <"$repo_root/$rollback"
[[ "$(scalar "SELECT (
  to_regprocedure('memory.preflight_project_projection_source_v5(uuid)') IS NULL
  AND to_regprocedure('memory.preflight_project_projection_packet_v5(uuid,text)') IS NULL
  AND to_regprocedure('memory.stage_project_projection_plan_v5(uuid,text,text)') IS NULL
)::integer")" == 1 ]]

printf '%s\n' 'memory_v1_v5_project_projection_api_production_clone: PASS'
