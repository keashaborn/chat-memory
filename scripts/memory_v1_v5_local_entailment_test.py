#!/usr/bin/env python3
"""Deterministic tests for the private local entailment worker."""

from __future__ import annotations

import uuid

from scripts.memory_v1_relational_extraction_v5_local_provider import (
    LocalProviderAdapterError,
    LocalStructuredResult,
    StaticLocalStructuredTransport,
)
from scripts.memory_v1_v5_local_entailment_provider import (
    ASSESSMENT_SCHEMA,
    assess,
    governed_decision,
    validate_assessment,
)
from scripts.memory_v1_v5_local_entailment_scheduler import operation_ids


MODEL = "qwen3-14b-local-extractor"


def result(decision: str, confidence: str) -> LocalStructuredResult:
    return LocalStructuredResult(
        response_id="synthetic",
        model=MODEL,
        finish_reason="stop",
        parsed={"decision": decision, "confidence": confidence},
        response_sha256="a" * 64,
        prompt_tokens=10,
        completion_tokens=5,
    )


def run_case(decision: str, confidence: str) -> tuple[str, str]:
    transport = StaticLocalStructuredTransport(
        result=result(decision, confidence)
    )
    assessment = assess(
        transport,
        model=MODEL,
        evidence_content="My name is Avery.",
        observation_payload={
            "predicate": "identity.name",
            "polarity": "positive",
            "modality": "asserted",
            "subject": {"entity_type": "self", "mention_kind": "self_reference"},
            "object": {"type": "text", "value": "Avery"},
            "source_spans": [{"start": 0, "end": 17, "span_sha256": "b" * 64}],
        },
    )
    assert assessment.local_model_calls == 1
    assert len(assessment.request_sha256) == 64
    assert len(assessment.output_schema_sha256) == 64
    return governed_decision(assessment)


def main() -> int:
    assert ASSESSMENT_SCHEMA["additionalProperties"] is False
    assert run_case("entailed", "high") == (
        "accepted",
        "predicate_entailment_v5_1_accepted",
    )
    assert run_case("entailed", "medium") == (
        "deferred",
        "predicate_semantics_unresolved",
    )
    assert run_case("contradicted", "high") == (
        "deferred",
        "source_contradicts_predicate",
    )
    assert run_case("ambiguous", "low") == (
        "deferred",
        "predicate_semantics_unresolved",
    )
    try:
        validate_assessment({"decision": "entailed", "confidence": "high", "why": "x"})
    except LocalProviderAdapterError:
        pass
    else:
        raise AssertionError("extra provider fields were accepted")

    owner = uuid.UUID("11111111-1111-4111-8111-111111111111")
    stage = uuid.UUID("22222222-2222-4222-8222-222222222222")
    observation = uuid.UUID("33333333-3333-4333-8333-333333333333")
    first = operation_ids(owner, stage, observation, "c" * 64)
    assert first == operation_ids(owner, stage, observation, "c" * 64)
    assert len(set(first)) == 2
    assert first != operation_ids(owner, stage, observation, "d" * 64)
    print("memory_v1_v5_local_entailment: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
