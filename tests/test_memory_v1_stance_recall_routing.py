from __future__ import annotations

import unittest

from rag_engine.memory_v1_intent import classify_memory_intent
from rag_engine.memory_v1_v5_shadow_trace import classify_v5_shadow_context


class StanceRecallRoutingTest(unittest.TestCase):
    def assert_stance_route(self, query: str, classification: str = "GENERAL") -> None:
        plan = classify_memory_intent(
            query,
            request_classification=classification,
        )
        self.assertEqual(plan["memory_intent"], "personal_recall")
        self.assertEqual(plan["domains"], ["stance_recall"])
        self.assertTrue(plan["direct_relevance"])
        self.assertTrue(plan["claim_context"]["explicit_recall"])
        self.assertEqual(
            plan["claim_context"]["allowed_predicates"],
            ["stance.reported"],
        )
        shadow = classify_v5_shadow_context(query, classification)
        self.assertTrue(shadow["eligible"])
        self.assertEqual(shadow["domain"], "stance_recall")
        self.assertEqual(
            shadow["allowed_predicate_prefixes"],
            ["stance.reported"],
        )

    def test_what_have_i_said_routes_to_reported_stances(self) -> None:
        self.assert_stance_route(
            "What have I said about worrying about the future?"
        )

    def test_what_do_i_believe_routes_to_reported_stances(self) -> None:
        self.assert_stance_route("What do I believe about worrying?")

    def test_broad_opinion_recall_routes_to_reported_stances(self) -> None:
        self.assert_stance_route("What are some opinions I have shared?")

    def test_fm_explicit_prior_stance_recall_remains_memory_eligible(self) -> None:
        self.assert_stance_route(
            "What have I said about how Fractal Monism can help people?",
            classification="FM_CONCEPTUAL",
        )

    def test_fm_conceptual_turn_without_prior_user_recall_stays_suppressed(
        self,
    ) -> None:
        plan = classify_memory_intent(
            "Explain how Fractal Monism can help people.",
            request_classification="FM_CONCEPTUAL",
        )
        self.assertEqual(plan["memory_intent"], "none")
        self.assertFalse(plan["direct_relevance"])
        self.assertFalse(plan["routes"]["governed_claims"])
        self.assertEqual(
            plan["claim_context"],
            {
                "eligible": False,
                "reason": "turn_intent:fm_conceptual",
            },
        )

    def test_information_providing_stance_does_not_retrieve_stances(self) -> None:
        plan = classify_memory_intent(
            "I believe worrying about next month does not help.",
            request_classification="GENERAL",
        )
        self.assertEqual(plan["memory_intent"], "none")
        self.assertFalse(plan["claim_context"]["eligible"])

    def test_technical_stance_question_remains_suppressed(self) -> None:
        context = classify_v5_shadow_context(
            "What have I said about stance.reported routing?",
            "TECH",
        )
        self.assertFalse(context["eligible"])
        self.assertEqual(context["reason"], "turn_intent:tech")


if __name__ == "__main__":
    unittest.main()
