#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any

from memory_v1_projection_v5_contract_test import (
    owner_manifest_sha256,
    semantic_key_sha256,
    sha256,
    stable_json,
    validate_packet,
    validate_projection_schema,
    validate_registry,
)


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "memory_v1_projection_stage_bundle_v1"
PROJECTOR = "memory_v1_deterministic_projection_v5"
PROJECTOR_VERSION = "occupation_claim_v1"
SUPPORTED_PREDICATE = "occupation.works_as"


class ProjectionPreflightError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True)
    parser.add_argument("--observation-id", required=True)
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def load_contract() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    schema = json.loads(
        (ROOT / "specs/memory_v1_projection_plan_v5.schema.json").read_text()
    )
    registry = json.loads(
        (ROOT / "specs/memory_v1_predicate_registry_v5.json").read_text()
    )
    validate_projection_schema(schema)
    return schema, validate_registry(registry)


def build_projection(owner: str, row: dict[str, Any]) -> dict[str, Any]:
    if row["predicate"] != SUPPORTED_PREDICATE:
        raise ProjectionPreflightError("unsupported deterministic predicate")
    exact = {
        "polarity": "affirmed",
        "modality": "asserted",
        "projection_class": "direct_claim",
        "surface_policy": "direct_or_relevant",
        "evidence_status": "active",
        "subject_entity_type": "self",
        "subject_entity_status": "active",
        "object_entity_type": "concept",
        "object_entity_status": "active",
        "object_canonical_name": "personal trainer",
        "temporal_semantic": "state_validity",
        "temporal_shape": "open_interval",
        "temporal_basis": "instant",
    }
    for field, expected in exact.items():
        if str(row[field]) != expected:
            raise ProjectionPreflightError(
                f"{field} drifted: expected {expected!r}, got {row[field]!r}"
            )
    identity = {
        "subject_entity_id": str(row["subject_entity_id"]),
        "predicate": row["predicate"],
        "object_kind": "entity",
        "object_entity_id": str(row["object_entity_id"]),
        "object_literal_sha256": None,
        "polarity": row["polarity"],
        "modality": row["modality"],
        "semantic_key_sha256": "",
    }
    payload = {
        "kind": "claim",
        "claim_class": "direct_claim",
        "canonical_text": "The user works as a personal trainer.",
        "surface_policy": "direct_or_relevant",
    }
    projection = {
        "projection_ref": "p01",
        "lane": "claim",
        "observation_inputs": [
            {
                "observation_id": str(row["observation_id"]),
                "observation_sha256": row["observation_sha256"],
                "stance": "supports",
            }
        ],
        "identity": identity,
        "target": {
            "action": "create",
            "aggregate_id": None,
            "expected_revision_number": None,
            "reason_codes": [],
        },
        "temporal_policy": {
            "canonical_source": "memory.observation_temporal",
            "materialization": "link_only",
            "source_observation_id": None,
        },
        "review": {
            "state": "manual_review_required",
            "authorization_required": True,
            "reason_codes": ["initial_live_projection_requires_review"],
        },
        "relations": [],
        "payload": payload,
    }
    identity["semantic_key_sha256"] = semantic_key_sha256(
        owner, "claim", identity, payload
    )
    return projection


def build_packet(projection: dict[str, Any]) -> dict[str, Any]:
    packet = {
        "contract_version": "memory_v1_projection_plan_v5",
        "predicate_registry_version": "memory_predicate_registry_v5",
        "projection_policy_version": "memory_projection_policy_v5",
        "projector": PROJECTOR,
        "projector_version": PROJECTOR_VERSION,
        "projections": [projection],
        "packet_sha256": "",
    }
    packet["packet_sha256"] = sha256(
        {key: value for key, value in packet.items() if key != "packet_sha256"}
    )
    return packet


async def load_source(
    conn: Any, observation_id: uuid.UUID
) -> dict[str, Any]:
    row = await conn.fetchrow(
        "SELECT * FROM memory.preflight_projection_source_v5($1)",
        observation_id,
    )
    if row is None:
        raise ProjectionPreflightError("complete owner-scoped observation not found")
    return dict(row)


async def assert_database_hashes(
    conn: Any,
    owner: uuid.UUID,
    plan_id: uuid.UUID,
    projection: dict[str, Any],
    packet: dict[str, Any],
) -> dict[str, str]:
    packet_text = stable_json(packet)
    projection_sha = sha256(projection)
    packet_sha = packet["packet_sha256"]
    owner_manifest = owner_manifest_sha256(str(owner), packet_sha)
    database = await conn.fetchrow(
        "SELECT * FROM memory.preflight_projection_packet_v5($1,$2)",
        plan_id,
        packet_text,
    )
    expected = {
        "packet_text_sha256": hashlib.sha256(packet_text.encode()).hexdigest(),
        "semantic_key_sha256": projection["identity"]["semantic_key_sha256"],
        "projection_sha256": projection_sha,
        "packet_sha256": packet_sha,
        "owner_manifest_sha256": owner_manifest,
    }
    for key, value in expected.items():
        if database[key] != value:
            raise ProjectionPreflightError(f"database {key} mismatch")
    if database["existing_claims"] != 0 or database["existing_plans"] != 0:
        raise ProjectionPreflightError("projection target or plan already exists")
    return {
        "packet_text": packet_text,
        "packet_text_sha256": expected["packet_text_sha256"],
        "projection_sha256": projection_sha,
        "packet_sha256": packet_sha,
        "owner_manifest_sha256": owner_manifest,
    }


async def async_main() -> int:
    import asyncpg

    args = arguments()
    owner = uuid.UUID(args.owner)
    observation_id = uuid.UUID(args.observation_id)
    plan_id = uuid.UUID(args.plan_id)
    output = Path(args.output).resolve()
    _, registry = load_contract()
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ProjectionPreflightError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            await conn.execute(
                "SELECT set_config('app.user_id',$1,true)", str(owner)
            )
            source = await load_source(conn, observation_id)
            projection = build_projection(str(owner), source)
            packet = build_packet(projection)
            validate_packet(packet, str(owner), registry)
            hashes = await assert_database_hashes(
                conn, owner, plan_id, projection, packet
            )
    finally:
        await conn.close()
    bundle: dict[str, Any] = {
        "contract_version": CONTRACT,
        "owner_user_id": str(owner),
        "plan_id": str(plan_id),
        "projection_ref": "p01",
        "packet": packet,
        **hashes,
        "lane_scope": {},
        "source_snapshot": {
            "observation_id": str(source["observation_id"]),
            "observation_sha256": source["observation_sha256"],
            "evidence_id": str(source["evidence_id"]),
            "evidence_content_sha256": source["evidence_content_sha256"],
            "subject_entity_id": str(source["subject_entity_id"]),
            "object_entity_id": str(source["object_entity_id"]),
            "temporal_normalized_sha256": source["temporal_normalized_sha256"],
        },
        "database_writes": 0,
        "external_model_calls": 0,
        "qdrant_writes": 0,
    }
    bundle["bundle_sha256"] = sha256(bundle)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n")
    output.chmod(stat.S_IRUSR | stat.S_IWUSR)
    print(f"bundle={output}")
    print(f"bundle_sha256={bundle['bundle_sha256']}")
    print(f"packet_sha256={bundle['packet_sha256']}")
    print(f"owner_manifest_sha256={bundle['owner_manifest_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
