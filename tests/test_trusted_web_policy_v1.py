from __future__ import annotations

import unittest

from rag_engine.trusted_source_registry_v1 import (
    ACSM_DOMAIN,
    APNEWS_DOMAIN,
    ARSTECHNICA_DOMAIN,
    BACB_DOMAIN,
    CLINICAL_TRIALS_DOMAIN,
    COCHRANE_DOMAIN,
    HUGGINGFACE_DOMAIN,
    NHK_DOMAIN,
    NSCA_DOMAIN,
    ODS_DOMAIN,
    OPENAI_DOMAIN,
    PMC_DOMAIN,
    PUBMED_DOMAIN,
    REUTERS_DOMAIN,
)
from rag_engine.trusted_web_policy_v1 import (
    CURRENT_NEWS_ALLOWED_DOMAINS,
    EXERCISE_TRAINING_ALLOWED_DOMAINS,
    GENERAL_CURRENT_NEWS_ALLOWED_DOMAINS,
    MEDICAL_HEALTH_ALLOWED_DOMAINS,
    NUTRITION_FOOD_ALLOWED_DOMAINS,
    SOFTWARE_SECURITY_ALLOWED_DOMAINS,
    TrustedWebDispositionV1,
    TrustedWebTopicV1,
    route_trusted_web_query,
    validate_allowed_source_url,
)


