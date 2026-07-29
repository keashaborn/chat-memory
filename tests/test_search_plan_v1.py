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

    def test_regional_biggest_news_uses_current_news_route(self) -> None:
        plan = create_search_plan_v1(
            "What's the biggest news in Japan right now?"
        )
        self.assertEqual(plan.decision, "live")
        self.assertEqual(plan.selected_route, "current_news")
        self.assertEqual(plan.policy_pack, "current_news")
        self.assertIn("freshness_required", plan.reason_codes)
        self.assertIn("general_current_news_scope", plan.reason_codes)

    def test_current_medical_news_uses_trusted_health_route(self) -> None:
        plan = create_search_plan_v1("Any current medical news?")
        self.assertEqual(plan.decision, "live")
        self.assertEqual(plan.selected_route, "trusted_health")
        self.assertEqual(plan.policy_pack, "health")

    def test_vaccine_news_uses_medical_pack_not_general_news(self) -> None:
        plan = create_search_plan_v1(
            "Is there any news about vaccine safety today?"
        )
        self.assertEqual(plan.decision, "live")
        self.assertEqual(plan.selected_route, "trusted_health")
        self.assertEqual(plan.policy_pack, "health")

    def test_named_software_product_news_wins_over_health_word(self) -> None:
        plan = create_search_plan_v1(
            "What is the latest OpenAI Health news?"
        )
        self.assertEqual(plan.decision, "live")
        self.assertEqual(plan.selected_route, "current_news")
        self.assertEqual(plan.policy_pack, "software_security")

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

    def test_official_software_docs_use_trusted_evidence_route(self) -> None:
        plan = create_search_plan_v1(
            "Search the web for the official OpenAI API documentation."
        )
        self.assertEqual(plan.decision, "live")
        self.assertEqual(plan.selected_route, "trusted_health")
        self.assertEqual(plan.policy_pack, "software_security")
        self.assertEqual(plan.reason_codes, ("explicit_web_request",))

    def test_named_official_security_guidance_uses_live_reference_pack(self) -> None:
        plan = create_search_plan_v1(
            "Use official OWASP guidance for this security question."
        )
        self.assertEqual(plan.decision, "live")
        self.assertTrue(plan.external_web_access)
        self.assertEqual(plan.selected_route, "trusted_health")
        self.assertEqual(plan.policy_pack, "software_security")

    def test_exercise_evidence_uses_exercise_pack(self) -> None:
        plan = create_search_plan_v1(
            "What does the evidence say about training frequency?"
        )
        self.assertEqual(plan.decision, "indexed")
        self.assertEqual(plan.selected_route, "trusted_health")
        self.assertEqual(plan.policy_pack, "exercise")

    def test_find_evidence_imperative_uses_exercise_pack(self) -> None:
        plan = create_search_plan_v1(
            "Find evidence about resistance training frequency."
        )
        self.assertEqual(plan.decision, "indexed")
        self.assertEqual(plan.selected_route, "trusted_health")
        self.assertEqual(plan.policy_pack, "exercise")

    def test_behavior_change_evidence_uses_registered_pack(self) -> None:
        plan = create_search_plan_v1(
            "Find evidence about self-monitoring and adherence."
        )
        self.assertEqual(plan.decision, "indexed")
        self.assertEqual(plan.selected_route, "trusted_health")
        self.assertEqual(plan.policy_pack, "behavior_change")

    def test_current_official_docs_use_reference_route_not_news(self) -> None:
        plan = create_search_plan_v1(
            "Check the official Supabase documentation for current RLS guidance."
        )
        self.assertEqual(plan.decision, "live")
        self.assertEqual(plan.selected_route, "trusted_health")
        self.assertEqual(plan.policy_pack, "software_security")
        self.assertEqual(plan.reason_codes, ("evidence_requested",))

    def test_current_software_news_remains_on_news_route(self) -> None:
        plan = create_search_plan_v1(
            "What is the latest Supabase security news?"
        )
        self.assertEqual(plan.decision, "live")
        self.assertEqual(plan.selected_route, "current_news")
        self.assertEqual(plan.policy_pack, "software_security")

    def test_nutrition_evidence_uses_nutrition_pack(self) -> None:
        plan = create_search_plan_v1(
            "Cite evidence about protein intake."
        )
        self.assertEqual(plan.decision, "indexed")
        self.assertEqual(plan.selected_route, "trusted_health")
        self.assertEqual(plan.policy_pack, "nutrition")

    def test_official_nutrition_guidelines_use_live_reference_pack(self) -> None:
        plan = create_search_plan_v1(
            "What do official dietary guidelines recommend for protein?"
        )
        self.assertEqual(plan.decision, "live")
        self.assertTrue(plan.external_web_access)
        self.assertEqual(plan.selected_route, "trusted_health")
        self.assertEqual(plan.policy_pack, "nutrition")

    def test_official_exercise_guidance_uses_live_reference_pack(self) -> None:
        plan = create_search_plan_v1(
            "Search official guidance on resistance training volume."
        )
        self.assertEqual(plan.decision, "live")
        self.assertTrue(plan.external_web_access)
        self.assertEqual(plan.selected_route, "trusted_health")
        self.assertEqual(plan.policy_pack, "exercise")

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
