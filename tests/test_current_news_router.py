from __future__ import annotations

import os
import unittest
from unittest.mock import patch
from uuid import UUID

from fastapi import Response
from pydantic import ValidationError

from seebx.capabilities.search.current_news import (
    CURRENT_NEWS_CITATION_REPAIR_INSTRUCTIONS_V1,
    CurrentNewsRequestV1,
    CurrentNewsResponseV1,
    _current_news_skeleton_answer,
    _current_news_sources_from_trusted_sources,
    _search_current_news_with_exact_page_repair,
    apply_current_news_no_store_headers,
    current_news_fetch_enabled_from_env,
    current_news_provider_settings_from_env,
)
from rag_engine.trusted_web_policy_v1 import (
    TrustedWebDispositionV1,
    TrustedWebTopicV1,
    route_trusted_web_query,
)
from rag_engine.trusted_web_provider_v1 import (
    TrustedWebProviderResultV1,
    TrustedWebProviderSecurityError,
    TrustedWebSourceV1,
)


ACTOR = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")


def provider_result(
    response_id: str,
    url: str,
) -> TrustedWebProviderResultV1:
    source = TrustedWebSourceV1(
        url=url,
        title=url,
        authority_type="official_web",
        evidence_type="web_source",
    )
    return TrustedWebProviderResultV1(
        provider_response_id=response_id,
        answer_text="Verified answer.",
        cited_sources=(source,),
        consulted_sources=(source,),
    )


class FakeProvider:
    def __init__(
        self,
        results: tuple[TrustedWebProviderResultV1, ...],
    ) -> None:
        self.results = list(results)
        self.calls: list[dict[str, object]] = []

    def search(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self.results.pop(0)


class CurrentNewsRouterTests(unittest.TestCase):
    def test_current_news_instructions_forbid_access_disclaimers(self) -> None:
        from seebx.capabilities.search.current_news import (
            CURRENT_NEWS_INSTRUCTIONS_V1,
        )

        self.assertIn("Never claim that you lack access", CURRENT_NEWS_INSTRUCTIONS_V1)
        self.assertIn("current-news sources", CURRENT_NEWS_INSTRUCTIONS_V1)

    def test_status_history_citation_gets_one_exact_incident_repair(self) -> None:
        provider = FakeProvider(
            (
                provider_result(
                    "resp-first",
                    "https://status.openai.com/history",
                ),
                provider_result(
                    "resp-repaired",
                    "https://status.openai.com/incidents/01KXYZEXACT",
                ),
            )
        )
        policy = route_trusted_web_query(
            "What happened with OpenAI today?"
        )

        result, admission, repaired = (
            _search_current_news_with_exact_page_repair(
                provider=provider,
                query="What happened with OpenAI today?",
                policy=policy,
                actor_user_id=str(ACTOR),
                safety_secret="x" * 32,
                response_language="en",
            )
        )

        self.assertTrue(repaired)
        self.assertEqual(result.provider_response_id, "resp-repaired")
        self.assertEqual(admission.exact_page_source_count, 1)
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(provider.calls[0]["max_searches"], 2)
        self.assertEqual(provider.calls[1]["max_searches"], 2)
        self.assertNotIn(
            CURRENT_NEWS_CITATION_REPAIR_INSTRUCTIONS_V1,
            str(provider.calls[0]["instructions"]),
        )
        self.assertIn(
            CURRENT_NEWS_CITATION_REPAIR_INSTRUCTIONS_V1,
            str(provider.calls[1]["instructions"]),
        )

    def test_status_history_repair_exhaustion_fails_closed(self) -> None:
        provider = FakeProvider(
            (
                provider_result(
                    "resp-first",
                    "https://status.openai.com/history",
                ),
                provider_result(
                    "resp-second",
                    "https://status.openai.com/history",
                ),
            )
        )
        policy = route_trusted_web_query(
            "What happened with OpenAI today?"
        )

        with self.assertRaisesRegex(
            TrustedWebProviderSecurityError,
            "citation_evidence_repair_exhausted_generic_index",
        ):
            _search_current_news_with_exact_page_repair(
                provider=provider,
                query="What happened with OpenAI today?",
                policy=policy,
                actor_user_id=str(ACTOR),
                safety_secret="x" * 32,
                response_language="en",
            )

        self.assertEqual(len(provider.calls), 2)

    def test_citation_repair_cannot_exceed_total_search_budget(self) -> None:
        provider = FakeProvider(
            (
                provider_result(
                    "resp-first",
                    "https://status.openai.com/history",
                ),
            )
        )
        policy = route_trusted_web_query(
            "What happened with OpenAI today?"
        )

        with self.assertRaisesRegex(
            TrustedWebProviderSecurityError,
            "citation_evidence_repair_budget_exhausted",
        ):
            _search_current_news_with_exact_page_repair(
                provider=provider,
                query="What happened with OpenAI today?",
                policy=policy,
                actor_user_id=str(ACTOR),
                safety_secret="x" * 32,
                response_language="en",
                max_searches=2,
            )

        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.calls[0]["max_searches"], 2)

    def test_non_generic_policy_failure_is_not_retried(self) -> None:
        result = TrustedWebProviderResultV1(
            provider_response_id="resp-missing",
            answer_text="No citation.",
            cited_sources=(),
            consulted_sources=(),
        )
        provider = FakeProvider((result,))
        policy = route_trusted_web_query(
            "What happened with OpenAI today?"
        )

        with self.assertRaisesRegex(
            TrustedWebProviderSecurityError,
            "citation_evidence_cited_sources_missing",
        ):
            _search_current_news_with_exact_page_repair(
                provider=provider,
                query="What happened with OpenAI today?",
                policy=policy,
                actor_user_id=str(ACTOR),
                safety_secret="x" * 32,
                response_language="en",
            )

        self.assertEqual(len(provider.calls), 1)

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
        self.assertEqual(response.admitted_sources, ())
        self.assertEqual(response.consulted_sources, ())
        self.assertEqual(
            response.source_contract,
            "web_source_provenance_v2",
        )
        self.assertEqual(
            response.admission_contract,
            "web_evidence_admission_v1",
        )
        self.assertEqual(
            response.citation_evidence_contract,
            "citation_evidence_v1",
        )
        self.assertEqual(
            response.citation_freshness_status,
            "not_applicable",
        )


if __name__ == "__main__":
    unittest.main()
