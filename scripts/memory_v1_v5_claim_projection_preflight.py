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
CONTRACT = "memory_v1_claim_projection_stage_bundle_v5_1"
PROJECTOR = "memory_v1_deterministic_claim_projection_v5_1"
PROJECTOR_VERSION = "claim_template_v1"
SUPPORTED_PREDICATES = {
    "identity.name",
    "pet.breed",
    "pet.sex",
    "relationship.has_pet",
}
CONTROL_CHARS = {chr(value) for value in range(32)} | {chr(127)}


class ClaimProjectionError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True)
    parser.add_argument("--observation-id", required=True)
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def load_contract() -> dict[str, dict[str, Any]]:
    schema = json.loads(
        (ROOT / "specs/memory_v1_projection_plan_v5.schema.json").read_text()
    )
    registry = json.loads(
        (ROOT / "specs/memory_v1_predicate_registry_v5.json").read_text()
    )
    validate_projection_schema(schema)
    return validate_registry(registry)


def safe_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ClaimProjectionError(f"invalid {label}")
    if len(value) > 500 or any(character in CONTROL_CHARS for character in value):
        raise ClaimProjectionError(f"unsafe {label}")
    return value


def literal_value(predicate: str, literal: Any) -> str:
    expected = {
        "identity.name": "text",
        "pet.breed": "text",
        "pet.sex": "enum",
    }.get(predicate)
    if expected is None or not isinstance(literal, dict):
        raise ClaimProjectionError("literal is outside the supported contract")
    if set(literal) != {"kind", "datatype", "value", "unit", "approximate"}:
        raise ClaimProjectionError("literal fields mismatch")
    if (
        literal["kind"] != "literal"
        or literal["datatype"] != expected
        or literal["unit"] is not None
        or literal["approximate"] is not False
    ):
        raise ClaimProjectionError("literal contract mismatch")
    return safe_text(literal["value"], "literal value")


def render_canonical_text(row: dict[str, Any]) -> str:
    predicate = row["predicate"]
    subject_type = row["subject_entity_type"]
    subject_name = safe_text(row["subject_canonical_name"], "subject name")
    object_kind = row["object_kind"]
    if predicate == "relationship.has_pet":
        object_name = safe_text(row["object_canonical_name"], "object name")
        if (
            object_kind != "entity"
            or subject_type not in {"self", "person"}
            or row["object_entity_type"] != "animal"
            or row["object_literal"] is not None
        ):
            raise ClaimProjectionError("pet relationship rendering mismatch")
        if subject_type == "self":
            return f"The user has a pet named {object_name}."
        return f"{subject_name} has a pet named {object_name}."
    if object_kind != "literal" or row["object_entity_id"] is not None:
        raise ClaimProjectionError("literal identity mismatch")
    value = literal_value(predicate, row["object_literal"])
    if predicate == "identity.name":
        labels = {
            "self": "The user's name",
            "person": "This person's name",
            "animal": "This animal's name",
            "organization": "This organization's name",
            "place": "This place's name",
            "project": "This project's name",
            "object": "This object's name",
            "concept": "This concept's name",
        }
        if subject_type not in labels:
            raise ClaimProjectionError("name subject type mismatch")
        return f"{labels[subject_type]} is {value}."
    if predicate == "pet.breed" and subject_type == "animal":
        return f"{subject_name} has recorded breed {value}."
    if predicate == "pet.sex" and subject_type == "animal":
        return f"{subject_name} has recorded sex {value}."
    raise ClaimProjectionError("predicate has no deterministic renderer")


