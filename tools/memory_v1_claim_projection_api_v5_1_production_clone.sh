#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_CLAIM_PROJECTION_V5_1_CLONE_PORT:-55454}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(docker compose -p memoryv1claimprojectionv51clone \
  -f docker-compose.ci.yml -f docker-compose.stage-batch-clone.yml)
migration=ops/sql/20260718_memory_v1_claim_projection_api_v5_1.sql
rollback=ops/sql/20260718_memory_v1_claim_projection_api_v5_1_rollback.sql
backup=$(mktemp /tmp/memory-v1-claim-projection-v5-1.XXXXXX.dump)
review_tmp=

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup"
  if [[ -n "$review_tmp" && -d "$review_tmp" ]]; then
    find "$review_tmp" -type f -delete
    rmdir "$review_tmp"
  fi
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
  'CREATE ROLE memory_v5_extraction_scheduler_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_inference_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_review_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists <"$backup"

before_claims=$(scalar 'SELECT count(*) FROM memory.claim')
before_plans=$(scalar 'SELECT count(*) FROM memory.projection_plan')
before_entailments=$(scalar 'SELECT count(*) FROM memory.observation_entailment_v5')
before_requests=$(scalar 'SELECT count(*) FROM memory.relational_operation_request')
run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$migration"

[[ "$(scalar "SELECT count(*) FROM pg_proc WHERE oid IN (
  'memory.v5_claim_literal_value_v5_1(text,jsonb)'::regprocedure,
  'memory.render_claim_projection_text_v5_1(text,text,text,text,text,text,jsonb)'::regprocedure,
  'memory.preflight_claim_projection_source_v5_1(uuid)'::regprocedure,
  'memory.preflight_claim_projection_packet_v5_1(uuid,text)'::regprocedure,
  'memory.stage_claim_projection_plan_v5_1(uuid,text,text)'::regprocedure
) AND proowner='memory_v5_writer'::regrole")" == 5 ]]
[[ "$(scalar "SELECT (
  has_function_privilege('brains_app','memory.preflight_claim_projection_source_v5_1(uuid)','EXECUTE')
  AND has_function_privilege('brains_app','memory.preflight_claim_projection_packet_v5_1(uuid,text)','EXECUTE')
  AND has_function_privilege('brains_app','memory.stage_claim_projection_plan_v5_1(uuid,text,text)','EXECUTE')
  AND NOT has_function_privilege('public','memory.stage_claim_projection_plan_v5_1(uuid,text,text)','EXECUTE')
  AND NOT has_function_privilege('brains_app','memory.render_claim_projection_text_v5_1(text,text,text,text,text,text,jsonb)','EXECUTE')
)::integer")" == 1 ]]

PYTHONPATH="$repo_root/scripts" /opt/chat-memory/venv/bin/python \
  "$repo_root/scripts/memory_v1_v5_claim_projection_preflight_test.py"

POSTGRES_DSN="postgresql://brains_app:clone_only_brains_password@127.0.0.1:$port/memory" \
PYTHONPATH="$repo_root/scripts" /opt/chat-memory/venv/bin/python - <<'PY'
import asyncio
import copy
import os
import uuid

import asyncpg

from memory_v1_projection_v5_contract_test import (
    owner_manifest_sha256,
    semantic_key_sha256,
    stable_json,
)
from memory_v1_v5_claim_projection_preflight import (
    build_packet,
    build_projection,
    load_contract,
    load_source,
)

OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OTHER = "557ea042-cb82-48f8-9429-472e96c957ef"
OBSERVATIONS = [
    "43858045-c943-425c-a018-2fca175a722e",
    "3807e5bb-cf65-4c84-83a4-ab049f4a95d2",
    "fb05d48e-4dee-4ca4-a501-029f9ca8629b",
    "8ab3b466-d24a-4de8-a0ee-37b8bae15df2",
]


