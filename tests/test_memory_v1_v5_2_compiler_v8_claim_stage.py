#!/usr/bin/env python3
from __future__ import annotations

import unittest

from scripts.memory_v1_v5_2_projection_dispatch import render_claim_text
from scripts.memory_v1_v5_2_compiler_v8_claim_stage import TARGETS


class CompilerV8ClaimStageTest(unittest.TestCase):
    def test_exact_target_semantics_are_atomic(self) -> None:
        self.assertEqual(len(TARGETS), 4)
        self.assertEqual(
            [target["predicate"] for target in TARGETS.values()].count(
                "occupation.works_as"
            ),
            2,
        )
        self.assertEqual(
            {target["state_relation"] for target in TARGETS.values()},
            {"current", "historical"},
        )

    def test_historical_occupation_renderer_is_not_current_tense(self) -> None:
        source = {
            "predicate": "occupation.works_as",
            "subject_entity_type": "self",
            "subject_canonical_name": "Self",
            "object_kind": "entity",
            "object_entity_type": "concept",
            "object_canonical_name": "clinical psychologist",
            "polarity": "affirmed",
            "modality": "asserted",
            "temporal": {"state_relation": "historical"},
        }
        self.assertEqual(
            render_claim_text(source),
            "The user formerly worked as clinical psychologist.",
        )

    def test_current_relationship_renderers_remain_current(self) -> None:
        base = {
            "subject_entity_type": "self",
            "subject_canonical_name": "Self",
            "object_kind": "entity",
            "object_entity_type": "person",
            "object_canonical_name": "Monika",
            "polarity": "affirmed",
            "modality": "asserted",
            "temporal": {"state_relation": "current"},
        }
        self.assertEqual(
            render_claim_text({**base, "predicate": "relationship.caregiver_for"}),
            "The user is a caregiver for Monika.",
        )
        self.assertEqual(
            render_claim_text({**base, "predicate": "relationship.spouse_of"}),
            "The user is a spouse of Monika.",
        )

    def test_historical_pet_relationship_renderer_is_not_current_tense(self) -> None:
        source = {
            "predicate": "relationship.has_pet",
            "subject_entity_type": "self",
            "subject_canonical_name": "Self",
            "object_kind": "entity",
            "object_entity_type": "animal",
            "object_canonical_name": "Max",
            "polarity": "affirmed",
            "modality": "asserted",
            "temporal": {"state_relation": "historical"},
        }
        self.assertEqual(
            render_claim_text(source),
            "The user formerly had a pet named Max.",
        )

    def test_historical_pet_relationship_renderer_preserves_uncertainty(self) -> None:
        source = {
            "predicate": "relationship.has_pet",
            "subject_entity_type": "self",
            "subject_canonical_name": "Self",
            "object_kind": "entity",
            "object_entity_type": "animal",
            "object_canonical_name": "Max",
            "polarity": "affirmed",
            "modality": "uncertain",
            "temporal": {"state_relation": "historical"},
        }
        self.assertEqual(
            render_claim_text(source),
            "The user may formerly have had a pet named Max.",
        )

    def test_current_pet_relationship_renderer_remains_current(self) -> None:
        source = {
            "predicate": "relationship.has_pet",
            "subject_entity_type": "self",
            "subject_canonical_name": "Self",
            "object_kind": "entity",
            "object_entity_type": "animal",
            "object_canonical_name": "Max",
            "polarity": "affirmed",
            "modality": "asserted",
            "temporal": {"state_relation": "current"},
        }
        self.assertEqual(render_claim_text(source), "The user has a pet named Max.")


if __name__ == "__main__":
    unittest.main()
