from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.memory_v1_relationship_observation_v5_1 import (
    directed_role_supported_by_source,
    explicit_relationship_assertions,
    normalize_relationship_observation,
    predicate_supported_by_source,
)


class RelationshipSourceAssertionsV51Test(unittest.TestCase):
    def assert_predicates(self, text: str, expected: set[str]) -> None:
        actual = {
            item.predicate for item in explicit_relationship_assertions(text)
        }
        self.assertEqual(actual, expected)

    def test_explicit_single_relationships(self) -> None:
        cases = (
            (
                "I am the primary caregiver for my father Jerry.",
                {"relationship.caregiver_for"},
            ),
            ("I currently live with Jordan.", {"relationship.lives_with"}),
            ("Nora is my sister, not my cousin.", {"relationship.sibling_of"}),
            ("Derek is my wife's brother.", {"relationship.in_law_of"}),
            ("Derek is my brother-in-law.", {"relationship.in_law_of"}),
            (
                "Jessica was my wife until she died in 2024.",
                {"relationship.spouse_of"},
            ),
            (
                "Jamie and I play on the same softball team, but we do not work together.",
                {"relationship.teammate_of"},
            ),
            ("I report directly to Avery at work.", {"relationship.manager_of"}),
            ("Avery reports directly to me at work.", {"relationship.manager_of"}),
            (
                "Dr. Chen is my physician, not a personal friend.",
                {"relationship.healthcare_provider_for"},
            ),
            ("Dr. Patel is my cardiologist.", {"relationship.healthcare_provider_for"}),
            (
                "Kelly has permission to edit my LifeSwitch plan and helps me manage it.",
                {"relationship.plan_helper_for"},
            ),
            (
                "Professor Lee teaches my statistics course; she is not my manager.",
                {"relationship.teacher_of"},
            ),
            ("There is ongoing tension between Pat and me.", {"social.experiences_tension_with"}),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assert_predicates(text, expected)

    def test_explicit_compound_relationships(self) -> None:
        cases = (
            (
                "Sam and I share an apartment as roommates.",
                {"relationship.lives_with", "relationship.roommate_of"},
            ),
            (
                "I live with my wife Jessica.",
                {"relationship.lives_with", "relationship.spouse_of"},
            ),
            (
                "Morgan is my friend and also my primary care doctor.",
                {"relationship.friend_of", "relationship.healthcare_provider_for"},
            ),
            (
                "Morgan is my coworker, workout partner, and friend.",
                {
                    "relationship.coworker_of",
                    "relationship.friend_of",
                    "relationship.training_partner_of",
                },
            ),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assert_predicates(text, expected)

    def test_negated_nested_and_metaphorical_relations_do_not_compile(self) -> None:
        for text in (
            "Rosa is my aunt's neighbor.",
            "Nora is my sister's friend.",
            "Nora is like a sister to me, but we are not related.",
        ):
            with self.subTest(text=text):
                self.assert_predicates(text, set())

    def test_historical_avoidance_is_preserved_as_a_closed_state(self) -> None:
        assertions = explicit_relationship_assertions(
            "I used to avoid Pat, but I do not avoid him anymore."
        )
        self.assertEqual(len(assertions), 1)
        self.assertEqual(assertions[0].predicate, "social.avoids")
        self.assertTrue(assertions[0].historical_end)

    def test_source_syntax_fixes_directed_roles(self) -> None:
        cases = (
            (
                "relationship.caregiver_for",
                "I am the primary caregiver for my father Jerry.",
                "care_recipient",
            ),
            (
                "relationship.manager_of",
                "I report directly to Avery at work.",
                "manager",
            ),
            (
                "relationship.manager_of",
                "Avery reports directly to me at work.",
                "direct_report",
            ),
            (
                "relationship.healthcare_provider_for",
                "Dr. Chen is my physician.",
                "doctor",
            ),
            (
                "relationship.teacher_of",
                "Professor Lee teaches my statistics course.",
                "teacher",
            ),
            (
                "social.experiences_tension_with",
                "There is ongoing tension between Pat and me.",
                "person_in_tension",
            ),
        )
        for predicate, text, expected in cases:
            with self.subTest(predicate=predicate):
                self.assertEqual(
                    directed_role_supported_by_source(predicate, text),
                    expected,
                )

    def test_negated_training_partner_is_not_entailed(self) -> None:
        text = "Morgan is my strength coach, not my workout partner."
        self.assertNotIn(
            "relationship.training_partner_of",
            {
                item.predicate
                for item in explicit_relationship_assertions(text)
            },
        )
        self.assertFalse(
            predicate_supported_by_source(
                "relationship.training_partner_of",
                "workout_partner",
                text,
            )
        )

    def test_every_explicit_assertion_is_entailed_by_its_source(self) -> None:
        texts = (
            "I currently live with Jordan.",
            "Nora is my sister, not my cousin.",
            "Derek is my wife's brother.",
            "Sam and I share an apartment as roommates.",
            "I live with my wife Jessica.",
            "Jamie and I play on the same softball team, but we do not work together.",
            "Dr. Chen is my physician, not a personal friend.",
            "Dr. Patel is my cardiologist.",
            "Kelly has permission to edit my LifeSwitch plan and helps me manage it.",
            "Professor Lee teaches my statistics course; she is not my manager.",
            "I used to avoid Pat, but I do not avoid him anymore.",
        )
        for text in texts:
            with self.subTest(text=text):
                assertions = explicit_relationship_assertions(text)
                self.assertTrue(assertions)
                for assertion in assertions:
                    self.assertTrue(
                        predicate_supported_by_source(
                            assertion.predicate,
                            assertion.named_party_role,
                            text,
                        )
                    )

    def test_nested_possessive_relationship_is_deferred(self) -> None:
        text = "Rosa is my aunt's neighbor."
        entities = [
            {
                "entity_ref": "e00",
                "entity_type": "self",
                "relationship_role": "user:self",
            },
            {
                "entity_ref": "e01",
                "entity_type": "person",
                "relationship_role": "relationship:neighbor",
            },
        ]
        observation = {
            "subject_entity_ref": "e00",
            "predicate": "relationship.neighbor_of",
            "object": {"kind": "entity", "entity_ref": "e01"},
            "source_spans": [
                {"start": 0, "end": len(text), "quote": text}
            ],
        }
        decision = normalize_relationship_observation(
            observation,
            entities,
            text,
            source_class="owner_assertion",
        )
        self.assertEqual(decision.status, "defer")
        self.assertEqual(
            decision.reason_code,
            "relationship_belongs_to_third_party",
        )

    def test_explicit_in_law_survives_nested_possessive_guard(self) -> None:
        text = "Derek is my wife's brother."
        entities = [
            {
                "entity_ref": "e00",
                "entity_type": "self",
                "relationship_role": "user:self",
            },
            {
                "entity_ref": "e01",
                "entity_type": "person",
                "relationship_role": "relationship:brother_in_law",
            },
        ]
        observation = {
            "subject_entity_ref": "e00",
            "predicate": "relationship.in_law_of",
            "object": {"kind": "entity", "entity_ref": "e01"},
            "source_spans": [
                {"start": 0, "end": len(text), "quote": text}
            ],
            "projection_class": "direct_claim",
            "surface_policy": "direct_or_relevant",
            "sensitivity": "medium",
            "modality": "asserted",
            "polarity": "affirmed",
            "temporal": {
                "semantic": "state_validity",
                "shape": "none",
                "basis": "none",
                "source_form": "none",
                "certainty": "unknown",
                "precision": "unknown",
                "instant": None,
                "calendar_range": None,
                "instant_range": None,
                "relative_offset": None,
                "recurrence": None,
                "anchored_to_source_time": False,
                "reason_codes": [],
            },
            "reason_codes": [],
        }
        decision = normalize_relationship_observation(
            observation,
            entities,
            text,
            source_class="owner_assertion",
        )
        self.assertIsNotNone(decision.normalized_observation)


if __name__ == "__main__":
    unittest.main()
