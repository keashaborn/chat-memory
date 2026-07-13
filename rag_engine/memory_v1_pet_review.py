from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Mapping

from .memory_v1_store import ProposalValidationError, normalize_proposal


OWNER_USER_ID = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
SOURCE_CHAT_LOG_ID = uuid.UUID("1f1ded30-4855-4506-9b7d-58bf3dfc529a")
SOURCE_SHA256 = "6348cb28af11a403dfd547893bbc0598c58826119d4c34b317aed663a20c24c4"
SOURCE_OBSERVED_AT = datetime(2026, 7, 2, 2, 33, 52, 950917, tzinfo=timezone.utc)
NEKO_CORRECTION_CLAIM_ID = uuid.UUID("3c50247c-e600-46f1-9ba3-afd2ec3f4dfe")
HELSING_LOSS_CLAIM_ID = uuid.UUID("8ff5da3c-58f8-4e13-bf46-a57267249162")


def json_object(value: Any, field: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    raise ProposalValidationError(f"{field} must be a JSON object")


def validate_source(row: Mapping[str, Any]) -> None:
    if uuid.UUID(str(row["id"])) != SOURCE_CHAT_LOG_ID:
        raise ProposalValidationError("unexpected pet-loss source ID")
    if uuid.UUID(str(row["user_id"])) != OWNER_USER_ID:
        raise ProposalValidationError("pet-loss source owner mismatch")
    if str(row["source"]) != "frontend/chat:user":
        raise ProposalValidationError("pet-loss source is not user-authored chat")
    observed_at = row["created_at"]
    if observed_at != SOURCE_OBSERVED_AT:
        raise ProposalValidationError("pet-loss source timestamp changed")
    content = str(row["text"])
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if digest != SOURCE_SHA256:
        raise ProposalValidationError("pet-loss source content hash changed")
    required_phrases = (
        "put her to sleep about two years ago",
        "female German shepherd was Dahlia",
        "male Helsing",
        "rare blood cancer",
    )
    lowered = content.casefold()
    if not all(phrase.casefold() in lowered for phrase in required_phrases):
        raise ProposalValidationError("pet-loss source no longer contains reviewed facts")


def _base_event_proposal() -> Dict[str, Any]:
    return {
        "subject": {
            "entity_key": "self",
            "entity_type": "person",
            "canonical_name": "The user",
        },
        "predicate": "personal_event.occurred",
        "status": "supported",
        "sensitivity": "medium",
        "retrieval_policy": {
            "domains": ["pet_loss"],
            "intents": ["personal_recall", "relevant_support"],
            "surface": "direct",
        },
        "evidence_stance": "supports",
        "evidence_relevance": 1.0,
        "support_score": 0.92,
        "opposition_score": 0.0,
        "assessment_method": "reviewed_source_split",
        "assessment_method_version": "v1",
    }


def neko_loss_proposal() -> Dict[str, Any]:
    proposal = _base_event_proposal()
    proposal.update(
        {
            "object_literal": {
                "event_type": "pet_death_loss",
                "event": "euthanized_or_put_to_sleep",
                "subject": {
                    "known_name": "Neko",
                    "relation_to_user": "pet",
                    "species": "cat",
                    "sex": "female",
                },
            },
            "canonical_text": (
                "User's cat Neko was put to sleep about two years before "
                "source date 2026-07-02T02:33:52.950917Z."
            ),
            "qualifiers": {
                "domains": ["pets", "grief", "life_event"],
                "relation_to_user": "pet",
                "species": "cat",
                "sex": "female",
                "temporal_reference": {
                    "kind": "relative_to_source",
                    "value": "about_two_years_before",
                    "source_date": "2026-07-02T02:33:52.950917Z",
                },
                "name_normalization": {
                    "source_value": "Nemo",
                    "canonical_value": "Neko",
                    "correction_claim_id": str(NEKO_CORRECTION_CLAIM_ID),
                },
            },
            "confidence": 0.94,
            "importance": 0.82,
            "salience": 0.80,
            "metadata": {
                "review": "split_from_multi_pet_source_v1",
                "source_chat_log_id": str(SOURCE_CHAT_LOG_ID),
                "normalized_by_claim_id": str(NEKO_CORRECTION_CLAIM_ID),
            },
        }
    )
    return normalize_proposal(proposal)


def dahlia_loss_proposal() -> Dict[str, Any]:
    proposal = _base_event_proposal()
    proposal.update(
        {
            "object_literal": {
                "event_type": "pet_death_loss",
                "event": "euthanized_or_put_to_sleep",
                "subject": {
                    "known_name": "Dahlia",
                    "relation_to_user": "pet",
                    "species": "dog",
                    "breed": "German shepherd",
                    "sex": "female",
                    "age_at_event_years": 12,
                },
            },
            "canonical_text": (
                "User's female German shepherd Dahlia was put to sleep at age 12 "
                "during the year before source date 2026-07-02T02:33:52.950917Z."
            ),
            "qualifiers": {
                "domains": ["pets", "grief", "life_event"],
                "relation_to_user": "pet",
                "species": "dog",
                "breed": "German shepherd",
                "sex": "female",
                "age_at_event_years": 12,
                "temporal_reference": {
                    "kind": "relative_to_source",
                    "value": "previous_year",
                    "source_date": "2026-07-02T02:33:52.950917Z",
                },
            },
            "confidence": 0.92,
            "importance": 0.82,
            "salience": 0.80,
            "metadata": {
                "review": "split_from_multi_pet_source_v1",
                "source_chat_log_id": str(SOURCE_CHAT_LOG_ID),
            },
        }
    )
    return normalize_proposal(proposal)


def existing_claim_proposal(row: Mapping[str, Any]) -> Dict[str, Any]:
    claim_id = uuid.UUID(str(row["claim_id"]))
    if claim_id != HELSING_LOSS_CLAIM_ID:
        raise ProposalValidationError("unexpected existing pet-loss claim")
    if str(row["entity_key"]) != "self" or str(row["predicate"]) != "personal_event.occurred":
        raise ProposalValidationError("existing Helsing claim identity changed")
    proposal: Dict[str, Any] = {
        "subject": {
            "entity_key": str(row["entity_key"]),
            "entity_type": str(row["entity_type"]),
            "canonical_name": str(row["canonical_name"]),
        },
        "predicate": str(row["predicate"]),
        "object_literal": json_object(row["object_literal"], "object_literal"),
        "canonical_text": str(row["canonical_text"]),
        "qualifiers": json_object(row["qualifiers"], "qualifiers"),
        "status": str(row["status"]),
        "confidence": float(row["confidence"]),
        "importance": float(row["importance"]),
        "salience": float(row["salience"]),
        "sensitivity": str(row["sensitivity"]),
        "valid_from": row["valid_from"].isoformat() if row["valid_from"] else None,
        "valid_to": row["valid_to"].isoformat() if row["valid_to"] else None,
        "retrieval_policy": json_object(row["retrieval_policy"], "retrieval_policy"),
        "metadata": json_object(row["metadata"], "metadata"),
        "evidence_stance": "supports",
        "evidence_relevance": 1.0,
        "support_score": 0.92,
        "opposition_score": 0.0,
        "assessment_method": "reviewed_source_split",
        "assessment_method_version": "v1",
    }
    return normalize_proposal(proposal)
