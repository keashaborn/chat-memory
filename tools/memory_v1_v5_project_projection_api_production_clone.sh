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
snapshot_dir=$(mktemp -d /tmp/memory-v1-v5-project-projection-snapshots.XXXXXX)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup"
  find "$snapshot_dir" -type f -delete
  rmdir "$snapshot_dir"
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

if [[ "$(scalar "SELECT count(*) FROM memory.observation_entailment_v5
  WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
    AND observation_id='258d8d96-2cbd-4296-b878-769c90533fae'::uuid")" == 0 ]]; then
  MEMORY_V1_PROJECT_ENTAILMENT_APPLY=authorized \
  MEMORY_V1_DB_CONTAINER=memoryv1v5projectprojectionclone-postgres-1 \
  MEMORY_V1_SNAPSHOT_DIR="$snapshot_dir" \
  "$repo_root/tools/memory_v1_project_observation_entailment_apply.sh"
else
  [[ "$(scalar "SELECT count(*) FROM memory.observation_entailment_v5
    WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
      AND observation_id='258d8d96-2cbd-4296-b878-769c90533fae'::uuid
      AND decision='accepted'
      AND reason_code='predicate_entailment_v5_1_accepted'
      AND authorization_manifest_sha256='6154ba22e45d7f4bc12a27688fb3f5eb38f9cc5cf0fda1c1e2f2141c71a9e089'")" == 1 ]]
fi

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
APPLY_REQUEST = "a58d8d96-2cbd-4296-b878-769c90533fae"
REVIEWER = "memory_v1_v5_project_projection_preparation_20260718"
REASON = (
    "direct user-endorsed architecture statement, exact trusted component "
    "scope, deterministic entailment accepted; phase-authorized project "
    "projection preparation"
)
REASON_CODES = json.dumps(
    [
        "direct_user_statement",
        "trusted_component_scope",
        "accepted_predicate_entailment",
        "phase_authorized_projection_preparation",
    ],
    separators=(",", ":"),
)


async def main():
    conn = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    try:
        transaction = conn.transaction()
        await transaction.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", OWNER)
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
        review_preflight = await conn.fetchrow(
            """SELECT * FROM memory.preflight_projection_review_v5(
                $1,'p01','authorized'::memory.projection_review_decision_v5,
                'system',$2,$3,$4::jsonb
            )""",
            uuid.UUID(PLAN),
            REVIEWER,
            REASON,
            REASON_CODES,
        )
        reviewed = await conn.fetchrow(
            """SELECT * FROM memory.review_projection_v5(
                $1,'p01','authorized'::memory.projection_review_decision_v5,
                'system',$2,$3,$4::jsonb,$5
            )""",
            uuid.UUID(PLAN),
            REVIEWER,
            REASON,
            REASON_CODES,
            review_preflight["authorization_manifest_sha256"],
        )
        assert reviewed["outcome"] == "applied"
        assert reviewed["rows_written"] == 1
        apply_preflight = await conn.fetchrow(
            "SELECT * FROM memory.preflight_projection_apply_v5($1,'p01',$2)",
            uuid.UUID(PLAN),
            reviewed["review_id"],
        )
        materialized = await conn.fetchrow(
            "SELECT * FROM memory.apply_projection_v5($1,$2,'p01',$3,$4)",
            uuid.UUID(APPLY_REQUEST),
            uuid.UUID(PLAN),
            reviewed["review_id"],
            apply_preflight["apply_manifest_sha256"],
        )
        assert materialized["outcome"] == "applied"
        assert str(materialized["lane"]) == "project_knowledge"
        assert materialized["revision_number"] == 1
        assert materialized["rows_written"] == 6
        review_replay = await conn.fetchrow(
            """SELECT * FROM memory.review_projection_v5(
                $1,'p01','authorized'::memory.projection_review_decision_v5,
                'system',$2,$3,$4::jsonb,$5
            )""",
            uuid.UUID(PLAN),
            REVIEWER,
            REASON,
            REASON_CODES,
            review_preflight["authorization_manifest_sha256"],
        )
        apply_replay = await conn.fetchrow(
            "SELECT * FROM memory.apply_projection_v5($1,$2,'p01',$3,$4)",
            uuid.UUID(APPLY_REQUEST),
            uuid.UUID(PLAN),
            reviewed["review_id"],
            apply_preflight["apply_manifest_sha256"],
        )
        assert review_replay["outcome"] == "replayed"
        assert review_replay["rows_written"] == 0
        assert apply_replay["outcome"] == "replayed"
        assert apply_replay["rows_written"] == 0
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
[[ "$(scalar "SELECT count(*) FROM memory.project_knowledge_head_v5 WHERE knowledge_key='architecture.memory_service'")" == 0 ]]
run_sql <"$repo_root/$rollback"
[[ "$(scalar "SELECT (
  to_regprocedure('memory.preflight_project_projection_source_v5(uuid)') IS NULL
  AND to_regprocedure('memory.preflight_project_projection_packet_v5(uuid,text)') IS NULL
  AND to_regprocedure('memory.stage_project_projection_plan_v5(uuid,text,text)') IS NULL
)::integer")" == 1 ]]

