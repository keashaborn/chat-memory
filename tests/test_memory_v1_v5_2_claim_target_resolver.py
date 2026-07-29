#!/usr/bin/env python3
from __future__ import annotations

import unittest

from scripts.memory_v1_v5_2_claim_target_review import stable_plan_id
from scripts.memory_v1_v5_2_claim_target_resolver import (
    ClaimTargetResolutionError,
    _claim_review_reason_codes,
    _claim_matches_projection,
)


class ClaimTargetResolverTest(unittest.TestCase):
    def projection(self) -> dict:
        return {
            "identity": {
                "subject_entity_id": "6db538e2-b7f9-48ff-b2bc-db757708b660",
                "predicate": "identity.name",
                "object_kind": "literal",
                "object_entity_id": None,
                "object_literal_sha256": (
                    "14a503162158bfa0f4cbb135658cf69c7ef9305416ec87dc"
                    "c49aec99877b8864"
                ),
                "semantic_key_sha256": "a" * 64,
            },
            "payload": {
                "canonical_text": "This animal's name is Keasha von Steffen Haus.",
                "surface_policy": "direct_or_relevant",
            },
        }

    def claim(self) -> dict:
        return {
            "claim_id": "8153ff74-0357-48e6-b341-39be1a54353d",
            "status": "supported",
            "subject_entity_id": "6db538e2-b7f9-48ff-b2bc-db757708b660",
            "predicate": "identity.name",
            "object_entity_id": None,
            "object_literal": {
                "kind": "literal",
                "datatype": "text",
                "value": "Keasha von Steffen Haus",
                "unit": None,
                "approximate": False,
            },
            "canonical_text": "This animal's name is Keasha von Steffen Haus.",
            "canonical_key": f"v5:{'a' * 64}",
            "retrieval_policy": {"surface_policy": "direct_or_relevant"},
            "current_revision_number": 2,
        }

    def test_exact_supported_claim_is_reinforceable(self) -> None:
        projection = self.projection()
        from scripts.memory_v1_v5_2_projection_dispatch import sha256

        projection["identity"]["object_literal_sha256"] = sha256(
            self.claim()["object_literal"]
        )
        _claim_matches_projection(self.claim(), projection)

    def test_non_supported_claim_is_not_reinforceable(self) -> None:
        projection = self.projection()
        claim = self.claim()
        claim["status"] = "candidate"
        with self.assertRaisesRegex(
            ClaimTargetResolutionError, "differs from the semantic projection"
        ):
            _claim_matches_projection(claim, projection)

    def test_renderer_drift_requires_review_without_changing_identity(self) -> None:
        projection = self.projection()
        claim = self.claim()
        from scripts.memory_v1_v5_2_projection_dispatch import sha256

        projection["identity"]["object_literal_sha256"] = sha256(
            claim["object_literal"]
        )
        claim["canonical_text"] = (
            "Keasha von Steffen Haus' name is Keasha von Steffen Haus."
        )
        self.assertEqual(
            _claim_review_reason_codes(claim, projection),
            ["existing_semantic_aggregate_render_drift"],
        )

    def test_surface_policy_drift_requires_review(self) -> None:
        projection = self.projection()
        claim = self.claim()
        from scripts.memory_v1_v5_2_projection_dispatch import sha256

        projection["identity"]["object_literal_sha256"] = sha256(
            claim["object_literal"]
        )
        claim["retrieval_policy"] = {
            "surface_policy": "mention_when_directly_relevant"
        }
        self.assertEqual(
            _claim_review_reason_codes(claim, projection),
            ["existing_semantic_aggregate_policy_drift"],
        )

    def test_semantic_identity_mismatch_is_a_hard_failure(self) -> None:
        projection = self.projection()
        claim = self.claim()
        from scripts.memory_v1_v5_2_projection_dispatch import sha256

        projection["identity"]["object_literal_sha256"] = sha256(
            claim["object_literal"]
        )
        claim["predicate"] = "pet.species"
        with self.assertRaisesRegex(
            ClaimTargetResolutionError, "differs from the semantic projection"
        ):
            _claim_review_reason_codes(claim, projection)

    def test_plan_identity_is_deterministic_and_owner_scoped(self) -> None:
        owner = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
        observation = "c0194481-bed5-438f-9407-07e398f14e50"
        self.assertEqual(
            stable_plan_id(owner, observation),
            stable_plan_id(owner, observation),
        )
        self.assertNotEqual(
            stable_plan_id(owner, observation),
            stable_plan_id(
                "557ea042-cb82-48f8-9429-472e96c957ef", observation
            ),
        )


if __name__ == "__main__":
    unittest.main()
