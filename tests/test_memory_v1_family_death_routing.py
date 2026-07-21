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
