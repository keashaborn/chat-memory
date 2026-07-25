from __future__ import annotations

import os
import unittest
from unittest.mock import patch
from uuid import UUID

from fastapi import Response
from pydantic import ValidationError

from rag_engine.current_news_router import (
    CurrentNewsRequestV1,
    CurrentNewsResponseV1,
    _current_news_skeleton_answer,
    _current_news_sources_from_trusted_sources,
    apply_current_news_no_store_headers,
    current_news_fetch_enabled_from_env,
    current_news_provider_settings_from_env,
)
from rag_engine.trusted_web_policy_v1 import (
    TrustedWebDispositionV1,
    TrustedWebTopicV1,
    route_trusted_web_query,
)
from rag_engine.trusted_web_provider_v1 import TrustedWebSourceV1


ACTOR = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")


class CurrentNewsRouterTests(unittest.TestCase):
    def test_public_contract_rejects_client_policy_controls(self) -> None:
        with self.assertRaises(ValidationError):
            CurrentNewsRequestV1.model_validate(
                {
                    "user_id": ACTOR,
                    "query": "What just happened with OpenAI?",
                    "allowed_domains": ["example.com"],
                    "model": "gpt-5.6",
                    "topic": "current_news",
                    "external_web_access": True,
                }
            )

    def test_public_contract_accepts_only_actor_and_query(self) -> None:
        request = CurrentNewsRequestV1.model_validate_json(
            '{"user_id":"1240822d-ac9a-4096-95aa-e2b24d36ef50",'
            '"query":"  What just   happened with OpenAI?  "}'
        )
        self.assertEqual(request.user_id, ACTOR)
        self.assertEqual(request.query, "What just happened with OpenAI?")

    def test_no_store_headers_are_mandatory(self) -> None:
        response = Response()
        apply_current_news_no_store_headers(response)
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")


    def test_current_news_vague_prompt_gets_actionable_guidance(self) -> None:
        decision = route_trusted_web_query("Can you check the news yourself?")
        self.assertEqual(decision.topic, TrustedWebTopicV1.UNSUPPORTED)
        answer = _current_news_skeleton_answer(decision.topic)
        self.assertIn("needs a specific topic", answer)
        self.assertIn("OpenAI and Hugging Face", answer)
        self.assertNotIn("outside current-news scope", answer)


    def test_current_news_fetch_flag_defaults_off(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(current_news_fetch_enabled_from_env())

    def test_current_news_fetch_flag_and_external_access_are_server_owned(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CURRENT_NEWS_FETCH_ENABLED": "true",
                "TRUSTED_WEB_EXTERNAL_WEB_ACCESS": "false",
                "CURRENT_NEWS_EXTERNAL_WEB_ACCESS": "true",
            },
            clear=True,
        ):
            self.assertTrue(current_news_fetch_enabled_from_env())
            settings = current_news_provider_settings_from_env()
            self.assertTrue(settings.enabled)
            self.assertTrue(settings.external_web_access)

    def test_current_news_sources_are_news_card_shaped(self) -> None:
        sources = _current_news_sources_from_trusted_sources(
            (
                TrustedWebSourceV1(
                    url="https://openai.com/news/example",
                    title="OpenAI update",
                    authority_type="official_web",
                    evidence_type="web_source",
                ),
                TrustedWebSourceV1(
                    url="https://www.reuters.com/technology/example",
                    title="Reuters report",
                    authority_type="official_web",
                    evidence_type="web_source",
                ),
            )
        )
        self.assertEqual(sources[0].publisher, "OpenAI")
        self.assertEqual(sources[0].source_type, "official_source")
        self.assertEqual(sources[1].publisher, "Reuters")
        self.assertEqual(sources[1].source_type, "news_source")

    def test_current_news_sources_are_deduped_without_losing_provenance(
        self,
    ) -> None:
        raw_sources = tuple(
            TrustedWebSourceV1(
                url=url,
                title="Source",
                authority_type="official_web",
                evidence_type="web_source",
            )
            for url in (
                "https://openai.com/index/hugging-face-model-evaluation-security-incident/?utm_source=openai",
                "https://openai.com/sk-SK/index/hugging-face-model-evaluation-security-incident/",
                "https://apnews.com/article/abc?utm_source=openai",
                "https://apnews.com/article/abc",
                "https://apnews.com/article/1",
                "https://apnews.com/article/2",
                "https://apnews.com/article/3",
                "https://apnews.com/article/4",
                "https://apnews.com/article/5",
                "https://apnews.com/article/6",
                "https://apnews.com/article/7",
            )
        )
        sources = _current_news_sources_from_trusted_sources(raw_sources)
        self.assertEqual(len(sources), 9)
        self.assertEqual(
            sources[0].url,
            "https://openai.com/index/hugging-face-model-evaluation-security-incident",
        )
        self.assertEqual(sources[0].title, "OpenAI")
        self.assertEqual(
            [source.url for source in sources].count("https://apnews.com/article/abc"),
            1,
        )

    def test_current_news_policy_routes_but_fetch_remains_disabled(self) -> None:
        decision = route_trusted_web_query(
            "What just happened with OpenAI and Hugging Face?"
        )
        response = CurrentNewsResponseV1(
            search_id=UUID("00000000-0000-0000-0000-000000000001"),
            policy_version=decision.policy_version,
            topic=decision.topic,
            disposition=TrustedWebDispositionV1.DECLINE,
            reason="current_news_fetch_not_enabled",
            searched=False,
            answer="Current news lookup is routed and policy-approved, but live news retrieval is not enabled yet.",
            sources=(),
        )
        self.assertEqual(response.topic, TrustedWebTopicV1.CURRENT_NEWS)
        self.assertFalse(response.searched)
        self.assertEqual(response.disposition, TrustedWebDispositionV1.DECLINE)
        self.assertEqual(response.sources, ())
        self.assertEqual(response.cited_sources, ())
        self.assertEqual(response.consulted_sources, ())
        self.assertEqual(
            response.source_contract,
            "web_source_provenance_v2",
        )


if __name__ == "__main__":
    unittest.main()
