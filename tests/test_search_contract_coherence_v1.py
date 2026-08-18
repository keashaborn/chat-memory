from __future__ import annotations

import unittest

from seebx.capabilities.search.plan import create_search_plan_v1
from rag_engine.trusted_web_policy_v1 import (
    TrustedWebDispositionV1,
    TrustedWebTopicV1,
    route_trusted_web_query,
)


class SearchContractCoherenceV1Tests(unittest.TestCase):
    def test_supported_queries_reach_searchable_policy_topics(self) -> None:
        cases = (
            (
                "What happened with OpenAI today?",
                "current_news",
                "software_security",
                TrustedWebTopicV1.CURRENT_NEWS,
            ),
            (
                "What is the biggest news in Japan right now?",
                "current_news",
                "current_news",
                TrustedWebTopicV1.CURRENT_NEWS,
            ),
            (
                "Any current medical news?",
                "trusted_health",
                "health",
                TrustedWebTopicV1.MEDICAL_CURRENT_NEWS,
            ),
            (
                "What is the official guidance for protein intake?",
                "trusted_health",
                "nutrition",
                TrustedWebTopicV1.NUTRITION_REFERENCE,
            ),
            (
                "Find evidence about resistance training frequency.",
                "trusted_health",
                "exercise",
                TrustedWebTopicV1.TRAINING_EVIDENCE,
            ),
            (
                "Find evidence about self-monitoring and adherence.",
                "trusted_health",
                "behavior_change",
                TrustedWebTopicV1.BEHAVIOR_CHANGE,
            ),
            (
                "Check the official Supabase documentation for current RLS guidance.",
                "trusted_health",
                "software_security",
                TrustedWebTopicV1.SOFTWARE_SECURITY_REFERENCE,
            ),
        )

        for query, expected_route, expected_pack, expected_topic in cases:
            with self.subTest(query=query):
                plan = create_search_plan_v1(query)
                policy = route_trusted_web_query(query)
                self.assertEqual(plan.selected_route, expected_route)
                self.assertEqual(plan.policy_pack, expected_pack)
                self.assertEqual(
                    policy.disposition,
                    TrustedWebDispositionV1.SEARCH,
                )
                self.assertEqual(policy.topic, expected_topic)
                self.assertGreater(len(policy.allowed_domains), 0)


if __name__ == "__main__":
    unittest.main()
