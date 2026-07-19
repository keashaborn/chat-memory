#!/usr/bin/env python3
"""Private, structured semantic-entailment judge for Memory V1 V5."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scripts.memory_v1_relational_extraction_v5_local_provider import (
    LlamaCppSecureTransport,
    LocalProviderAdapterError,
    LocalStructuredRequest,
    LocalStructuredTransport,
)
from scripts.memory_v1_relational_extraction_v5_provider import canonical_sha256


POLICY_VERSION = "memory_v1_v5_local_entailment_policy_v1"
PROVIDER_VERSION = "memory_v1_v5_local_entailment_provider_v1"

ASSESSMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "confidence"],
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["entailed", "contradicted", "ambiguous"],
        },
        "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
        },
    },
}

ASSESSMENT_INSTRUCTIONS = """You are a strict evidence-entailment classifier.
Decide whether the supplied source evidence directly supports the supplied
structured observation. Use only the source. Do not use world knowledge.

Labels:
- entailed: the source directly supports the complete observation.
- contradicted: the source directly conflicts with the observation.
- ambiguous: support is incomplete, indirect, uncertain, or depends on an
  unresolved interpretation.

Entity identity, predicate, polarity, modality, object, and time must all be
supported. A merely plausible inference is ambiguous. Return only JSON that
matches the schema. Never return prose, names, source text, or explanations.
"""


@dataclass(frozen=True)
class EntailmentAssessment:
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
    observation_payload: dict[str, Any],
    timeout_seconds: float,
    max_output_tokens: int,
) -> LocalStructuredRequest:
    if not evidence_content or len(evidence_content) > 200_000:
        raise ValueError("entailment evidence length is invalid")
    input_payload = {
        "source_evidence": evidence_content,
        "structured_observation": observation_payload,
    }
    return LocalStructuredRequest(
        model=model,
        instructions=ASSESSMENT_INSTRUCTIONS,
        input_text=_stable_json(input_payload),
        output_schema=ASSESSMENT_SCHEMA,
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
        seed=7,
        temperature=0.0,
        top_k=1,
        top_p=1.0,
        min_p=0.0,
    )


def assess(
    transport: LocalStructuredTransport,
    *,
    model: str,
    evidence_content: str,
    observation_payload: dict[str, Any],
    timeout_seconds: float = 300.0,
    max_output_tokens: int = 128,
) -> EntailmentAssessment:
    request = build_request(
        model=model,
        evidence_content=evidence_content,
        observation_payload=observation_payload,
        timeout_seconds=timeout_seconds,
        max_output_tokens=max_output_tokens,
    )
    result = transport.complete(request)
    if result.model != model:
        raise LocalProviderAdapterError(
            "local_entailment_model_alias_mismatch", retryable=False
        )
    if result.finish_reason not in {"stop", "eos_token"}:
        raise LocalProviderAdapterError(
            "local_entailment_incomplete_response", retryable=False
        )
    decision, confidence = validate_assessment(result.parsed)
    return EntailmentAssessment(
        decision=decision,
        confidence=confidence,
        request_sha256=request.request_sha256,
        response_sha256=result.response_sha256,
        output_schema_sha256=canonical_sha256(ASSESSMENT_SCHEMA),
        local_model_calls=int(transport.local_model_calls),
    )


def validate_assessment(value: Any) -> tuple[str, str]:
    if not isinstance(value, dict) or set(value) != {"decision", "confidence"}:
        raise LocalProviderAdapterError(
            "local_entailment_response_shape_invalid", retryable=False
        )
    decision = value.get("decision")
    confidence = value.get("confidence")
    if decision not in {"entailed", "contradicted", "ambiguous"}:
        raise LocalProviderAdapterError(
            "local_entailment_decision_invalid", retryable=False
        )
    if confidence not in {"high", "medium", "low"}:
        raise LocalProviderAdapterError(
            "local_entailment_confidence_invalid", retryable=False
        )
    return str(decision), str(confidence)


def governed_decision(assessment: EntailmentAssessment) -> tuple[str, str]:
    if assessment.decision == "entailed" and assessment.confidence == "high":
        return "accepted", "predicate_entailment_v5_1_accepted"
    if assessment.decision == "contradicted":
        return "deferred", "source_contradicts_predicate"
    return "deferred", "predicate_semantics_unresolved"


def _stable_json(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"))


__all__ = [
    "ASSESSMENT_SCHEMA",
    "EntailmentAssessment",
    "LlamaCppSecureTransport",
    "POLICY_VERSION",
    "PROVIDER_VERSION",
    "assess",
    "build_request",
    "governed_decision",
    "validate_assessment",
]
