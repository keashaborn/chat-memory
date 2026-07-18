#!/usr/bin/env python3
"""Offline tests for immutable, hash-locked predicate review."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from scripts.memory_v1_relational_extraction_v5_provider import canonical_sha256
from scripts.memory_v1_v5_apply_local_predicate_review import (
    PredicateReviewError,
    apply_predicate_decisions,
)
from scripts.memory_v1_v5_build_local_predicate_review_decision import (
    build_decision,
)


ROOT = Path(__file__).resolve().parents[1]
BLOCKER = "open_pet_species_domain_review_required"


def registry() -> dict:
    value = json.loads(
        (ROOT / "specs/memory_v1_predicate_registry_v5.json").read_text()
    )
    return {item["predicate"]: item for item in value["predicates"]}


def packet() -> dict:
    return {
        "entity_mentions": [{"mention_ref": "e00"}],
        "observations": [
            {
                "observation_ref": "o03",
                "predicate": "pet.species",
                "object": {
                    "kind": "literal",
                    "datatype": "text",
                    "value": "private breed value",
                },
                "reason_codes": ["explicit_pet_species", "source_direct"],
            }
        ],
    }


def decision_for(value: dict) -> dict:
    observation = value["observations"][0]
    return {
        "action": "reclassify_predicate",
        "expected_object_value_sha256": canonical_sha256(
            observation["object"]["value"]
        ),
        "expected_observation_sha256": canonical_sha256(observation),
        "from_predicate": "pet.species",
        "observation_ref": "o03",
        "replacement_reason_code": "reviewed_pet_breed_reclassification",
        "to_predicate": "pet.breed",
    }


def expect_reject(callable_value, expected: str) -> None:
    try:
        callable_value()
    except PredicateReviewError as exc:
        assert expected in str(exc)
    else:
        raise AssertionError(f"expected rejection containing {expected!r}")


def main() -> int:
    original = packet()
    original_copy = copy.deepcopy(original)
    derived, transformations = apply_predicate_decisions(
        original,
        [decision_for(original)],
        registry(),
        {BLOCKER},
    )
    assert original == original_copy
    assert derived["observations"][0]["predicate"] == "pet.breed"
    assert derived["observations"][0]["reason_codes"] == [
        "reviewed_pet_breed_reclassification",
        "source_direct",
    ]
    assert transformations[0]["object_value_sha256"] == canonical_sha256(
        "private breed value"
    )
    assert "private breed value" not in json.dumps(transformations)

    wrong_hash = decision_for(original)
    wrong_hash["expected_object_value_sha256"] = "0" * 64
    expect_reject(
        lambda: apply_predicate_decisions(
            original, [wrong_hash], registry(), {BLOCKER}
        ),
        "literal value hash differs",
    )

    illegal = decision_for(original)
    illegal["to_predicate"] = "pet.coat_color"
    expect_reject(
        lambda: apply_predicate_decisions(
            original, [illegal], registry(), {BLOCKER}
        ),
        "not allowlisted",
    )
    expect_reject(
        lambda: apply_predicate_decisions(
            original, [decision_for(original)], registry(), set()
        ),
        "required semantic blocker is absent",
    )

    unequal = registry()
    unequal["pet.breed"] = copy.deepcopy(unequal["pet.breed"])
    unequal["pet.breed"]["cardinality"] = "many"
    expect_reject(
        lambda: apply_predicate_decisions(
            original, [decision_for(original)], unequal, {BLOCKER}
        ),
        "changes persistence or resolution policy",
    )

    source_review = {
        "contract_version": "memory_v1_v5_local_packet_review_v1",
        "review_disposition": "manual_review_required",
        "blocking_codes": [BLOCKER],
        "owner_user_id": "1240822d-ac9a-4096-95aa-e2b24d36ef50",
        "packet_id": "2fa0db2a-0636-5353-9f3f-3f952bd4632b",
    }
    source_bundle = {
        "contract_version": "memory_v1_v5_stage_preflight_v1",
        "mode": "preflight_only_zero_write",
        "authorized_stage": False,
        "extraction_packet_text": json.dumps(original),
        "extraction_packet_sha256": canonical_sha256(original),
    }
    built_1 = build_decision(
        source_review=source_review,
        source_bundle=source_bundle,
        source_review_sha256="1" * 64,
        source_bundle_sha256="2" * 64,
        observation_ref="o03",
        from_predicate="pet.species",
        to_predicate="pet.breed",
        reviewer_ref="standing-owner-authorization",
    )
    built_2 = build_decision(
        source_review=source_review,
        source_bundle=source_bundle,
        source_review_sha256="1" * 64,
        source_bundle_sha256="2" * 64,
        observation_ref="o03",
        from_predicate="pet.species",
        to_predicate="pet.breed",
        reviewer_ref="standing-owner-authorization",
    )
    assert built_1 == built_2
    assert "private breed value" not in json.dumps(built_1)

    print("memory_v1_v5_apply_local_predicate_review: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
