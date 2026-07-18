#!/usr/bin/env python3
from __future__ import annotations

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
OBSERVATIONS = (
    "43858045-c943-425c-a018-2fca175a722e",
    "3807e5bb-cf65-4c84-83a4-ab049f4a95d2",
    "fb05d48e-4dee-4ca4-a501-029f9ca8629b",
    "8ab3b466-d24a-4de8-a0ee-37b8bae15df2",
)
ASSESSOR = "memory_v1_claim_projection_v5_1_security_probe"


async def rejected(conn: asyncpg.Connection, call) -> None:
    savepoint = conn.transaction()
    await savepoint.start()
    try:
        await call()
    except asyncpg.PostgresError:
        await savepoint.rollback()
        return
    await savepoint.rollback()
    raise RuntimeError("forged or unauthorized call was accepted")


async def record_entailment(
    conn: asyncpg.Connection, observation: str, source: dict
) -> None:
    spans = stable_json(source["source_spans"])
    preflight = await conn.fetchrow(
        """SELECT * FROM memory.preflight_observation_entailment_v5(
            $1,'accepted'::memory.observation_entailment_decision_v5,
            'predicate_entailment_v5_1_accepted',$2::jsonb,
            'system',$3
        )""",
        uuid.UUID(observation),
        spans,
        ASSESSOR,
    )
    request_id = uuid.uuid5(
        uuid.NAMESPACE_URL, f"memory-v5-1-probe-entailment:{observation}"
    )
    values = (
        request_id,
        uuid.UUID(observation),
        spans,
        ASSESSOR,
        preflight["authorization_manifest_sha256"],
    )
    applied = await conn.fetchrow(
        """SELECT * FROM memory.record_observation_entailment_v5(
            $1,$2,'accepted'::memory.observation_entailment_decision_v5,
            'predicate_entailment_v5_1_accepted',$3::jsonb,
            'system',$4,$5
        )""",
        *values,
    )
    replay = await conn.fetchrow(
        """SELECT * FROM memory.record_observation_entailment_v5(
            $1,$2,'accepted'::memory.observation_entailment_decision_v5,
            'predicate_entailment_v5_1_accepted',$3::jsonb,
            'system',$4,$5
        )""",
        *values,
    )
    if applied["outcome"] != "applied" or applied["rows_written"] != 2:
        raise RuntimeError("entailment apply count mismatch")
    if replay["outcome"] != "replayed" or replay["rows_written"] != 0:
        raise RuntimeError("entailment replay was not zero-write")


async def main() -> None:
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    registry = load_contract()
    if not all(predicate in registry for predicate in {
        "identity.name", "pet.breed", "pet.sex", "relationship.has_pet"
    }):
        raise RuntimeError("required predicate registry entries are absent")
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("POSTGRES_DSN must authenticate as brains_app")
        await rejected(
            conn,
            lambda: conn.fetchrow(
                "SELECT * FROM memory.preflight_claim_projection_source_v5_1($1)",
                uuid.UUID(OBSERVATIONS[0]),
            ),
        )

        transaction = conn.transaction()
        await transaction.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", OWNER)
        first_packet = None
        for observation in OBSERVATIONS:
            source = await load_source(conn, uuid.UUID(observation))
            await record_entailment(conn, observation, source)
            projection = build_projection(OWNER, source)
            packet = build_packet(projection)
            plan_id = uuid.uuid5(
                uuid.NAMESPACE_URL, f"memory-v5-1-probe-plan:{observation}"
            )
            packet_text = stable_json(packet)
            manifest = owner_manifest_sha256(OWNER, packet["packet_sha256"])
            preflight = await conn.fetchrow(
                "SELECT * FROM memory.preflight_claim_projection_packet_v5_1($1,$2)",
                plan_id,
                packet_text,
            )
            if (
                preflight["owner_manifest_sha256"] != manifest
                or preflight["existing_claims"] != 0
                or preflight["existing_plans"] != 0
            ):
                raise RuntimeError("claim projection preflight is not clean")
            applied = await conn.fetchrow(
                "SELECT * FROM memory.stage_claim_projection_plan_v5_1($1,$2,$3)",
                plan_id,
                packet_text,
                manifest,
            )
            replay = await conn.fetchrow(
                "SELECT * FROM memory.stage_claim_projection_plan_v5_1($1,$2,$3)",
                plan_id,
                packet_text,
                manifest,
            )
            if applied["outcome"] != "applied" or applied["rows_written"] != 4:
                raise RuntimeError("claim projection staging count mismatch")
            if replay["outcome"] != "replayed" or replay["rows_written"] != 0:
                raise RuntimeError("claim projection replay was not zero-write")
            await conn.execute("SET CONSTRAINTS ALL DEFERRED")
            first_packet = first_packet or packet

        forged_projection = copy.deepcopy(first_packet["projections"][0])
        forged_projection["payload"]["canonical_text"] = "Forged canonical text."
        forged_packet = build_packet(forged_projection)
        await rejected(
            conn,
            lambda: conn.fetchrow(
                "SELECT * FROM memory.preflight_claim_projection_packet_v5_1($1,$2)",
                uuid.uuid4(),
                stable_json(forged_packet),
            ),
        )

        literal_source = await load_source(conn, uuid.UUID(OBSERVATIONS[1]))
        literal_projection = build_projection(OWNER, literal_source)
        literal_projection["identity"]["object_literal_sha256"] = "0" * 64
        literal_projection["identity"]["semantic_key_sha256"] = semantic_key_sha256(
            OWNER,
            "claim",
            literal_projection["identity"],
            literal_projection["payload"],
        )
        forged_literal_packet = build_packet(literal_projection)
        await rejected(
            conn,
            lambda: conn.fetchrow(
                "SELECT * FROM memory.preflight_claim_projection_packet_v5_1($1,$2)",
                uuid.uuid4(),
                stable_json(forged_literal_packet),
            ),
        )
        await transaction.rollback()

        transaction = conn.transaction(readonly=True)
        await transaction.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", OTHER)
        await rejected(
            conn,
            lambda: conn.fetchrow(
                "SELECT * FROM memory.preflight_claim_projection_source_v5_1($1)",
                uuid.UUID(OBSERVATIONS[0]),
            ),
        )
        await transaction.rollback()
    finally:
        await conn.close()
    print("memory_v1_v5_claim_projection_security_probe: PASS")


if __name__ == "__main__":
    asyncio.run(main())