async def rejected(conn, call):
    savepoint = conn.transaction()
    await savepoint.start()
    try:
        await call()
    except asyncpg.PostgresError:
        await savepoint.rollback()
        return
    await savepoint.rollback()
    raise AssertionError("forged or unauthorized call was accepted")


async def main():
    registry = load_contract()
    assert all(name in registry for name in {
        "identity.name", "pet.breed", "pet.sex", "relationship.has_pet"
    })
    conn = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    try:
        await rejected(conn, lambda: conn.fetchrow(
            "SELECT * FROM memory.preflight_claim_projection_source_v5_1($1)",
            uuid.UUID(OBSERVATIONS[0]),
        ))

        tx = conn.transaction()
        await tx.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", OWNER)
        first_packet = None
        for index, observation in enumerate(OBSERVATIONS, start=1):
            source = await load_source(conn, uuid.UUID(observation))
            source_spans_text = stable_json(source["source_spans"])
            entailment = await conn.fetchrow(
                """SELECT * FROM memory.preflight_observation_entailment_v5(
                    $1,'accepted'::memory.observation_entailment_decision_v5,
                    'predicate_entailment_v5_1_accepted',$2::jsonb,
                    'system','memory_v1_claim_projection_v5_1_clone'
                )""",
                uuid.UUID(observation),
                source_spans_text,
            )
            entailment_request = uuid.uuid5(
                uuid.NAMESPACE_URL, f"memory-v5-1-entailment:{observation}"
            )
            accepted = await conn.fetchrow(
                """SELECT * FROM memory.record_observation_entailment_v5(
                    $1,$2,'accepted'::memory.observation_entailment_decision_v5,
                    'predicate_entailment_v5_1_accepted',$3::jsonb,
                    'system','memory_v1_claim_projection_v5_1_clone',$4
                )""",
                entailment_request,
                uuid.UUID(observation),
                source_spans_text,
                entailment["authorization_manifest_sha256"],
            )
            assert accepted["outcome"] == "applied" and accepted["rows_written"] == 2
            accepted_replay = await conn.fetchrow(
                """SELECT * FROM memory.record_observation_entailment_v5(
                    $1,$2,'accepted'::memory.observation_entailment_decision_v5,
                    'predicate_entailment_v5_1_accepted',$3::jsonb,
                    'system','memory_v1_claim_projection_v5_1_clone',$4
                )""",
                entailment_request,
                uuid.UUID(observation),
                source_spans_text,
                entailment["authorization_manifest_sha256"],
            )
            assert accepted_replay["outcome"] == "replayed"
            assert accepted_replay["rows_written"] == 0
            projection = build_projection(OWNER, source)
            packet = build_packet(projection)
            plan_id = uuid.uuid5(uuid.NAMESPACE_URL, f"memory-v5-1:{observation}")
            packet_text = stable_json(packet)
            preflight = await conn.fetchrow(
                "SELECT * FROM memory.preflight_claim_projection_packet_v5_1($1,$2)",
                plan_id, packet_text,
            )
            manifest = owner_manifest_sha256(OWNER, packet["packet_sha256"])
            assert preflight["owner_manifest_sha256"] == manifest
            assert preflight["existing_claims"] == 0
            assert preflight["existing_plans"] == 0
            applied = await conn.fetchrow(
                "SELECT * FROM memory.stage_claim_projection_plan_v5_1($1,$2,$3)",
                plan_id, packet_text, manifest,
            )
            assert applied["outcome"] == "applied" and applied["rows_written"] == 4
            replay = await conn.fetchrow(
                "SELECT * FROM memory.stage_claim_projection_plan_v5_1($1,$2,$3)",
                plan_id, packet_text, manifest,
            )
            assert replay["outcome"] == "replayed" and replay["rows_written"] == 0
            await conn.execute("SET CONSTRAINTS ALL DEFERRED")
            if index == 1:
                first_packet = packet

        forged_projection = copy.deepcopy(first_packet["projections"][0])
        forged_projection["payload"]["canonical_text"] = "Forged canonical text."
        forged_packet = build_packet(forged_projection)
        await rejected(conn, lambda: conn.fetchrow(
            "SELECT * FROM memory.preflight_claim_projection_packet_v5_1($1,$2)",
            uuid.uuid4(), stable_json(forged_packet),
        ))

        literal_source = await load_source(conn, uuid.UUID(OBSERVATIONS[1]))
        literal_projection = build_projection(OWNER, literal_source)
        literal_projection["identity"]["object_literal_sha256"] = "0" * 64
        literal_projection["identity"]["semantic_key_sha256"] = semantic_key_sha256(
            OWNER, "claim", literal_projection["identity"], literal_projection["payload"]
        )
        forged_literal_packet = build_packet(literal_projection)
        await rejected(conn, lambda: conn.fetchrow(
            "SELECT * FROM memory.preflight_claim_projection_packet_v5_1($1,$2)",
            uuid.uuid4(), stable_json(forged_literal_packet),
        ))
        await tx.rollback()

        tx = conn.transaction(readonly=True)
        await tx.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", OTHER)
        await rejected(conn, lambda: conn.fetchrow(
            "SELECT * FROM memory.preflight_claim_projection_source_v5_1($1)",
            uuid.UUID(OBSERVATIONS[0]),
        ))
        await tx.rollback()
    finally:
        await conn.close()


