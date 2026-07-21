#!/usr/bin/env python3
"""Deterministic V5.1 relationship/social observation-to-claim projection."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any
import uuid

from memory_v1_projection_v5_contract_test import (
    IDENTITY_KEYS,
    PACKET_KEYS,
    PROJECTION_KEYS,
    owner_manifest_sha256,
    semantic_key_sha256,
    sha256,
    stable_json,
)


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_VERSION = "memory_predicate_registry_v5_1"
CONTRACT_VERSION = "memory_v1_projection_plan_v5"
POLICY_VERSION = "memory_projection_policy_v5"
PROJECTOR = "memory_v1_deterministic_relationship_claim_projection_v5_1"
PROJECTOR_VERSION = "relationship_template_v1"
REVIEW_REASON = "initial_relationship_claim_projection_requires_review"
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class RelationshipProjectionError(RuntimeError):
    pass


def safe_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise RelationshipProjectionError(f"invalid {label}")
    if len(value) > 500 or CONTROL_RE.search(value):
        raise RelationshipProjectionError(f"unsafe {label}")
    return value


def load_registry() -> dict[str, dict[str, Any]]:
    value = json.loads(
        (ROOT / "specs/memory_v1_predicate_registry_v5_1.json").read_text(
            encoding="utf-8"
        )
    )
    if value.get("registry_version") != REGISTRY_VERSION:
        raise RelationshipProjectionError("V5.1 registry version mismatch")
    entries: dict[str, dict[str, Any]] = {}
    for entry in value.get("predicates", []):
        predicate = entry.get("predicate")
        if not isinstance(predicate, str) or predicate in entries:
            raise RelationshipProjectionError("invalid or duplicate registry predicate")
        policy = entry.get("relationship_policy")
        if (
            isinstance(policy, dict)
            and (predicate.startswith("relationship.") or predicate.startswith("social."))
        ):
            if not entry.get("object_contract", "").startswith("entity."):
                raise RelationshipProjectionError("relationship object must be an entity")
            entries[predicate] = entry
    if len(entries) < 40:
        raise RelationshipProjectionError("V5.1 relationship registry is incomplete")
    return entries


def entity_label(entity_type: str, name: str, *, object_position: bool) -> str:
    safe_text(entity_type, "entity type")
    safe_text(name, "entity name")
    if entity_type == "self":
        return "the user" if object_position else "The user"
    return name


def render_canonical_text(row: dict[str, Any]) -> str:
    predicate = safe_text(row["predicate"], "predicate")
    subject_type = safe_text(row["subject_entity_type"], "subject type")
    object_type = safe_text(row["object_entity_type"], "object type")
    subject_name = safe_text(row["subject_canonical_name"], "subject name")
    object_name = safe_text(row["object_canonical_name"], "object name")
    subject = entity_label(subject_type, subject_name, object_position=False)
    obj = entity_label(object_type, object_name, object_position=True)
    pair_templates = {
        "relationship.acquaintance_of": "{s} and {o} are acquaintances.",
        "relationship.business_partner_of": "{s} and {o} are business partners.",
        "relationship.collaborator_with": "{s} and {o} collaborate.",
        "relationship.cousin_of": "{s} and {o} are cousins.",
        "relationship.coworker_of": "{s} and {o} are coworkers.",
        "relationship.friend_of": "{s} and {o} are friends.",
        "relationship.in_law_of": "{s} and {o} are related by marriage.",
        "relationship.lives_with": "{s} and {o} live together.",
        "relationship.neighbor_of": "{s} and {o} are neighbors.",
        "relationship.relative_of": "{s} and {o} are relatives.",
        "relationship.romantic_partner_of": "{s} and {o} are romantic partners.",
        "relationship.roommate_of": "{s} and {o} are roommates.",
        "relationship.sibling_of": "{s} and {o} are siblings.",
        "relationship.spouse_of": "{s} and {o} are spouses.",
        "relationship.teammate_of": "{s} and {o} are teammates.",
        "relationship.training_partner_of": "{s} and {o} are training partners.",
    }
    directed_templates = {
        "relationship.aunt_or_uncle_of": "{s} is an aunt or uncle of {o}.",
        "relationship.caregiver_for": "{s} is a caregiver for {o}.",
        "relationship.coach_of": "{s} is a coach of {o}.",
        "relationship.grandparent_of": "{s} is a grandparent of {o}.",
        "relationship.guardian_of": "{s} is a guardian of {o}.",
        "relationship.healthcare_provider_for": "{s} is a healthcare provider for {o}.",
        "relationship.manager_of": "{s} is a manager of {o}.",
        "relationship.mentor_of": "{s} is a mentor of {o}.",
        "relationship.plan_helper_for": "{s} helps {o} with planning.",
        "relationship.teacher_of": "{s} is a teacher of {o}.",
        "social.avoids": "{s} avoids {o}.",
        "social.competes_with": "{s} competes with {o}.",
        "social.depends_on": "{s} depends on {o}.",
        "social.distrusts": "{s} distrusts {o}.",
        "social.estranged_from": "{s} is estranged from {o}.",
        "social.experiences_tension_with": "{s} experiences tension with {o}.",
        "social.feels_close_to": "{s} feels close to {o}.",
        "social.feels_unsafe_with": "{s} feels unsafe with {o}.",
        "social.in_conflict_with": "{s} is in conflict with {o}.",
        "social.no_contact_with": "{s} has no contact with {o}.",
        "social.perceives_as_adversary": "{s} perceives {o} as an adversary.",
        "social.supports": "{s} supports {o}.",
        "social.trusts": "{s} trusts {o}.",
    }
    if predicate == "relationship.parent_of":
        result = (
            f"{subject_name} is the user's parent."
            if object_type == "self"
            else f"{subject} is a parent of {obj}."
        )
    elif predicate == "relationship.has_pet":
        if object_type != "animal":
            raise RelationshipProjectionError("pet relationship object is not animal")
        result = f"{subject} has a pet named {object_name}."
    elif predicate in pair_templates:
        result = pair_templates[predicate].format(s=subject, o=obj)
    elif predicate in directed_templates:
        result = directed_templates[predicate].format(s=subject, o=obj)
    else:
        raise RelationshipProjectionError("predicate has no relationship renderer")
    if len(result) > 2000 or CONTROL_RE.search(result):
        raise RelationshipProjectionError("rendered relationship text is unsafe")
    return result


def validate_source(row: dict[str, Any], registry: dict[str, dict[str, Any]]) -> None:
    predicate = row["predicate"]
    entry = registry.get(predicate)
    if entry is None:
        raise RelationshipProjectionError("unregistered relationship predicate")
    expected = {
        "predicate_registry_version": REGISTRY_VERSION,
        "polarity": "affirmed",
        "evidence_status": "active",
        "subject_entity_status": "active",
        "object_entity_status": "active",
    }
    for field, value in expected.items():
        if str(row[field]) != value:
            raise RelationshipProjectionError(f"{field} drifted")
    if row["modality"] not in {"asserted", "reported_observation"}:
        raise RelationshipProjectionError("relationship modality is not projectable")
    if row["modality"] not in entry["modalities"]:
        raise RelationshipProjectionError("relationship modality is not registered")
    if row["projection_class"] not in {"direct_claim", "supportive_context"}:
        raise RelationshipProjectionError("relationship projection class is invalid")
    if row["projection_class"] not in entry["projection_classes"]:
        raise RelationshipProjectionError("relationship projection class is unregistered")
    if row["surface_policy"] not in entry["surface_policies"]:
        raise RelationshipProjectionError("relationship surface policy is unregistered")
    if row["object_entity_id"] is None:
        raise RelationshipProjectionError("relationship object entity is absent")
    policy = entry["relationship_policy"]
    if row["subject_entity_type"] not in policy["subject_entity_types"]:
        raise RelationshipProjectionError("relationship subject type is invalid")
    if row["object_entity_type"] not in policy["object_entity_types"]:
        raise RelationshipProjectionError("relationship object type is invalid")
    if row["canonical_text"] != render_canonical_text(row):
        raise RelationshipProjectionError("database and local relationship renderers disagree")


def build_projection(owner: str, row: dict[str, Any]) -> dict[str, Any]:
    registry = load_registry()
    validate_source(row, registry)
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


def build_packet(projection: dict[str, Any]) -> dict[str, Any]:
    packet = {
        "contract_version": CONTRACT_VERSION,
        "predicate_registry_version": REGISTRY_VERSION,
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
    uuid.UUID(owner)
    if set(packet) != PACKET_KEYS:
        raise RelationshipProjectionError("relationship packet fields mismatch")
    if (
        packet["contract_version"] != CONTRACT_VERSION
        or packet["predicate_registry_version"] != REGISTRY_VERSION
        or packet["projection_policy_version"] != POLICY_VERSION
        or packet["projector"] != PROJECTOR
        or packet["projector_version"] != PROJECTOR_VERSION
        or not isinstance(packet["projections"], list)
        or len(packet["projections"]) != 1
    ):
        raise RelationshipProjectionError("relationship packet boundary mismatch")
    if packet["packet_sha256"] != sha256(
        {key: value for key, value in packet.items() if key != "packet_sha256"}
    ):
        raise RelationshipProjectionError("relationship packet hash mismatch")
    projection = packet["projections"][0]
    if set(projection) != PROJECTION_KEYS or projection["projection_ref"] != "p01":
        raise RelationshipProjectionError("relationship projection fields mismatch")
    identity = projection["identity"]
    payload = projection["payload"]
    if set(identity) != IDENTITY_KEYS or identity["object_kind"] != "entity":
        raise RelationshipProjectionError("relationship identity mismatch")
    if identity["object_entity_id"] is None or identity["object_literal_sha256"] is not None:
        raise RelationshipProjectionError("relationship object identity mismatch")
    if semantic_key_sha256(owner, "claim", identity, payload) != identity["semantic_key_sha256"]:
        raise RelationshipProjectionError("relationship semantic key mismatch")
    entry = load_registry().get(identity["predicate"])
    if entry is None or payload["claim_class"] not in entry["projection_classes"]:
        raise RelationshipProjectionError("relationship payload is unregistered")
    if payload["surface_policy"] not in entry["surface_policies"]:
        raise RelationshipProjectionError("relationship surface policy is unregistered")
    expected_review = {
        "state": "manual_review_required",
        "authorization_required": True,
        "reason_codes": [REVIEW_REASON],
    }
    if projection["review"] != expected_review:
        raise RelationshipProjectionError("relationship review gate was weakened")


async def load_source(conn: Any, observation_id: uuid.UUID) -> dict[str, Any]:
    row = await conn.fetchrow(
        "SELECT * FROM memory.preflight_relationship_claim_source_v5_1($1)",
        observation_id,
    )
    if row is None:
        raise RelationshipProjectionError("complete relationship source not found")
    return dict(row)


def owner_manifest(owner: str, packet: dict[str, Any]) -> str:
    return owner_manifest_sha256(owner, packet["packet_sha256"])


__all__ = [
    "RelationshipProjectionError",
    "build_packet",
    "build_projection",
    "load_registry",
    "load_source",
    "owner_manifest",
    "render_canonical_text",
    "stable_json",
    "validate_packet",
    "validate_source",
]
