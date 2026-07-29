from __future__ import annotations

import os
import unittest
from unittest.mock import patch
from uuid import UUID

from rag_engine.memory_v1_intent import classify_memory_intent
from rag_engine.memory_v1_v5_shadow_trace import (
    _maximum_sensitivity,
    classify_v5_shadow_context,
)


ACTOR = UUID("557ea042-cb82-48f8-9429-472e96c957ef")
QUERY = "Have I had any deaths in the family?"


class MemoryV1FamilyDeathRoutingTests(unittest.TestCase):
    def test_broad_pet_recall_routes_to_profile_not_loss(self) -> None:
        query = "Do you know anything about my pets?"
        plan = classify_memory_intent(query, request_classification="GENERAL")

        self.assertEqual(plan["memory_intent"], "personal_recall")
        self.assertEqual(plan["domains"], ["pet_profile"])
        self.assertTrue(plan["direct_relevance"])
        self.assertTrue(plan["claim_context"]["explicit_recall"])
        self.assertTrue(plan["claim_context"]["broad_profile_recall"])
        self.assertEqual(
            plan["claim_context"]["allowed_predicates"],
            [
                "identity.name",
                "pet.breed",
                "pet.sex",
                "relationship.has_pet",
            ],
        )
        shadow = classify_v5_shadow_context(query, "GENERAL")
        self.assertEqual(shadow["domain"], "pet_profile")
        self.assertEqual(
            shadow["allowed_predicate_prefixes"],
            [
                "identity.name",
                "pet.breed",
                "pet.sex",
                "relationship.has_pet",
            ],
        )

    def test_plural_pet_history_recall_routes_to_profile_not_loss(self) -> None:
        query = "Which pets have I had?"
        plan = classify_memory_intent(query, request_classification="GENERAL")

        self.assertEqual(plan["memory_intent"], "personal_recall")
        self.assertEqual(plan["domains"], ["pet_profile"])
        self.assertTrue(plan["direct_relevance"])
        self.assertTrue(plan["claim_context"]["explicit_recall"])
        self.assertTrue(plan["claim_context"]["broad_profile_recall"])
        self.assertEqual(
            plan["claim_context"]["allowed_predicates"],
            [
                "identity.name",
                "pet.breed",
                "pet.sex",
                "relationship.has_pet",
            ],
        )
        shadow = classify_v5_shadow_context(query, "GENERAL")
        self.assertEqual(shadow["domain"], "pet_profile")
        self.assertEqual(
            shadow["allowed_predicate_prefixes"],
            [
                "identity.name",
                "pet.breed",
                "pet.sex",
                "relationship.has_pet",
            ],
        )

    def test_explicit_pet_loss_routes_only_to_entity_scoped_death(self) -> None:
        plan = classify_memory_intent(
            "Do you remember when I lost my pet?",
            request_classification="GENERAL",
        )

        self.assertEqual(plan["domains"], ["pet_loss"])
        self.assertEqual(plan["memory_intent"], "personal_recall")
        self.assertTrue(plan["routes"]["governed_claims"])
        self.assertTrue(plan["claim_context"]["eligible"])
        self.assertFalse(plan["claim_context"]["broad_profile_recall"])
        self.assertEqual(
            plan["claim_context"]["allowed_predicates"],
            ["life_event.died"],
        )
        shadow = classify_v5_shadow_context(
            "Do you remember when I lost my pet?",
            "GENERAL",
        )
        self.assertFalse(shadow["eligible"])
        self.assertEqual(shadow["reason"], "entity_scope_v2_live_only")

    def test_name_correction_never_falls_back_to_generic_name(self) -> None:
        query = "Was it Nemo or Neko?"
        plan = classify_memory_intent(query, request_classification="GENERAL")

        self.assertEqual(plan["domains"], ["name_correction"])
        self.assertTrue(plan["routes"]["governed_claims"])
        self.assertEqual(
            plan["claim_context"]["allowed_predicates"],
            ["identity.name_canonical"],
        )
        shadow = classify_v5_shadow_context(query, "GENERAL")
        self.assertTrue(shadow["eligible"])
        self.assertEqual(
            shadow["allowed_predicate_prefixes"],
            ["identity.name_canonical"],
        )

    def test_direct_family_death_recall_allows_only_death_claims(self) -> None:
        plan = classify_memory_intent(QUERY, request_classification="GENERAL")

        self.assertEqual(plan["memory_intent"], "personal_recall")
        self.assertEqual(plan["domains"], ["family_death"])
        self.assertTrue(plan["direct_relevance"])
        self.assertTrue(plan["claim_context"]["explicit_recall"])
        self.assertEqual(
            plan["claim_context"]["allowed_predicates"],
            ["life_event.died"],
        )

    def test_shadow_policy_matches_authoritative_direct_recall_policy(self) -> None:
        context = classify_v5_shadow_context(QUERY, "GENERAL")

        self.assertTrue(context["eligible"])
        self.assertTrue(context["explicit_recall"])
        self.assertEqual(context["allowed_predicate_prefixes"], ["life_event.died"])
        with patch.dict(
            os.environ,
            {
                "MEMORY_V1_V5_SHADOW_MAX_SENSITIVITY": "medium",
                "MEMORY_V1_V5_SHADOW_EXPLICIT_MAX_SENSITIVITY": "high",
                "MEMORY_V1_V5_SHADOW_EXPLICIT_HIGH_USER_IDS": "",
            },
            clear=False,
        ):
            self.assertEqual(_maximum_sensitivity(ACTOR, context), "high")

    def test_technical_family_question_remains_suppressed(self) -> None:
        context = classify_v5_shadow_context(QUERY, "TECH")

        self.assertFalse(context["eligible"])
        self.assertEqual(context["reason"], "turn_intent:tech")


if __name__ == "__main__":
    unittest.main()
