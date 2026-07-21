#!/usr/bin/env python3
"""Deterministic V5.1 death-event observation-to-claim projection."""

from __future__ import annotations

import json
import re
from typing import Any

from memory_v1_projection_v5_contract_test import (
    IDENTITY_KEYS,
    PACKET_KEYS,
    PROJECTION_KEYS,
    owner_manifest_sha256,
    semantic_key_sha256,
    sha256,
    stable_json,
)


CONTRACT_VERSION = "memory_v1_projection_plan_v5"
POLICY_VERSION = "memory_projection_policy_v5"
PROJECTOR = "memory_v1_deterministic_life_event_claim_projection_v5_1"
PROJECTOR_VERSION = "life_event_template_v1"
REVIEW_REASON = "initial_life_event_claim_projection_requires_review"
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class LifeEventProjectionError(RuntimeError):
    pass


def safe_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise LifeEventProjectionError(f"invalid {label}")
    if len(value) > 500 or CONTROL_RE.search(value):
        raise LifeEventProjectionError(f"unsafe {label}")
    return value


def validate_literal(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "kind", "datatype", "value", "unit", "approximate"
    }:
        raise LifeEventProjectionError("death-event literal fields mismatch")
    if (
        value["kind"] != "literal"
        or value["datatype"] != "boolean"
        or value["value"] is not True
        or value["unit"] is not None
        or value["approximate"] is not False
    ):
        raise LifeEventProjectionError("death-event literal must be exact true")
    return value


def render_canonical_text(row: dict[str, Any]) -> str:
    if row.get("predicate") != "life_event.died":
        raise LifeEventProjectionError("unsupported life-event predicate")
    subject_type = row.get("subject_entity_type")
    if subject_type not in {"person", "animal"}:
        raise LifeEventProjectionError("death-event subject type is invalid")
    name = safe_text(row.get("subject_canonical_name"), "subject name")
    validate_literal(row.get("object_literal"))
    if subject_type == "person" and name.lower() in {"mother", "father"}:
        return f"The user's {name.lower()} died."
    return f"{name} died."


def validate_source(row: dict[str, Any]) -> None:
    expected = {
        "predicate": "life_event.died",
        "polarity": "affirmed",
        "evidence_status": "active",
        "subject_entity_status": "active",
        "object_kind": "literal",
        "temporal_semantic": "occurrence",
    }
    for field, value in expected.items():
        if str(row.get(field)) != value:
            raise LifeEventProjectionError(f"{field} drifted")
    if row.get("predicate_registry_version") not in {
        "memory_predicate_registry_v5", "memory_predicate_registry_v5_1"
    }:
        raise LifeEventProjectionError("predicate registry version drifted")
    if row.get("modality") not in {"asserted", "reported_observation"}:
        raise LifeEventProjectionError("death-event modality is invalid")
    if row.get("projection_class") not in {"direct_claim", "supportive_context"}:
        raise LifeEventProjectionError("death-event projection class is invalid")
    if row.get("surface_policy") not in {
        "direct_or_relevant", "mention_when_directly_relevant"
    }:
        raise LifeEventProjectionError("death-event surface policy is invalid")
    if row.get("object_entity_id") is not None:
        raise LifeEventProjectionError("death-event literal has an object entity")
    literal = validate_literal(row.get("object_literal"))
    if row.get("object_literal_sha256") != sha256(literal):
        raise LifeEventProjectionError("death-event literal hash mismatch")
    if row.get("canonical_text") != render_canonical_text(row):
        raise LifeEventProjectionError("database and local death renderers disagree")


def build_projection(owner: str, row: dict[str, Any]) -> dict[str, Any]:
    validate_source(row)
    identity = {
        "subject_entity_id": str(row["subject_entity_id"]),
        "predicate": "life_event.died",
        "object_kind": "literal",
        "object_entity_id": None,
        "object_literal_sha256": row["object_literal_sha256"],
        "polarity": "affirmed",
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
        "observation_inputs": [{
            "observation_id": str(row["observation_id"]),
            "observation_sha256": row["observation_sha256"],
            "stance": "supports",
        }],
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
            "reason_codes": [REVIEW_REASON],
        },
        "relations": [],
        "payload": payload,
    }
    identity["semantic_key_sha256"] = semantic_key_sha256(
        owner, "claim", identity, payload
    )
    return projection


