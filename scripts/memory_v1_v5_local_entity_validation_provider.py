#!/usr/bin/env python3
"""Private structured source validator for a proposed named entity."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from scripts.memory_v1_relational_extraction_v5_local_provider import (
    LocalProviderAdapterError,
    LocalStructuredRequest,
    LocalStructuredTransport,
)
from scripts.memory_v1_relational_extraction_v5_provider import canonical_sha256


POLICY_VERSION = "memory_v1_v5_local_entity_validation_policy_v1"
PROVIDER_VERSION = "memory_v1_v5_local_entity_validation_provider_v1"

ASSESSMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "confidence"],
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["supported", "contradicted", "ambiguous"],
        },
        "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
        },
    },
}

ASSESSMENT_INSTRUCTIONS = """You are a strict named-entity source validator.
Decide whether the supplied source explicitly refers to the supplied named
entity mention. Use only the source. Do not use world knowledge.

Labels:
- supported: the source explicitly contains or unambiguously refers to the
  supplied name, and the proposed entity type is compatible with the wording.
- contradicted: the source directly conflicts with the supplied name or type.
- ambiguous: the name/type is incomplete, indirect, uncertain, or requires an
  inference not stated in the source.

Do not decide whether any relationship or other observation about the entity
is true; this check is only entity existence/name/type admission. Return only
JSON matching the schema. Never return prose, names, or source text.
"""


@dataclass(frozen=True)
class EntityValidationAssessment:
    decision: str
    confidence: str
    request_sha256: str
    response_sha256: str
    output_schema_sha256: str
    local_model_calls: int


def build_request(
    *,
    model: str,
    evidence_content: str,
    entity_mention: dict[str, Any],
    timeout_seconds: float,
    max_output_tokens: int,
) -> LocalStructuredRequest:
    if not evidence_content or len(evidence_content) > 200_000:
        raise ValueError("entity-validation evidence length is invalid")
    if not isinstance(entity_mention, dict):
        raise ValueError("entity-validation mention is invalid")
    return LocalStructuredRequest(
        model=model,
        instructions=ASSESSMENT_INSTRUCTIONS,
        input_text=json.dumps(
            {
                "source_evidence": evidence_content,
                "structured_entity_mention": entity_mention,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        output_schema=ASSESSMENT_SCHEMA,
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
        seed=7,
        temperature=0.0,
        top_k=1,
        top_p=1.0,
        min_p=0.0,
    )


def validate_assessment(value: Any) -> tuple[str, str]:
    if not isinstance(value, dict) or set(value) != {"decision", "confidence"}:
        raise LocalProviderAdapterError(
            "local_entity_validation_response_shape_invalid", retryable=False
        )
    decision = value.get("decision")
    confidence = value.get("confidence")
    if decision not in {"supported", "contradicted", "ambiguous"}:
        raise LocalProviderAdapterError(
            "local_entity_validation_decision_invalid", retryable=False
        )
    if confidence not in {"high", "medium", "low"}:
        raise LocalProviderAdapterError(
            "local_entity_validation_confidence_invalid", retryable=False
        )
    return str(decision), str(confidence)


def assess(
    transport: LocalStructuredTransport,
    *,
    model: str,
    evidence_content: str,
    entity_mention: dict[str, Any],
    timeout_seconds: float = 300.0,
    max_output_tokens: int = 128,
) -> EntityValidationAssessment:
    request = build_request(
        model=model,
        evidence_content=evidence_content,
        entity_mention=entity_mention,
        timeout_seconds=timeout_seconds,
        max_output_tokens=max_output_tokens,
    )
    result = transport.complete(request)
    if result.model != model:
        raise LocalProviderAdapterError(
            "local_entity_validation_model_alias_mismatch", retryable=False
        )
    if result.finish_reason not in {"stop", "eos_token"}:
        raise LocalProviderAdapterError(
            "local_entity_validation_incomplete_response", retryable=False
        )
    decision, confidence = validate_assessment(result.parsed)
    return EntityValidationAssessment(
        decision=decision,
        confidence=confidence,
        request_sha256=request.request_sha256,
        response_sha256=result.response_sha256,
        output_schema_sha256=canonical_sha256(ASSESSMENT_SCHEMA),
        local_model_calls=int(transport.local_model_calls),
    )


def governed_decision(
    assessment: EntityValidationAssessment,
) -> tuple[str, str]:
    if assessment.decision == "supported" and assessment.confidence == "high":
        return "accepted", "explicit_named_entity_supported"
    if assessment.decision == "contradicted":
        return "deferred", "source_contradicts_named_entity"
    return "deferred", "named_entity_support_unresolved"


__all__ = [
    "ASSESSMENT_SCHEMA",
    "EntityValidationAssessment",
    "POLICY_VERSION",
    "PROVIDER_VERSION",
    "assess",
    "build_request",
    "governed_decision",
    "validate_assessment",
]