# Reinstall only inside the disposable clone and exercise the actual controlled
# apply/replay program across committed transactions.
run_sql <"$repo_root/$migration"
clone_dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:$port/memory"
runner_bundle="$snapshot_dir/runner-bundle.json"
runner_apply="$snapshot_dir/runner-apply.json"
runner_replay="$snapshot_dir/runner-replay.json"
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python \
  "$repo_root/scripts/memory_v1_v5_project_projection_preflight.py" \
  --owner 1240822d-ac9a-4096-95aa-e2b24d36ef50 \
  --observation-id 258d8d96-2cbd-4296-b878-769c90533fae \
  --plan-id c8e6f378-0f69-56b8-a032-e01af4411f8e \
  --knowledge-key architecture.memory_service --output "$runner_bundle" \
  >/dev/null
MEMORY_V1_PROJECT_PROJECTION_APPLY=authorized POSTGRES_DSN="$clone_dsn" \
  PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python \
  "$repo_root/scripts/memory_v1_v5_project_projection_apply.py" \
  --mode apply --bundle "$runner_bundle" \
  --request-id a3ac4965-c20a-540a-96ff-a497b98c51c1 \
  --output "$runner_apply" >/dev/null
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python \
  "$repo_root/scripts/memory_v1_v5_project_projection_apply.py" \
  --mode replay --bundle "$runner_bundle" \
  --request-id a3ac4965-c20a-540a-96ff-a497b98c51c1 \
  --prior-result "$runner_apply" --output "$runner_replay" >/dev/null
[[ "$(scalar "SELECT (
  (SELECT count(*) FROM memory.projection_plan
    WHERE plan_id='c8e6f378-0f69-56b8-a032-e01af4411f8e'::uuid)=1
  AND (SELECT count(*) FROM memory.projection_review
    WHERE plan_id='c8e6f378-0f69-56b8-a032-e01af4411f8e'::uuid)=1
  AND (SELECT count(*) FROM memory.project_knowledge_head_v5
    WHERE knowledge_key='architecture.memory_service')=1
  AND (SELECT count(*) FROM memory.project_knowledge_revision_v5
    WHERE knowledge_id=(SELECT knowledge_id FROM memory.project_knowledge_head_v5
      WHERE knowledge_key='architecture.memory_service'))=1
  AND (SELECT count(*) FROM memory.projection_apply_event
    WHERE request_id='a3ac4965-c20a-540a-96ff-a497b98c51c1'::uuid)=1
  AND (SELECT count(*) FROM memory.projection_dispatch_v5
    WHERE apply_event_id=(SELECT event_id FROM memory.projection_apply_event
      WHERE request_id='a3ac4965-c20a-540a-96ff-a497b98c51c1'::uuid))=1
)::integer")" == 1 ]]
[[ "$(jq -r '.database_writes' "$runner_replay")" == 0 ]]

printf '%s\n' 'memory_v1_v5_project_projection_api_production_clone: PASS'
