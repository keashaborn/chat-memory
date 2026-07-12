from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any, Dict

from .memory_v1_store import ProposalValidationError, proposal_hash


APPROVED_SEED_CARD_IDS = (104, 105, 106, 107)


def _json_object(value: Any, field: str) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    raise ProposalValidationError(f"{field} must be a JSON object")


def _score(value: Any, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, Decimal):
        value = float(value)
    parsed = float(value)
    if not 0 <= parsed <= 1:
        raise ProposalValidationError("legacy score is outside 0..1")
    return parsed


def _owner_from_card(card: Dict[str, Any], payload: Dict[str, Any]) -> uuid.UUID:
    topic_key = str(card.get("topic_key") or "")
    parts = topic_key.split("/")
    if len(parts) < 3 or parts[0] != "user":
        raise ProposalValidationError("legacy topic_key is not user-owned")
    topic_owner = uuid.UUID(parts[1])
    payload_owner = uuid.UUID(str(payload.get("user_id")))
    if topic_owner != payload_owner:
        raise ProposalValidationError("legacy topic_key and payload user_id differ")
    return topic_owner


def _source_ids(payload: Dict[str, Any]) -> list[uuid.UUID]:
    raw = payload.get("source_point_ids") or []
    if payload.get("source_point_id"):
        raw = [*raw, payload["source_point_id"]]
    out: list[uuid.UUID] = []
    for value in raw:
        parsed = uuid.UUID(str(value))
        if parsed not in out:
            out.append(parsed)
    if not out:
        raise ProposalValidationError("legacy card has no source point ID")
    return out


def _base_proposal(card: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "subject": {
            "entity_key": "self",
            "entity_type": "person",
            "canonical_name": "The user",
        },
        "canonical_text": str(card.get("summary") or "").strip(),
        "status": "supported",
        "confidence": _score(card.get("confidence"), 0.5),
        "importance": _score(card.get("strength"), 0.5),
        "salience": _score(card.get("strength"), 0.5),
        "sensitivity": str(payload.get("sensitivity") or "medium").lower(),
        "evidence_stance": "supports",
        "evidence_relevance": 1.0,
        "support_score": _score(card.get("confidence"), 0.5),
        "opposition_score": 0.0,
        "assessment_method": "legacy_manual_review",
        "assessment_method_version": "v1",
        "metadata": {
            "legacy_card_id": int(card["card_id"]),
            "legacy_topic_key": str(card["topic_key"]),
            "legacy_review_status": str(payload.get("review_status") or ""),
        },
    }


def build_seed_mapping(card: Dict[str, Any]) -> Dict[str, Any]:
    payload = _json_object(card.get("payload"), "legacy payload")
    if str(card.get("status") or "") != "active":
        raise ProposalValidationError("legacy seed card is not active")
    if str(payload.get("review_status") or "") != "approved":
        raise ProposalValidationError("legacy seed card is not approved")

    owner = _owner_from_card(card, payload)
    proposal = _base_proposal(card, payload)
    kind = str(card.get("kind") or "")

    if kind == "personal_event":
        event_type = str(payload.get("event_type") or "")
        if event_type not in {"death_loss", "pet_death_loss"}:
            raise ProposalValidationError(f"unsupported seed personal event: {event_type}")
        subject = _json_object(payload.get("subject"), "legacy subject")
        domain = "family_death" if event_type == "death_loss" else "pet_loss"
        proposal.update(
            {
                "predicate": "personal_event.occurred",
                "object_literal": {
                    "event_type": event_type,
                    "event": payload.get("event"),
                    "subject": subject,
                },
                "qualifiers": {
                    "domains": payload.get("domains") or [],
                    "relation_to_user": subject.get("relation_to_user"),
                },
                "retrieval_policy": {
                    "domains": [domain],
                    "intents": ["personal_recall", "relevant_support"],
                    "surface": "direct",
                },
            }
        )
    elif kind == "life_context":
        subject = _json_object(payload.get("subject"), "legacy subject")
        proposal.update(
            {
                "predicate": "life_context.active",
                "object_literal": {
                    "event_type": payload.get("event_type"),
                    "event": payload.get("event"),
                    "subject": subject,
                },
                "qualifiers": {
                    "domains": payload.get("domains") or [],
                    "relation_to_user": subject.get("relation_to_user"),
                },
                "retrieval_policy": {
                    "domains": ["life_context"],
                    "intents": ["relevant_support", "personal_recall"],
                    "surface": "support",
                },
            }
        )
    elif kind == "correction":
        if str(payload.get("correction_type") or "") != "name_alias_correction":
            raise ProposalValidationError("unsupported seed correction type")
        proposal.update(
            {
                "predicate": "name.canonical",
                "object_literal": {
                    "entity_type": payload.get("entity_type") or "pet",
                    "canonical_value": payload.get("canonical_value"),
                    "incorrect_values": [payload.get("incorrect_value")],
                },
                "qualifiers": {
                    "entity_role": payload.get("entity_type") or "pet",
                    "correction_type": payload.get("correction_type"),
                },
                "retrieval_policy": {
                    "domains": ["name_correction"],
                    "intents": ["personal_recall"],
                    "surface": "normalization",
                },
            }
        )
    else:
        raise ProposalValidationError(f"unsupported seed card kind: {kind}")

    return {
        "card_id": int(card["card_id"]),
        "owner_user_id": str(owner),
        "source_ids": [str(value) for value in _source_ids(payload)],
        "proposal": proposal,
        "proposal_hash": proposal_hash(proposal),
    }