def build_packet(projection: dict[str, Any], registry_version: str) -> dict[str, Any]:
    if registry_version not in {
        "memory_predicate_registry_v5", "memory_predicate_registry_v5_1"
    }:
        raise LifeEventProjectionError("packet registry version is invalid")
    packet = {
        "contract_version": CONTRACT_VERSION,
        "predicate_registry_version": registry_version,
        "projection_policy_version": POLICY_VERSION,
        "projector": PROJECTOR,
        "projector_version": PROJECTOR_VERSION,
        "projections": [projection],
        "packet_sha256": "",
    }
    packet["packet_sha256"] = sha256(
        {key: value for key, value in packet.items() if key != "packet_sha256"}
    )
    return packet


def validate_packet(packet: dict[str, Any], owner: str) -> None:
    if set(packet) != PACKET_KEYS:
        raise LifeEventProjectionError("death-event packet fields mismatch")
    if (
        packet.get("contract_version") != CONTRACT_VERSION
        or packet.get("predicate_registry_version") not in {
            "memory_predicate_registry_v5", "memory_predicate_registry_v5_1"
        }
        or packet.get("projection_policy_version") != POLICY_VERSION
        or packet.get("projector") != PROJECTOR
        or packet.get("projector_version") != PROJECTOR_VERSION
        or not isinstance(packet.get("projections"), list)
        or len(packet["projections"]) != 1
    ):
        raise LifeEventProjectionError("death-event packet boundary mismatch")
    if packet["packet_sha256"] != sha256(
        {key: value for key, value in packet.items() if key != "packet_sha256"}
    ):
        raise LifeEventProjectionError("death-event packet hash mismatch")
    projection = packet["projections"][0]
    if set(projection) != PROJECTION_KEYS or projection.get("projection_ref") != "p01":
        raise LifeEventProjectionError("death-event projection fields mismatch")
    identity = projection["identity"]
    payload = projection["payload"]
    if set(identity) != IDENTITY_KEYS:
        raise LifeEventProjectionError("death-event identity fields mismatch")
    if (
        identity.get("predicate") != "life_event.died"
        or identity.get("object_kind") != "literal"
        or identity.get("object_entity_id") is not None
        or not isinstance(identity.get("object_literal_sha256"), str)
    ):
        raise LifeEventProjectionError("death-event identity mismatch")
    if semantic_key_sha256(owner, "claim", identity, payload) != identity[
        "semantic_key_sha256"
    ]:
        raise LifeEventProjectionError("death-event semantic key mismatch")
    if projection["review"] != {
        "state": "manual_review_required",
        "authorization_required": True,
        "reason_codes": [REVIEW_REASON],
    }:
        raise LifeEventProjectionError("death-event review gate was weakened")


async def load_source(conn: Any, observation_id: Any) -> dict[str, Any]:
    row = await conn.fetchrow(
        "SELECT * FROM memory.preflight_claim_projection_source_v5_1($1)",
        observation_id,
    )
    if row is None:
        raise LifeEventProjectionError("complete death-event source not found")
    result = dict(row)
    for field in ("object_literal", "source_spans"):
        if isinstance(result.get(field), str):
            result[field] = json.loads(result[field])
    return result


def owner_manifest(owner: str, packet: dict[str, Any]) -> str:
    return owner_manifest_sha256(owner, packet["packet_sha256"])


__all__ = [
    "LifeEventProjectionError",
    "build_packet",
    "build_projection",
    "load_source",
    "owner_manifest",
    "render_canonical_text",
    "stable_json",
    "validate_packet",
    "validate_source",
]