asyncio.run(main())
PY

[[ "$(scalar "SELECT count(*) FROM memory.projection_plan
  WHERE projector='memory_v1_deterministic_claim_projection_v5_1'")" == 0 ]]
[[ "$(scalar 'SELECT count(*) FROM memory.claim')" == "$before_claims" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.projection_plan')" == "$before_plans" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.observation_entailment_v5')" == "$before_entailments" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.relational_operation_request')" == "$before_requests" ]]
run_sql <"$repo_root/$rollback"
[[ "$(scalar "SELECT (
  to_regprocedure('memory.v5_claim_literal_value_v5_1(text,jsonb)') IS NULL
  AND to_regprocedure('memory.render_claim_projection_text_v5_1(text,text,text,text,text,text,jsonb)') IS NULL
  AND to_regprocedure('memory.preflight_claim_projection_source_v5_1(uuid)') IS NULL
  AND to_regprocedure('memory.preflight_claim_projection_packet_v5_1(uuid,text)') IS NULL
  AND to_regprocedure('memory.stage_claim_projection_plan_v5_1(uuid,text,text)') IS NULL
)::integer")" == 1 ]]

# Reinstall in the disposable clone, generate four zero-write bundles, then
# prove the reusable batch runner commits exactly 24 preparation rows and
# performs a zero-write replay across committed transaction boundaries.
run_sql <"$repo_root/$migration"
review_tmp=$(mktemp -d /home/ubuntu/memory-v1-reviews/.claim-projection-clone.XXXXXX)
chmod 0700 "$review_tmp"
clone_dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:$port/memory"
head=$(git -C "$repo_root" rev-parse HEAD)
declare -a bundle_args=()
while IFS=$'\t' read -r observation plan label; do
  bundle="$review_tmp/$label.json"
  POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root/scripts" \
    /opt/chat-memory/venv/bin/python \
    "$repo_root/scripts/memory_v1_v5_claim_projection_preflight.py" \
    --owner 1240822d-ac9a-4096-95aa-e2b24d36ef50 \
    --observation-id "$observation" --plan-id "$plan" \
    --output "$bundle" >/dev/null
  bundle_args+=(--bundle "$bundle")
