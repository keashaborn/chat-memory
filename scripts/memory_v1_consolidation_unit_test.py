#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

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
    _normalize_temporal_candidate,
    _validate_candidate,
    looks_like_artifact,
    persist_extraction,
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
    life_preference = preference(
        canonical_text="The user likes jazz.",
        preference_class="life",
        preference_domain="music",
        preference_key="genre.jazz",
        surface_policy="mention_when_relevant",
    )
    _validate_candidate(life_preference)
    expect_error(
        lambda: _validate_candidate(
            life_preference.model_copy(update={"surface_policy": "silent_style_influence"})
        ),
        ConsolidationError,
    )
    expect_error(
        lambda: _validate_candidate(
            claim(
                canonical_text=(
                    "The user has not worked in that field for 15-20 years as of "
                    "2026-07-14."
                ),
                predicate="work.inactive_duration",
                object_literal="15-20 years as of 2026-07-14",
            )
        ),
        ConsolidationError,
    )
    expect_error(
        lambda: _validate_candidate(
            claim(
                canonical_text="The user reports they are not an alcoholic anymore.",
                predicate="is_alcoholic",
                object_literal="false",
                sensitivity="high",
            )
        ),
        ConsolidationError,
    )
    expect_error(
        lambda: _validate_candidate(
            claim(
                canonical_text=(
                    "The user spends time using tractors, farming, and raising cattle."
                ),
                predicate="activity.ranch_work",
                object_literal="using tractors, farming, and raising cattle",
            )
        ),
        ConsolidationError,
    )
    expect_error(
        lambda: _validate_candidate(
            response_preference.model_copy(update={"surface_policy": "mention_when_relevant"})
        ),
        ConsolidationError,
    )

    anchored = claim(
        canonical_text="The user stopped drinking alcohol on 2026-06-30.",
        predicate="health.alcohol_use.stopped",
        object_literal="2026-06-30",
        valid_from="2026-06-30T21:44:14Z",
        sensitivity="high",
    )
    _validate_candidate(anchored)
    anchored_proposal = _claim_proposal(anchored)
    assert anchored_proposal["valid_from"] == "2026-06-30T21:44:14+00:00"
    normalized = _normalize_temporal_candidate(
        claim(
            canonical_text="The user quit alcohol about two weeks ago.",
            predicate="health.alcohol_use.stopped",
            object_literal="quit about two weeks ago",
            sensitivity="high",
        ),
        datetime(2026, 7, 14, 21, 44, 14, tzinfo=timezone.utc),
    )
    _validate_candidate(normalized)
    assert normalized.valid_from == "2026-06-30T21:44:14Z"
    assert "around 2026-06-30" in normalized.canonical_text
    normalized_before = _normalize_temporal_candidate(
        claim(
            canonical_text=(
                "The user quit alcohol about two weeks before "
                "2026-07-14T21:44:14Z."
            ),
            predicate="health.alcohol_use.stopped",
            object_literal="Quit alcohol around 2026-06-30.",
            valid_from="2026-06-30T21:44:14Z",
            sensitivity="high",
        ),
        datetime(2026, 7, 14, 21, 44, 14, tzinfo=timezone.utc),
    )
    _validate_candidate(normalized_before)
    assert "two weeks before" not in normalized_before.canonical_text
    expect_error(
        lambda: _validate_candidate(
            anchored.model_copy(
                update={
                    "canonical_text": "The user stopped drinking about two weeks ago.",
                    "object_literal": "about two weeks ago",
                }
            )
        ),
        ConsolidationError,
    )
    expect_error(
        lambda: _validate_candidate(
            anchored.model_copy(update={"valid_from": "2026-06-30T21:44:14"})
        ),
        ConsolidationError,
    )
    expect_error(
        lambda: _validate_candidate(
            claim(
                canonical_text="The user feels blocked lately.",
                predicate="wellbeing.current_state",
                object_literal="feeling blocked lately",
            )
        ),
        ConsolidationError,
    )
    expect_error(
        lambda: _validate_candidate(
            claim(
                canonical_text="The user farms; the user raises cattle.",
                predicate="activity.farming",
                object_literal="farms; raises cattle",
            )
        ),
        ConsolidationError,
    )

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
    invalid_preference = preference(preference_class="none")
    rejected = asyncio.run(
        persist_extraction(
            None,
            actor_user_id=uuid.UUID("557ea042-cb82-48f8-9429-472e96c957ef"),
            evidence_id=uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            extraction=ExtractionResult(
                contains_quoted_or_pasted_content=False,
                candidates=[invalid_preference],
            ),
            source_text="A transient creative request.",
            observed_at=datetime.now(timezone.utc),
        )
    )
    assert rejected["validation_rejections"] == [
        {"lane": "preference", "reason": "preference_class is required"}
    ]
    print("memory_v1_consolidation_unit_test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
