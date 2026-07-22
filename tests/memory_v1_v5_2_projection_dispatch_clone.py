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
PROJECTION_REQUEST_IDS = {
    "name": uuid.UUID("a5000000-0000-4000-8000-000000000001"),
    "stance": uuid.UUID("a5000000-0000-4000-8000-000000000002"),
}
ASSESSMENT_REVIEW_REQUEST_IDS = {
    "name": uuid.UUID("a6000000-0000-4000-8000-000000000001"),
    "stance": uuid.UUID("a6000000-0000-4000-8000-000000000002"),
}
ASSESSMENT_APPLY_REQUEST_IDS = {
    "name": uuid.UUID("a7000000-0000-4000-8000-000000000001"),
    "stance": uuid.UUID("a7000000-0000-4000-8000-000000000002"),
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


async def review_apply_and_assess_claim(
    conn: asyncpg.Connection,
    name: str,
) -> dict[str, object]:
    reason_codes = stable_json(
        [
            "synthetic_clone_fixture",
            "accepted_predicate_entailment",
            "reviewed_v5_2_projection",
        ]
    )
    review_preflight = await conn.fetchrow(
        """
        SELECT * FROM memory.preflight_projection_review_v5(
          $1,'p01','authorized'::memory.projection_review_decision_v5,
          'system','synthetic_v5_2_projection_clone',
          'reviewed synthetic V5.2 projection',$2::jsonb
        )
        """,
        PLAN_IDS[name],
        reason_codes,
    )
    reviewed = await conn.fetchrow(
        """
        SELECT * FROM memory.review_projection_v5(
          $1,'p01','authorized'::memory.projection_review_decision_v5,
          'system','synthetic_v5_2_projection_clone',
          'reviewed synthetic V5.2 projection',$2::jsonb,$3
        )
        """,
        PLAN_IDS[name],
        reason_codes,
        review_preflight["authorization_manifest_sha256"],
    )
    if reviewed["outcome"] != "applied" or reviewed["rows_written"] != 1:
        raise RuntimeError("V5.2 projection review did not write exactly one row")

    apply_preflight = await conn.fetchrow(
        "SELECT * FROM memory.preflight_projection_apply_v5($1,'p01',$2)",
        PLAN_IDS[name],
        reviewed["review_id"],
    )
    applied = await conn.fetchrow(
        "SELECT * FROM memory.apply_projection_v5($1,$2,'p01',$3,$4)",
        PROJECTION_REQUEST_IDS[name],
        PLAN_IDS[name],
        reviewed["review_id"],
        apply_preflight["apply_manifest_sha256"],
    )
    if (
        applied["outcome"] != "applied"
        or str(applied["lane"]) != "claim"
        or applied["revision_number"] != 1
        or applied["rows_written"] != 5
    ):
        raise RuntimeError("V5.2 claim projection materialization drifted")

    assessment_preflight = await conn.fetchrow(
        """
        SELECT * FROM memory.preflight_claim_assessment_review_v5(
          $1,'promote_supported'::memory.claim_assessment_action_v5,
          1::numeric,0::numeric,1::numeric,1::numeric,$2::jsonb,
          'accepted synthetic evidence and entailment','system',
          'synthetic_v5_2_projection_clone'
        )
        """,
        applied["aggregate_id"],
        reason_codes,
    )
    assessment_review = await conn.fetchrow(
        """
        SELECT * FROM memory.review_claim_assessment_v5(
          $1,$2,'promote_supported'::memory.claim_assessment_action_v5,
          1::numeric,0::numeric,1::numeric,1::numeric,$3::jsonb,
          'accepted synthetic evidence and entailment','system',
          'synthetic_v5_2_projection_clone',$4
        )
        """,
        ASSESSMENT_REVIEW_REQUEST_IDS[name],
        applied["aggregate_id"],
        reason_codes,
        assessment_preflight["authorization_manifest_sha256"],
    )
    assessment_apply_preflight = await conn.fetchrow(
        "SELECT * FROM memory.preflight_claim_assessment_apply_v5($1,$2)",
        applied["aggregate_id"],
        assessment_review["review_id"],
    )
    assessed = await conn.fetchrow(
        "SELECT * FROM memory.apply_claim_assessment_v5($1,$2,$3,$4)",
        ASSESSMENT_APPLY_REQUEST_IDS[name],
        applied["aggregate_id"],
        assessment_review["review_id"],
        assessment_apply_preflight["apply_manifest_sha256"],
    )
    if (
        assessment_review["outcome"] != "applied"
        or assessed["outcome"] != "applied"
        or assessed["resulting_revision_number"] != 2
        or assessed["rows_written"] != 5
    ):
        raise RuntimeError("V5.2 claim assessment did not promote to supported")

    review_replay = await conn.fetchrow(
        """
        SELECT * FROM memory.review_projection_v5(
          $1,'p01','authorized'::memory.projection_review_decision_v5,
          'system','synthetic_v5_2_projection_clone',
          'reviewed synthetic V5.2 projection',$2::jsonb,$3
        )
        """,
        PLAN_IDS[name],
        reason_codes,
        review_preflight["authorization_manifest_sha256"],
    )
    apply_replay = await conn.fetchrow(
        "SELECT * FROM memory.apply_projection_v5($1,$2,'p01',$3,$4)",
        PROJECTION_REQUEST_IDS[name],
        PLAN_IDS[name],
        reviewed["review_id"],
        apply_preflight["apply_manifest_sha256"],
    )
    assessment_apply_replay = await conn.fetchrow(
        "SELECT * FROM memory.apply_claim_assessment_v5($1,$2,$3,$4)",
        ASSESSMENT_APPLY_REQUEST_IDS[name],
        applied["aggregate_id"],
        assessment_review["review_id"],
        assessment_apply_preflight["apply_manifest_sha256"],
    )
    for replay in (
        review_replay,
        apply_replay,
        assessment_apply_replay,
    ):
        if replay["outcome"] != "replayed" or replay["rows_written"] != 0:
            raise RuntimeError("V5.2 controlled projection replay wrote rows")
    return {
        "claim_id": applied["aggregate_id"],
        "review_id": reviewed["review_id"],
        "apply_event_id": applied["apply_event_id"],
    }


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
        claims: dict[str, dict[str, object]] = {}
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

        for name in sorted(packets):
            claims[name] = await review_apply_and_assess_claim(conn, name)
        if len({value["claim_id"] for value in claims.values()}) != 2:
            raise RuntimeError("V5.2 controlled projection claim identities collided")

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
    finally:
        await conn.close()
    print("memory_v1_v5_2_projection_dispatch_clone: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