class TrustedWebPolicyV1Tests(unittest.TestCase):

    def test_current_news_routes_only_for_current_event_queries(self) -> None:
        decision = route_trusted_web_query(
            "What just happened with OpenAI and Hugging Face?"
        )
        self.assertEqual(decision.topic, TrustedWebTopicV1.CURRENT_NEWS)
        self.assertEqual(decision.disposition, TrustedWebDispositionV1.SEARCH)
        self.assertEqual(decision.reason, "approved_current_news_lookup")
        self.assertEqual(decision.allowed_domains, CURRENT_NEWS_ALLOWED_DOMAINS)
        self.assertIn(OPENAI_DOMAIN, decision.allowed_domains)
        self.assertIn(HUGGINGFACE_DOMAIN, decision.allowed_domains)
        self.assertIn(APNEWS_DOMAIN, decision.allowed_domains)
        self.assertIn(ARSTECHNICA_DOMAIN, decision.allowed_domains)
        self.assertNotIn(PUBMED_DOMAIN, decision.allowed_domains)

    def test_current_news_requires_current_intent_not_just_entity_name(self) -> None:
        decision = route_trusted_web_query(
            "Explain what Hugging Face is in general."
        )
        self.assertEqual(decision.topic, TrustedWebTopicV1.UNSUPPORTED)
        self.assertEqual(decision.disposition, TrustedWebDispositionV1.DECLINE)

    def test_current_news_routes_natural_freshness_phrase_with_entity(self) -> None:
        decision = route_trusted_web_query(
            "What's going on with OpenAI and Hugging Face?"
        )
        self.assertEqual(decision.topic, TrustedWebTopicV1.CURRENT_NEWS)
        self.assertEqual(decision.disposition, TrustedWebDispositionV1.SEARCH)
        self.assertEqual(decision.reason, "approved_current_news_lookup")

    def test_general_current_news_is_limited_to_wire_services(self) -> None:
        decision = route_trusted_web_query(
            "Is there any news about the US and Iran today?"
        )
        self.assertEqual(decision.topic, TrustedWebTopicV1.CURRENT_NEWS)
        self.assertEqual(decision.disposition, TrustedWebDispositionV1.SEARCH)
        self.assertEqual(
            decision.reason,
            "approved_general_current_news_lookup",
        )
        self.assertEqual(
            decision.allowed_domains,
            GENERAL_CURRENT_NEWS_ALLOWED_DOMAINS,
        )
        self.assertEqual(
            decision.allowed_domains,
            (APNEWS_DOMAIN, REUTERS_DOMAIN, NHK_DOMAIN),
        )

    def test_japan_news_uses_bounded_general_news_pack(self) -> None:
        decision = route_trusted_web_query(
            "What's the biggest news in Japan right now?"
        )
        self.assertEqual(decision.topic, TrustedWebTopicV1.CURRENT_NEWS)
        self.assertEqual(decision.disposition, TrustedWebDispositionV1.SEARCH)
        self.assertEqual(
            decision.reason,
            "approved_general_current_news_lookup",
        )
        self.assertEqual(
            decision.allowed_domains,
            GENERAL_CURRENT_NEWS_ALLOWED_DOMAINS,
        )
        self.assertIn(NHK_DOMAIN, decision.allowed_domains)

    def test_current_medical_news_uses_medical_pack(self) -> None:
        decision = route_trusted_web_query("Any current medical news?")
        self.assertEqual(
            decision.topic,
            TrustedWebTopicV1.MEDICAL_CURRENT_NEWS,
        )
        self.assertEqual(
            decision.allowed_domains,
            MEDICAL_HEALTH_ALLOWED_DOMAINS,
        )
        self.assertIn(CLINICAL_TRIALS_DOMAIN, decision.allowed_domains)
        self.assertIn(COCHRANE_DOMAIN, decision.allowed_domains)

    def test_named_software_product_news_wins_over_health_word(self) -> None:
        decision = route_trusted_web_query(
            "What is the latest OpenAI Health news?"
        )
        self.assertEqual(decision.topic, TrustedWebTopicV1.CURRENT_NEWS)
        self.assertEqual(
            decision.allowed_domains,
            CURRENT_NEWS_ALLOWED_DOMAINS,
        )

    def test_general_current_news_does_not_steal_health_topics(self) -> None:
        decision = route_trusted_web_query(
            "Is there any news about creatine safety today?"
        )
        self.assertEqual(decision.topic, TrustedWebTopicV1.SUPPLEMENTS)
        self.assertEqual(decision.disposition, TrustedWebDispositionV1.SEARCH)
        self.assertNotEqual(
            decision.allowed_domains,
            GENERAL_CURRENT_NEWS_ALLOWED_DOMAINS,
        )

    def test_current_news_rejects_vague_freshness_phrase_without_entity(self) -> None:
        decision = route_trusted_web_query("What's going on?")
        self.assertEqual(decision.topic, TrustedWebTopicV1.UNSUPPORTED)
        self.assertEqual(decision.disposition, TrustedWebDispositionV1.DECLINE)

    def test_user_search_prohibition_fails_closed_inside_provider_policy(self) -> None:
        decision = route_trusted_web_query(
            "Do not search the web. What happened with OpenAI today?"
        )
        self.assertEqual(decision.topic, TrustedWebTopicV1.UNSUPPORTED)
        self.assertEqual(decision.disposition, TrustedWebDispositionV1.DECLINE)
        self.assertEqual(decision.reason, "search_prohibited_by_user")
        self.assertEqual(decision.allowed_domains, ())

    def test_current_news_does_not_steal_health_or_medical_queries(self) -> None:
        supplement = route_trusted_web_query(
            "What does the evidence say about creatine safety?"
        )
        medical = route_trusted_web_query(
            "Should I change my testosterone dose based on recent news?"
        )
        self.assertEqual(supplement.topic, TrustedWebTopicV1.SUPPLEMENTS)
        self.assertNotEqual(medical.topic, TrustedWebTopicV1.CURRENT_NEWS)
        self.assertEqual(medical.disposition, TrustedWebDispositionV1.DECLINE)

    def test_current_news_rejects_social_gossip_sources(self) -> None:
        decision = route_trusted_web_query(
            "Search Reddit and Twitter gossip for the latest OpenAI rumors."
        )
        self.assertEqual(decision.topic, TrustedWebTopicV1.UNSUPPORTED)
        self.assertEqual(decision.disposition, TrustedWebDispositionV1.DECLINE)
        self.assertEqual(decision.reason, "unapproved_news_source")

    def test_supplement_route_uses_only_approved_domains(self) -> None:
        decision = route_trusted_web_query(
            "What does the evidence say about creatine for strength?"
        )
        self.assertEqual(decision.topic, TrustedWebTopicV1.SUPPLEMENTS)
        self.assertEqual(decision.disposition, TrustedWebDispositionV1.SEARCH)
        self.assertIn(ODS_DOMAIN, decision.allowed_domains)
        self.assertIn(PUBMED_DOMAIN, decision.allowed_domains)
        self.assertNotIn(BACB_DOMAIN, decision.allowed_domains)

    def test_training_evidence_uses_exercise_pack(self) -> None:
        decision = route_trusted_web_query(
            "What does evidence say about training volume for hypertrophy?"
        )
        self.assertEqual(decision.topic, TrustedWebTopicV1.TRAINING_EVIDENCE)
        self.assertEqual(
            decision.allowed_domains,
            EXERCISE_TRAINING_ALLOWED_DOMAINS,
        )
        self.assertIn(ACSM_DOMAIN, decision.allowed_domains)
        self.assertIn(NSCA_DOMAIN, decision.allowed_domains)

    def test_nutrition_evidence_uses_nutrition_pack(self) -> None:
        decision = route_trusted_web_query(
            "Cite evidence about protein intake and meal timing."
        )
        self.assertEqual(
            decision.topic,
            TrustedWebTopicV1.NUTRITION_EVIDENCE,
        )
        self.assertEqual(
            decision.allowed_domains,
            NUTRITION_FOOD_ALLOWED_DOMAINS,
        )

    def test_official_nutrition_guidance_uses_live_reference_topic(self) -> None:
        decision = route_trusted_web_query(
            "What do official dietary guidelines recommend for protein?"
        )
        self.assertEqual(
            decision.topic,
            TrustedWebTopicV1.NUTRITION_REFERENCE,
        )
        self.assertEqual(
            decision.allowed_domains,
            NUTRITION_FOOD_ALLOWED_DOMAINS,
        )

    def test_official_exercise_guidance_uses_live_reference_topic(self) -> None:
        decision = route_trusted_web_query(
            "Search official guidance on resistance training volume."
        )
        self.assertEqual(
            decision.topic,
            TrustedWebTopicV1.EXERCISE_REFERENCE,
        )
        self.assertEqual(
            decision.allowed_domains,
            EXERCISE_TRAINING_ALLOWED_DOMAINS,
        )

    def test_software_reference_uses_only_official_reference_pack(self) -> None:
        decision = route_trusted_web_query(
            "Search the official OpenAI API documentation."
        )
        self.assertEqual(
            decision.topic,
            TrustedWebTopicV1.SOFTWARE_SECURITY_REFERENCE,
        )
        self.assertEqual(
            decision.allowed_domains,
            SOFTWARE_SECURITY_ALLOWED_DOMAINS,
        )
        self.assertNotIn(APNEWS_DOMAIN, decision.allowed_domains)
        self.assertNotIn(ARSTECHNICA_DOMAIN, decision.allowed_domains)

    def test_current_official_docs_prefer_reference_pack_over_news(self) -> None:
        decision = route_trusted_web_query(
            "Check the official Supabase documentation for current RLS guidance."
        )
        self.assertEqual(
            decision.topic,
            TrustedWebTopicV1.SOFTWARE_SECURITY_REFERENCE,
        )
        self.assertEqual(
            decision.allowed_domains,
            SOFTWARE_SECURITY_ALLOWED_DOMAINS,
        )
        self.assertNotIn(APNEWS_DOMAIN, decision.allowed_domains)
        self.assertNotIn(ARSTECHNICA_DOMAIN, decision.allowed_domains)

    def test_current_news_wording_is_not_stolen_by_reference_pack(self) -> None:
        for query in (
            "Search the latest OpenAI news.",
            "What is the latest OpenAI research news?",
        ):
            with self.subTest(query=query):
                decision = route_trusted_web_query(query)
                self.assertEqual(
                    decision.topic,
                    TrustedWebTopicV1.CURRENT_NEWS,
                )
                self.assertEqual(
                    decision.allowed_domains,
                    CURRENT_NEWS_ALLOWED_DOMAINS,
                )

    def test_behavior_route_keeps_bacb_off_by_default(self) -> None:
        default = route_trusted_web_query(
            "How can I use a baseline phase for training adherence?"
        )
        enabled = route_trusted_web_query(
            "How can I use a baseline phase for training adherence?",
            allow_bacb=True,
        )
        self.assertNotIn(BACB_DOMAIN, default.allowed_domains)
        self.assertIn(BACB_DOMAIN, enabled.allowed_domains)

    def test_general_trivia_is_declined(self) -> None:
        decision = route_trusted_web_query(
            "Who won the championship last night?"
        )
        self.assertEqual(decision.topic, TrustedWebTopicV1.UNSUPPORTED)
        self.assertEqual(decision.disposition, TrustedWebDispositionV1.DECLINE)
        self.assertEqual(decision.allowed_domains, ())

    def test_food_composition_routes_to_usda_integration(self) -> None:
        decision = route_trusted_web_query(
            "How many calories in this food barcode?"
        )
        self.assertEqual(
            decision.topic,
            TrustedWebTopicV1.USDA_FOOD_COMPOSITION,
        )
        self.assertEqual(
            decision.disposition,
            TrustedWebDispositionV1.ROUTE_INTERNAL,
        )

    def test_safety_signal_stops_search(self) -> None:
        decision = route_trusted_web_query(
            "Optimize my plan even though I have chest pain."
        )
        self.assertEqual(decision.topic, TrustedWebTopicV1.SAFETY_STOP)
        self.assertEqual(
            decision.disposition,
            TrustedWebDispositionV1.SAFETY_STOP,
        )

    def test_query_with_unapproved_url_is_declined(self) -> None:
        decision = route_trusted_web_query(
            "Use https://example.com to answer my creatine question."
        )
        self.assertEqual(decision.reason, "unapproved_url_target")
        self.assertEqual(decision.disposition, TrustedWebDispositionV1.DECLINE)

    def test_source_validation_rejects_ssrf_and_non_allowlist_targets(self) -> None:
        allowed = (PUBMED_DOMAIN,)
        with self.assertRaisesRegex(ValueError, "ip_literal"):
            validate_allowed_source_url("https://127.0.0.1/a", allowed)
        with self.assertRaisesRegex(ValueError, "scheme"):
            validate_allowed_source_url(
                "http://pubmed.ncbi.nlm.nih.gov/a",
                allowed,
            )
        with self.assertRaisesRegex(ValueError, "not_allowed"):
            validate_allowed_source_url("https://example.com/a", allowed)

    def test_source_validation_accepts_allowlisted_subdomain_and_drops_fragment(self) -> None:
        result = validate_allowed_source_url(
            "https://sub.pubmed.ncbi.nlm.nih.gov/a?q=1#fragment",
            (PUBMED_DOMAIN,),
        )
        self.assertEqual(
            result,
            "https://sub.pubmed.ncbi.nlm.nih.gov/a?q=1",
        )


if __name__ == "__main__":
    unittest.main()
