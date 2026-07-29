#!/usr/bin/env python3
from __future__ import annotations

import unittest

from scripts.memory_v1_v5_2_pet_profile_claim_stage import (
    TARGETS,
    literal,
)
from scripts.memory_v1_v5_2_projection_dispatch import render_claim_text


class PetProfileClaimStageTest(unittest.TestCase):
    def test_exact_eleven_atomic_targets(self) -> None:
        self.assertEqual(len(TARGETS), 11)
        self.assertEqual(
            sorted(target["predicate"] for target in TARGETS.values()),
            sorted(
                ["identity.name"] * 3
                + ["pet.breed"] * 2
                + ["pet.species"] * 3
                + ["relationship.has_pet"] * 3
            ),
        )

    def test_three_pet_relationships_are_explicitly_historical(self) -> None:
        relations = [
            target
            for target in TARGETS.values()
            if target["predicate"] == "relationship.has_pet"
        ]
        self.assertEqual(len(relations), 3)
        self.assertTrue(
            all(target["state_relation"] == "historical" for target in relations)
        )
        self.assertTrue(
            all("formerly had a pet named" in target["canonical_text"] for target in relations)
        )
        self.assertTrue(
            all(" has a pet named " not in target["canonical_text"] for target in relations)
        )

    def test_only_neko_historical_relationship_has_existing_aggregate(self) -> None:
        overlaps = [
            observation_id
            for observation_id, target in TARGETS.items()
            if target.get("expected_existing_aggregates", 0) != 0
        ]
        self.assertEqual(
            overlaps,
            ["bc8866ad-95e8-4413-832e-813f601eece6"],
        )
        self.assertEqual(
            TARGETS[overlaps[0]]["expected_existing_aggregates"],
            1,
        )

    def test_literal_contract_is_exact(self) -> None:
        self.assertEqual(
            literal("dog"),
            {
                "kind": "literal",
                "datatype": "text",
                "value": "dog",
                "unit": None,
                "approximate": False,
            },
        )

    def test_renderer_matches_every_target(self) -> None:
        for target in TARGETS.values():
            source = {
                "predicate": target["predicate"],
                "subject_entity_type": target["subject_entity_type"],
                "subject_canonical_name": target["subject_canonical_name"],
                "object_kind": target["object_kind"],
                "object_entity_type": target.get("object_entity_type"),
                "object_canonical_name": target.get("object_canonical_name"),
                "object_literal": target.get("object_literal"),
                "polarity": "affirmed",
                "modality": "asserted",
                "temporal": {"state_relation": target["state_relation"]},
            }
            with self.subTest(observation=target["observation_sha256"]):
                self.assertEqual(render_claim_text(source), target["canonical_text"])


if __name__ == "__main__":
    unittest.main()