done <<'EOF'
43858045-c943-425c-a018-2fca175a722e	ce967856-a164-526f-b6b9-25ae83272479	relationship
3807e5bb-cf65-4c84-83a4-ab049f4a95d2	d093f78f-1de7-5c60-b428-024381424ad7	name
fb05d48e-4dee-4ca4-a501-029f9ca8629b	4326e92c-bbdf-5529-8988-8bb88955a4bd	sex
8ab3b466-d24a-4de8-a0ee-37b8bae15df2	886c6391-159a-5a08-a284-42e32b1023f0	breed
EOF
manifest="$review_tmp/manifest.json"
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root/scripts" \
  /opt/chat-memory/venv/bin/python \
  "$repo_root/scripts/memory_v1_v5_claim_projection_stage_manifest.py" \
  --owner 1240822d-ac9a-4096-95aa-e2b24d36ef50 \
  --required-head "$head" \
  --assessor-ref memory_v1_claim_projection_v5_1_clone_batch \
  "${bundle_args[@]}" --output "$manifest" >/dev/null
preflight_result="$review_tmp/preflight.json"
apply_result="$review_tmp/apply.json"
replay_result="$review_tmp/replay.json"
POSTGRES_DSN="$clone_dsn" MEMORY_V1_REQUIRED_HEAD="$head" \
PYTHONPATH="$repo_root/scripts" /opt/chat-memory/venv/bin/python \
  "$repo_root/scripts/memory_v1_v5_claim_projection_stage_batch.py" \
  --mode preflight --manifest "$manifest" --output "$preflight_result" \
  >/dev/null
POSTGRES_DSN="$clone_dsn" MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_CLAIM_PROJECTION_STAGE_BATCH_APPLY=authorized \
PYTHONPATH="$repo_root/scripts" /opt/chat-memory/venv/bin/python \
  "$repo_root/scripts/memory_v1_v5_claim_projection_stage_batch.py" \
  --mode apply --manifest "$manifest" --output "$apply_result" >/dev/null
POSTGRES_DSN="$clone_dsn" MEMORY_V1_REQUIRED_HEAD="$head" \
PYTHONPATH="$repo_root/scripts" /opt/chat-memory/venv/bin/python \
  "$repo_root/scripts/memory_v1_v5_claim_projection_stage_batch.py" \
  --mode replay --manifest "$manifest" --output "$replay_result" >/dev/null
[[ "$(jq -r '.rows_written' "$preflight_result")" == 0 ]]
[[ "$(jq -r '.rows_written' "$apply_result")" == 24 ]]
[[ "$(jq -r '.rows_written' "$replay_result")" == 0 ]]
[[ "$(scalar "SELECT (
  (SELECT count(*) FROM memory.observation_entailment_v5
    WHERE observation_id=ANY(ARRAY[
      '43858045-c943-425c-a018-2fca175a722e',
      '3807e5bb-cf65-4c84-83a4-ab049f4a95d2',
      'fb05d48e-4dee-4ca4-a501-029f9ca8629b',
      '8ab3b466-d24a-4de8-a0ee-37b8bae15df2'
    ]::uuid[]))=4
  AND (SELECT count(*) FROM memory.projection_plan
    WHERE plan_id=ANY(ARRAY[
      'ce967856-a164-526f-b6b9-25ae83272479',
      'd093f78f-1de7-5c60-b428-024381424ad7',
      '4326e92c-bbdf-5529-8988-8bb88955a4bd',
      '886c6391-159a-5a08-a284-42e32b1023f0'
    ]::uuid[]))=4
  AND (SELECT count(*) FROM memory.projection_plan_item
    WHERE plan_id=ANY(ARRAY[
      'ce967856-a164-526f-b6b9-25ae83272479',
      'd093f78f-1de7-5c60-b428-024381424ad7',
      '4326e92c-bbdf-5529-8988-8bb88955a4bd',
      '886c6391-159a-5a08-a284-42e32b1023f0'
    ]::uuid[]))=4
  AND (SELECT count(*) FROM memory.claim)=$before_claims
)::integer")" == 1 ]]

printf '%s\n' 'memory_v1_claim_projection_api_v5_1_production_clone: PASS'
