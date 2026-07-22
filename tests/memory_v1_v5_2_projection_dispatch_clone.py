#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import uuid

import asyncpg

from scripts.memory_v1_projection_v5_2_contract import (
    owner_manifest_sha256,
    validate_packet,
    validate_projection_schema,
    validate_registry,
)
from scripts.memory_v1_v5_2_projection_dispatch import build_packet, stable_json


OWNER_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
OWNER_B = uuid.UUID("22222222-2222-4222-8222-222222222222")
PLAN_IDS = {
    "name": uuid.UUID("a3000000-0000-4000-8000-000000000001"),
    "stance": uuid.UUID("a3000000-0000-4000-8000-000000000002"),
}
ENTAILMENT_REQUEST_IDS = {
    "name": uuid.UUID("a4000000-0000-4000-8000-000000000001"),
    "stance": uuid.UUID("a4000000-0000-4000-8000-000000000002"),
}
SOURCE_TEXT = {
    "name": "My name is Avery.",
    "stance": "I think expert consensus should be treated as evidence, not absolute fact.",
}
ROOT = Path(__file__).resolve().parents[1]


async def actor(conn: asyncpg.Connection, owner: uuid.UUID) -> None:
    await conn.execute("SELECT set_config('app.user_id',$1,false)", str(owner))


async def source_for_observation(
    conn: asyncpg.Connection, observation_id: uuid.UUID
) -> dict:
    row = await conn.fetchrow(
        "SELECT * FROM memory.preflight_projection_source_v5_2($1)", observation_id
    )
    if row is None:
        raise RuntimeError("V5.2 source preflight returned no row")
    return dict(row)


def source_spans(name: str) -> list[dict[str, object]]:
    value = SOURCE_TEXT[name]
    return [
        {
            "start": 0,
            "end": len(value),
            "span_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        }
    ]


async def record_entailment(
    conn: asyncpg.Connection, name: str, observation_id: uuid.UUID
) -> None:
    spans_text = stable_json(source_spans(name))
    preflight = await conn.fetchrow(
        """
        SELECT * FROM memory.preflight_observation_entailment_v5(
          $1,'accepted','predicate_entailment_v5_1_accepted',$2::jsonb,
          'admin','synthetic_v5_2_projection_clone'
        )
        """,
        observation_id,
        spans_text,
    )
    if preflight is None:
        raise RuntimeError("entailment preflight returned no row")
    result = await conn.fetchrow(
        """
        SELECT * FROM memory.record_observation_entailment_v5(
          $1,$2,'accepted','predicate_entailment_v5_1_accepted',$3::jsonb,
          'admin','synthetic_v5_2_projection_clone',$4
        )
        """,
        ENTAILMENT_REQUEST_IDS[name],
        observation_id,
        spans_text,
        preflight["authorization_manifest_sha256"],
    )
    if result is None or result["outcome"] != "applied" or result["rows_written"] != 2:
        raise RuntimeError("synthetic V5.2 entailment did not apply exactly two rows")
    replay = await conn.fetchrow(
        """
        SELECT * FROM memory.record_observation_entailment_v5(
          $1,$2,'accepted','predicate_entailment_v5_1_accepted',$3::jsonb,
          'admin','synthetic_v5_2_projection_clone',$4
        )
        """,
        ENTAILMENT_REQUEST_IDS[name],
        observation_id,
        spans_text,
        preflight["authorization_manifest_sha256"],
    )
    if replay is None or replay["outcome"] != "replayed" or replay["rows_written"] != 0:
        raise RuntimeError("synthetic V5.2 entailment replay wrote rows")


async def main() -> int:
    dsn = os.environ["POSTGRES_DSN"]
    schema = json.loads(
        (ROOT / "specs/memory_v1_projection_plan_v5_2.schema.json").read_text()
    )
    registry = json.loads(
        (ROOT / "specs/memory_v1_predicate_registry_v5_2.json").read_text()
    )
    validate_projection_schema(schema)
    registry_by_name = validate_registry(registry)
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("clone DSN must use brains_app")
        await actor(conn, OWNER_A)
        observation_ids = {
            "name": uuid.UUID(os.environ["V5_2_NAME_OBSERVATION_ID"]),
            "stance": uuid.UUID(os.environ["V5_2_STANCE_OBSERVATION_ID"]),
        }
        packets: dict[str, tuple[dict, str]] = {}
        for name, observation_id in observation_ids.items():
            await record_entailment(conn, name, observation_id)
            source = await source_for_observation(conn, observation_id)
            packet = build_packet(str(OWNER_A), source)
            validate_packet(packet, str(OWNER_A), registry_by_name)
            packet_text = stable_json(packet)
            expected_manifest = owner_manifest_sha256(
                str(OWNER_A), packet["packet_sha256"]
            )
            preflight = await conn.fetchrow(
                "SELECT * FROM memory.preflight_projection_packet_v5_2($1,$2)",
                PLAN_IDS[name],
                packet_text,
            )
            if preflight is None or preflight["existing_aggregates"] != 0:
                raise RuntimeError("V5.2 projection preflight is not empty")
            if preflight["owner_manifest_sha256"] != expected_manifest:
                raise RuntimeError("owner manifest mismatch")
            result = await conn.fetchrow(
                "SELECT * FROM memory.stage_projection_plan_v5_2($1,$2,$3)",
                PLAN_IDS[name],
                packet_text,
                expected_manifest,
            )
            if result is None or result["outcome"] != "applied" or result["rows_written"] != 4:
                raise RuntimeError("V5.2 projection did not stage exactly four rows")
            replay = await conn.fetchrow(
                "SELECT * FROM memory.stage_projection_plan_v5_2($1,$2,$3)",
                PLAN_IDS[name],
                packet_text,
                expected_manifest,
            )
            if replay is None or replay["outcome"] != "replayed" or replay["rows_written"] != 0:
                raise RuntimeError("V5.2 projection replay wrote rows")
            packets[name] = (packet, packet_text)

        await actor(conn, OWNER_B)
        packet, packet_text = packets["name"]
        try:
            await conn.fetchrow(
                "SELECT * FROM memory.preflight_projection_packet_v5_2($1,$2)",
                uuid.UUID("b3000000-0000-4000-8000-000000000001"),
                packet_text,
            )
        except asyncpg.PostgresError:
            pass
        else:
            raise RuntimeError("cross-owner V5.2 projection preflight passed")
        await actor(conn, OWNER_A)
        counts = await conn.fetchrow(
            """
            SELECT
              (SELECT count(*) FROM memory.projection_plan
               WHERE owner_user_id=$1 AND predicate_registry_version='memory_predicate_registry_v5_2') AS plans,
              (SELECT count(*) FROM memory.projection_plan_item
               WHERE owner_user_id=$1 AND predicate_registry_version='memory_predicate_registry_v5_2') AS items,
              (SELECT count(*) FROM memory.projection_claim_payload
               WHERE owner_user_id=$1 AND claim_class='reported_stance') AS stances,
              (SELECT count(*) FROM memory.observation_entailment_v5
               WHERE owner_user_id=$1 AND decision='accepted') AS entailed,
              (SELECT count(*) FROM memory.claim WHERE owner_user_id=$1) AS claims
            """,
            OWNER_A,
        )
        if dict(counts) != {
            "plans": 2,
            "items": 2,
            "stances": 1,
            "entailed": 2,
            "claims": 0,
        }:
            raise RuntimeError(f"unexpected V5.2 projection counts: {dict(counts)}")
    finally:
        await conn.close()
    print("memory_v1_v5_2_projection_dispatch_clone: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
