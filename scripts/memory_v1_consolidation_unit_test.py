#!/usr/bin/env python3
from __future__ import annotations

from pydantic import ValidationError

from rag_engine.memory_v1_consolidation import (
    ConsolidationError,
    EXTRACTOR_VERSION,
    ExtractedCandidate,
    ExtractionResult,
    _claim_proposal,
    _canonicalize_generated_key,
    _explicit_correction_eligible,
    _project_key_is_explicit,
    _key_identity,
    _validate_candidate,
    looks_like_artifact,
)
from scripts.memory_v1_consolidation_worker import checkpointed_extraction


EMPTY = {
    "valid_from": "",
    "valid_to": "",
    "preference_class": "none",
    "preference_domain": "",
    "preference_key": "",
    "polarity": "none",
    "stability": "none",
    "surface_policy": "none",
    "project_key": "",
    "knowledge_kind": "none",
    "knowledge_key": "",
    "document_state": "none",
    "authority_level": "none",
    "reason_codes": ["explicit_user_statement"],
}


def claim(**overrides: object) -> ExtractedCandidate:
    values = {
        **EMPTY,
        "lane": "claim",
        "canonical_text": "The user's preferred name is Eric.",
        "explicit": True,
        "correction": False,
        "sensitivity": "low",
        "confidence": 0.99,
        "subject_entity_key": "user:self",
        "subject_entity_type": "person",
        "subject_canonical_name": "User",
        "predicate": "identity.preferred_name",
        "object_literal": "Eric",
        **overrides,
    }
    return ExtractedCandidate.model_validate(values)


def preference(**overrides: object) -> ExtractedCandidate:
    values = {
        **EMPTY,
        "lane": "preference",
        "canonical_text": "The user prefers concise responses.",
        "explicit": True,
        "correction": False,
        "sensitivity": "low",
        "confidence": 0.98,
        "subject_entity_key": "",
        "subject_entity_type": "",
        "subject_canonical_name": "",
        "predicate": "",
        "object_literal": "",
        "preference_class": "response",
        "preference_domain": "response.style",
        "preference_key": "response.length",
        "polarity": "prefer",
        "stability": "stable",
        "surface_policy": "silent_style_influence",
        **overrides,
    }
    return ExtractedCandidate.model_validate(values)


def project(**overrides: object) -> ExtractedCandidate:
    values = {
        **EMPTY,
        "lane": "project_knowledge",
        "canonical_text": "Verbal Sage memory is owned only by the account UUID.",
        "explicit": True,
        "correction": False,
        "sensitivity": "medium",
        "confidence": 0.99,
        "subject_entity_key": "",
        "subject_entity_type": "",
        "subject_canonical_name": "",
        "predicate": "",
        "object_literal": "",
        "project_key": "verbal-sage",
        "knowledge_kind": "constraint",
        "knowledge_key": "memory.owner_boundary",
        "document_state": "ratified",
        "authority_level": "user_ratified",
        **overrides,
    }
    return ExtractedCandidate.model_validate(values)


def expect_error(fn, expected: type[Exception]) -> None:
    try:
        fn()
    except expected:
        return
    raise AssertionError(f"expected {expected.__name__}")


def main() -> int:
    ordinary = claim()
    _validate_candidate(ordinary)
    proposal = _claim_proposal(ordinary)
    assert proposal["subject"]["entity_key"] == "user:self"
    assert proposal["predicate"] == "identity.preferred_name"
    assert not _explicit_correction_eligible(ordinary, "My preferred name is Eric.")

    anonymous_self = claim(subject_entity_type="", subject_canonical_name="")
    _validate_candidate(anonymous_self)
    anonymous_proposal = _claim_proposal(anonymous_self)
    assert anonymous_proposal["subject"]["entity_type"] == "person"
    assert anonymous_proposal["subject"]["canonical_name"] == "User"

    correction = claim(correction=True, confidence=0.99)
    assert _explicit_correction_eligible(
        correction,
        "Correction: my preferred name should be Eric, not Erik.",
    )
    assert not _explicit_correction_eligible(
        correction.model_copy(update={"sensitivity": "high"}),
        "Correction: my preferred name should be Eric, not Erik.",
    )

    response_preference = preference()
    _validate_candidate(response_preference)
    assert response_preference.surface_policy == "silent_style_influence"

    project_candidate = project()
    _validate_candidate(project_candidate)
    assert project_candidate.project_key == "verbal-sage"
    assert _project_key_is_explicit("verbal-sage", "The Verbal Sage architecture is changing.")
    assert not _project_key_is_explicit("verbal-sage", "The unrelated project is changing.")
    assert _key_identity("Verbal Sage") == _key_identity("verbal-sage")
    assert _key_identity("Other Project") != _key_identity("verbal-sage")
    assert _canonicalize_generated_key("Memory Owner Boundary") == "memory_owner_boundary"
    assert not looks_like_artifact(
        "I decided that the Verbal Sage project will use account UUIDs."
    )
    assert looks_like_artifact("# Plan\n\n" + ("Detailed material.\n" * 150))
    assert looks_like_artifact("```sql\n" + ("SELECT owner_user_id;\n" * 30) + "```")

    expect_error(
        lambda: _validate_candidate(project(project_key="Verbal Sage")),
        ConsolidationError,
    )
    expect_error(
        lambda: ExtractionResult.model_validate(
            {
                "contains_quoted_or_pasted_content": False,
                "candidates": [],
                "unexpected": True,
            }
        ),
        ValidationError,
    )
    checkpoint_payload = ExtractionResult(
        contains_quoted_or_pasted_content=False,
        candidates=[ordinary],
    ).model_dump(mode="json")
    replayed = checkpointed_extraction(
        {
            "source_sha256": "a" * 64,
            "result": {
                "extraction_checkpoint": {
                    "pipeline": EXTRACTOR_VERSION,
                    "source_sha256": "a" * 64,
                    "model_response_id": "resp_test",
                    "extraction": checkpoint_payload,
                }
            },
        }
    )
    assert replayed is not None
    assert replayed[1] == "resp_test"
    assert replayed[0].candidates[0].predicate == "identity.preferred_name"
    expect_error(
        lambda: checkpointed_extraction(
            {
                "source_sha256": "b" * 64,
                "result": {
                    "extraction_checkpoint": {
                        "pipeline": EXTRACTOR_VERSION,
                        "source_sha256": "a" * 64,
                        "model_response_id": "resp_test",
                        "extraction": checkpoint_payload,
                    }
                },
            }
        ),
        RuntimeError,
    )
    print("memory_v1_consolidation_unit_test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
