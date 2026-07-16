#!/usr/bin/env python3
from __future__ import annotations

import uuid

from memory_v1_v5_stage_preflight import resolve_mention


def mention(
    entity_ref: str,
    entity_type: str,
    kind: str,
    name: str | None,
    role: str | None = None,
) -> dict:
    return {
        "entity_ref": entity_ref,
        "entity_type": entity_type,
        "mention_kind": kind,
        "name_text": name,
        "relationship_role": role,
        "source_spans": [{"start": 0, "end": 1, "span_sha256": "a" * 64}],
        "extraction_confidence": 1.0,
        "reason_codes": ["explicit_source_mention"],
    }


def candidate(entity_type: str, suffix: int, *, alias: bool = False) -> dict:
    return {
        "entity_id": uuid.UUID(f"00000000-0000-4000-8000-{suffix:012d}"),
        "entity_type": entity_type,
        "exact_canonical_name": not alias,
        "exact_alias": alias,
    }


def observation(entity_ref: str, *, sensitivity: str = "low", correction: bool = False) -> dict:
    return {
        "subject_entity_ref": entity_ref,
        "object": {"kind": "literal", "value": "x"},
        "sensitivity": sensitivity,
        "projection_class": "correction" if correction else "direct_claim",
        "modality": "corrective" if correction else "asserted",
    }


def main() -> None:
    self_result = resolve_mention(
        mention("e01", "self", "self_reference", None),
        [],
        [candidate("self", 1)],
    )
    assert self_result["action"] == "link_existing"
    assert self_result["decision_state"] == "auto_link_eligible"

    exact = resolve_mention(
        mention("e02", "animal", "named", "Koda"),
        [observation("e02")],
        [candidate("animal", 2)],
    )
    assert exact["decision_state"] == "auto_link_eligible"

    sensitive = resolve_mention(
        mention("e03", "person", "named", "Mira"),
        [observation("e03", sensitivity="high")],
        [candidate("person", 3)],
    )
    assert sensitive["action"] == "link_existing"
    assert sensitive["decision_state"] == "manual_review_required"

    correction = resolve_mention(
        mention("e04", "animal", "named", "Neko"),
        [observation("e04", correction=True)],
        [candidate("animal", 4, alias=True)],
    )
    assert correction["decision_state"] == "manual_review_required"

    new_named = resolve_mention(
        mention("e05", "person", "named", "Avery"), [], []
    )
    assert new_named["action"] == "create_new"
    assert new_named["decision_state"] == "manual_review_required"

    ambiguous = resolve_mention(
        mention("e06", "person", "named", "Sam"),
        [],
        [candidate("person", 6), candidate("person", 7)],
    )
    assert ambiguous["action"] == "defer"
    assert ambiguous["decision_state"] == "deferred"

    role_only = resolve_mention(
        mention("e07", "person", "role_only", None, "father"), [], []
    )
    assert role_only["action"] == "defer"

    project = resolve_mention(
        mention("e08", "project", "named", "Verbal Sage"), [], []
    )
    assert project["action"] == "defer"

    ambiguous = resolve_mention(
        mention("e09", "concept", "named", "unclear term"),
        [],
        [],
        [{
            "reason_code": "ambiguous_transcription",
            "source_spans": [{"start": 0, "end": 1, "span_sha256": "a" * 64}],
        }],
    )
    assert ambiguous["action"] == "defer"
    assert ambiguous["review_reason_codes"] == ["ambiguous_transcription_defer"]

    correction_target = resolve_mention(
        mention("e10", "animal", "anonymous", None, "pet:corrected_name_subject"),
        [observation("e10", correction=True)],
        [],
    )
    assert correction_target["action"] == "defer"
    assert correction_target["review_reason_codes"] == [
        "correction_target_resolution_required"
    ]

    print("memory_v1_v5_stage_preflight: PASS")


if __name__ == "__main__":
    main()