def validate_source(row: dict[str, Any]) -> None:
    if row["predicate"] not in SUPPORTED_PREDICATES:
        raise ClaimProjectionError("unsupported deterministic predicate")
    exact = {
        "predicate_registry_version": "memory_predicate_registry_v5",
        "polarity": "affirmed",
        "modality": "asserted",
        "evidence_status": "active",
        "subject_entity_status": "active",
    }
    for field, expected in exact.items():
        if str(row[field]) != expected:
            raise ClaimProjectionError(f"{field} drifted")
    if row["projection_class"] not in {"direct_claim", "supportive_context"}:
        raise ClaimProjectionError("unsupported projection class")
    if row["object_kind"] == "entity":
        if row["object_entity_id"] is None or row["object_entity_status"] != "active":
            raise ClaimProjectionError("inactive or absent object entity")
        expected_literal_hash = None
    elif row["object_kind"] == "literal":
        if row["object_entity_id"] is not None:
            raise ClaimProjectionError("literal has an object entity")
        expected_literal_hash = sha256(row["object_literal"])
    else:
        raise ClaimProjectionError("unsupported object kind")
    if row["object_literal_sha256"] != expected_literal_hash:
        raise ClaimProjectionError("object literal hash mismatch")
    if row["canonical_text"] != render_canonical_text(row):
        raise ClaimProjectionError("database and local renderers disagree")


def build_projection(owner: str, row: dict[str, Any]) -> dict[str, Any]:
    validate_source(row)
    identity = {
        "subject_entity_id": str(row["subject_entity_id"]),
        "predicate": row["predicate"],
        "object_kind": row["object_kind"],
        "object_entity_id": (
            str(row["object_entity_id"])
            if row["object_entity_id"] is not None
            else None
        ),
        "object_literal_sha256": row["object_literal_sha256"],
        "polarity": row["polarity"],
        "modality": row["modality"],
        "semantic_key_sha256": "",
    }
    payload = {
        "kind": "claim",
        "claim_class": row["projection_class"],
        "canonical_text": render_canonical_text(row),
        "surface_policy": row["surface_policy"],
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
            "reason_codes": ["initial_claim_projection_requires_review"],
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


async def load_source(conn: Any, observation_id: uuid.UUID) -> dict[str, Any]:
    row = await conn.fetchrow(
        "SELECT * FROM memory.preflight_claim_projection_source_v5_1($1)",
        observation_id,
    )
    if row is None:
        raise ClaimProjectionError("complete owner-scoped observation not found")
    result = dict(row)
    for field in ("object_literal", "source_spans"):
        if isinstance(result.get(field), str):
            result[field] = json.loads(result[field])
    return result


async def assert_database_hashes(
    conn: Any,
    owner: uuid.UUID,
    plan_id: uuid.UUID,
    projection: dict[str, Any],
    packet: dict[str, Any],
) -> dict[str, str]:
    packet_text = stable_json(packet)
    expected = {
        "packet_text_sha256": hashlib.sha256(packet_text.encode()).hexdigest(),
        "semantic_key_sha256": projection["identity"]["semantic_key_sha256"],
        "projection_sha256": sha256(projection),
        "packet_sha256": packet["packet_sha256"],
        "owner_manifest_sha256": owner_manifest_sha256(
            str(owner), packet["packet_sha256"]
        ),
    }
    database = await conn.fetchrow(
        "SELECT * FROM memory.preflight_claim_projection_packet_v5_1($1,$2)",
        plan_id,
        packet_text,
    )
    for key, value in expected.items():
        if database[key] != value:
            raise ClaimProjectionError(f"database {key} mismatch")
    if database["existing_claims"] != 0 or database["existing_plans"] != 0:
        raise ClaimProjectionError("projection target or plan already exists")
    return {"packet_text": packet_text, **expected}


async def async_main() -> int:
    import asyncpg

    args = arguments()
    owner = uuid.UUID(args.owner)
    observation_id = uuid.UUID(args.observation_id)
    plan_id = uuid.UUID(args.plan_id)
    output = Path(args.output).resolve()
    registry = load_contract()
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ClaimProjectionError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
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
            "object_entity_id": (
                str(source["object_entity_id"])
                if source["object_entity_id"] is not None
                else None
            ),
            "object_literal_sha256": source["object_literal_sha256"],
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
