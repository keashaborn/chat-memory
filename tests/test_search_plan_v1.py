from __future__ import annotations

import unittest

from rag_engine.search_plan_v1 import create_search_plan_v1


class SearchPlanV1Tests(unittest.TestCase):
    def test_stable_fact_stays_on_normal_chat(self) -> None:
        plan = create_search_plan_v1("What is the capital of France?")
        self.assertEqual(plan.decision, "no_search")
        self.assertEqual(plan.selected_route, "normal_chat")
        self.assertFalse(plan.external_web_access)
        self.assertEqual(plan.budget.max_searches, 0)

    def test_user_prohibition_overrides_freshness(self) -> None:
        plan = create_search_plan_v1(
            "Do not search the web. What is the latest OpenAI news?"
        )
        self.assertEqual(plan.decision, "no_search")
        self.assertEqual(plan.selected_route, "normal_chat")
        self.assertEqual(
            plan.reason_codes,
            ("search_prohibited_by_user",),
        )

    def test_current_openai_news_uses_live_news_pack(self) -> None:
        plan = create_search_plan_v1("What happened with OpenAI today?")
        self.assertEqual(plan.decision, "live")
        self.assertEqual(plan.selected_route, "current_news")
        self.assertEqual(plan.policy_pack, "software_security")
        self.assertIn("freshness_required", plan.reason_codes)
        self.assertIn("trusted_current_news_scope", plan.reason_codes)
        self.assertEqual(plan.budget.max_searches, 4)
        self.assertEqual(plan.budget.max_sources, 10)

    def test_general_current_news_uses_bounded_current_news_pack(self) -> None:
        plan = create_search_plan_v1(
            "Is there any news about the US and Iran today?"
        )
        self.assertEqual(plan.decision, "live")
        self.assertEqual(plan.selected_route, "current_news")
        self.assertEqual(plan.policy_pack, "current_news")
        self.assertIn("freshness_required", plan.reason_codes)
        self.assertIn("general_current_news_scope", plan.reason_codes)
        self.assertEqual(plan.budget.max_searches, 4)
        self.assertEqual(plan.budget.max_sources, 10)

    def test_health_news_stays_on_trusted_health_route(self) -> None:
        plan = create_search_plan_v1(
            "Is there any news about creatine safety today?"
        )
        self.assertEqual(plan.decision, "live")
        self.assertEqual(plan.selected_route, "trusted_health")
        self.assertEqual(plan.policy_pack, "health")
        self.assertNotIn(
            "general_current_news_scope",
            plan.reason_codes,
        )

    def test_high_stakes_health_uses_indexed_health_pack(self) -> None:
        plan = create_search_plan_v1(
            "Is creatine safe with kidney disease? Cite studies."
        )
        self.assertEqual(plan.decision, "indexed")
        self.assertEqual(plan.selected_route, "trusted_health")
        self.assertEqual(plan.policy_pack, "health")
        self.assertIn("evidence_requested", plan.reason_codes)
        self.assertIn("high_stakes_verification", plan.reason_codes)

    def test_internal_context_does_not_search(self) -> None:
        plan = create_search_plan_v1(
            "What did I say earlier in this conversation?"
        )
        self.assertEqual(plan.decision, "no_search")
        self.assertEqual(plan.selected_route, "normal_chat")
        self.assertEqual(
            plan.reason_codes,
            ("internal_context_sufficient",),
        )


if __name__ == "__main__":
    unittest.main()
