from __future__ import annotations

import json
import unittest

from rag_engine.trusted_web_ncbi_v1 import NCBIResearchRecordV1
from rag_engine.trusted_web_policy_v1 import route_trusted_web_query
from rag_engine.trusted_web_provider_v1 import (
    OpenAITrustedWebProviderV1,
    TrustedWebProviderError,
    TrustedWebProviderSecurityError,
    TrustedWebSettingsV1,
)


ACTOR = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
SECRET = "test-service-token-with-at-least-32-bytes"


class FakeResponse:
    def __init__(
        self,
        source_url: str,
        answer_text: str | None = None,
        *,
        source_title: str = "Creatine evidence review",
        citation_url: str | None = None,
        citation_title: str = "Creatine evidence review",
        additional_sources: tuple[dict[str, str], ...] = (),
        include_citation: bool = True,
    ):
        self.output_text = answer_text or (
            "Creatine improves some high-intensity performance outcomes."
        )
        annotation_url = citation_url or source_url
        self._payload = {
            "id": "resp_test",
            "output": [
                {
                    "type": "web_search_call",
                    "action": {
                        "type": "search",
                        "sources": [
                            {
                                "url": source_url,
                                "title": source_title,
                            },
                            *additional_sources,
                        ],
                    },
                },
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": self.output_text,
                            "annotations": (
                                [
                                    {
                                        "type": "url_citation",
                                        "url": annotation_url,
                                        "title": citation_title,
                                        "start_index": 0,
                                        "end_index": len(self.output_text),
                                    }
                                ]
                                if include_citation
                                else []
                            ),
                        }
                    ],
                },
            ],
        }

    def model_dump(self, mode: str):
        self._payload["output_text"] = self.output_text
        return self._payload


class TextOnlyFakeResponse:
    def __init__(
        self,
        output_text: str = "Creatine evidence is mixed but generally favorable for resistance-training performance [PMID:123].",
    ):
        self.output_text = output_text

    def model_dump(self, mode: str):
        return {"id": "resp_pubmed_test", "output_text": self.output_text, "output": []}


class FakeResponses:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


class FakeClient:
    def __init__(self, response: FakeResponse):
        self.responses = FakeResponses(response)


class TrustedWebProviderV1Tests(unittest.TestCase):
    def settings(self) -> TrustedWebSettingsV1:
        return TrustedWebSettingsV1(
            enabled=True,
            model="gpt-5.5",
            external_web_access=False,
        )

    def test_instructions_forbid_source_access_disclaimers(self) -> None:
        from rag_engine.trusted_web_provider_v1 import TRUSTED_WEB_INSTRUCTIONS_V1

        self.assertIn("Never claim that you lack access", TRUSTED_WEB_INSTRUCTIONS_V1)
        self.assertIn("PubMed", TRUSTED_WEB_INSTRUCTIONS_V1)
        self.assertIn("ODS", TRUSTED_WEB_INSTRUCTIONS_V1)

    def test_provider_request_is_bounded_and_stateless(self) -> None:
        client = FakeClient(
            FakeResponse(
                "https://pubmed.ncbi.nlm.nih.gov/12345678/"
            )
        )
        policy = route_trusted_web_query("Does creatine improve strength?")
        result = OpenAITrustedWebProviderV1(
            client,
            self.settings(),
        ).search(
            query="Does creatine improve strength?",
            policy=policy,
            actor_user_id=ACTOR,
            safety_secret=SECRET,
        )
        kwargs = client.responses.kwargs
        self.assertEqual(kwargs["tool_choice"], "required")
        self.assertFalse(kwargs["store"])
        self.assertFalse(kwargs["parallel_tool_calls"])
        self.assertEqual(kwargs["max_tool_calls"], 2)
        self.assertEqual(kwargs["max_output_tokens"], 1_200)
        self.assertEqual(kwargs["reasoning"], {"effort": "low"})
        self.assertEqual(
            kwargs["include"],
            ["web_search_call.action.sources"],
        )
        self.assertEqual(
            kwargs["tools"][0]["filters"]["allowed_domains"],
            list(policy.allowed_domains),
        )
        self.assertFalse(kwargs["tools"][0]["external_web_access"])
        self.assertEqual(len(kwargs["safety_identifier"]), 64)
        self.assertNotEqual(kwargs["safety_identifier"], ACTOR)
        self.assertNotIn(ACTOR, json.dumps(kwargs, sort_keys=True))
        self.assertEqual(len(result.cited_sources), 1)
        self.assertEqual(len(result.consulted_sources), 1)
        self.assertIn("Sources:", result.answer_markdown())

    def test_provider_tool_calls_are_bound_by_server_budget(self) -> None:
        client = FakeClient(
            FakeResponse("https://pubmed.ncbi.nlm.nih.gov/12345678/")
        )
        policy = route_trusted_web_query("Does creatine improve strength?")
        OpenAITrustedWebProviderV1(client, self.settings()).search(
            query="Does creatine improve strength?",
            policy=policy,
            actor_user_id=ACTOR,
            safety_secret=SECRET,
            max_searches=1,
        )
        self.assertEqual(client.responses.kwargs["max_tool_calls"], 1)

    def test_provider_rejects_zero_search_budget(self) -> None:
        client = FakeClient(
            FakeResponse("https://pubmed.ncbi.nlm.nih.gov/12345678/")
        )
        policy = route_trusted_web_query("Does creatine improve strength?")
        with self.assertRaisesRegex(
            TrustedWebProviderError,
            "trusted_web_search_budget_invalid",
        ):
            OpenAITrustedWebProviderV1(client, self.settings()).search(
                query="Does creatine improve strength?",
                policy=policy,
                actor_user_id=ACTOR,
                safety_secret=SECRET,
                max_searches=0,
            )

    def test_provider_separates_cited_from_consulted_and_prefers_citation_title(
        self,
    ) -> None:
        client = FakeClient(
            FakeResponse(
                "https://openai.com/news/?utm_source=openai",
                source_title="OpenAI",
                citation_url="https://openai.com/news/",
                citation_title="OpenAI Newsroom",
                additional_sources=(
                    {
                        "url": "https://status.openai.com/",
                        "title": "OpenAI Status",
                    },
                ),
            )
        )
        policy = route_trusted_web_query("What just happened with OpenAI?")
        result = OpenAITrustedWebProviderV1(
            client,
            self.settings(),
        ).search(
            query="What just happened with OpenAI?",
            policy=policy,
            actor_user_id=ACTOR,
            safety_secret=SECRET,
        )
        self.assertEqual(len(result.cited_sources), 1)
        self.assertEqual(len(result.consulted_sources), 2)
        self.assertEqual(result.cited_sources[0].title, "OpenAI Newsroom")
        self.assertEqual(
            result.consulted_sources[0].title,
            "OpenAI Newsroom",
        )
        self.assertNotIn("utm_source", result.consulted_sources[0].url)

    def test_provider_strips_marketing_parameters_from_canonical_url(
        self,
    ) -> None:
        url = (
            "https://dietaryguidelines.gov/report.pdf"
            "?aff_id=G001&hsa_acc=123&utm_source=openai&section=protein"
        )
        client = FakeClient(FakeResponse(url))
        policy = route_trusted_web_query(
            "What do current dietary guidelines say about protein?"
        )
        result = OpenAITrustedWebProviderV1(
            client,
            self.settings(),
        ).search(
            query="What do current dietary guidelines say about protein?",
            policy=policy,
            actor_user_id=ACTOR,
            safety_secret=SECRET,
        )
        self.assertEqual(
            result.cited_sources[0].url,
            "https://dietaryguidelines.gov/report.pdf?section=protein",
        )

    def test_provider_fails_closed_without_citation_annotations(self) -> None:
        client = FakeClient(
            FakeResponse(
                "https://pubmed.ncbi.nlm.nih.gov/12345678/",
                include_citation=False,
            )
        )
        policy = route_trusted_web_query("Does creatine improve strength?")
        with self.assertRaisesRegex(
            TrustedWebProviderSecurityError,
            "trusted_web_no_cited_sources",
        ):
            OpenAITrustedWebProviderV1(
                client,
                self.settings(),
            ).search(
                query="Does creatine improve strength?",
                policy=policy,
                actor_user_id=ACTOR,
                safety_secret=SECRET,
            )


    def test_provider_fails_closed_when_sourced_answer_denies_access(self) -> None:
        client = FakeClient(
            FakeResponse(
                "https://openai.com/news/example",
                "I don't have access to current news, but OpenAI announced an update.",
            )
        )
        policy = route_trusted_web_query("What is the current news about OpenAI?")
        with self.assertRaisesRegex(
            TrustedWebProviderSecurityError,
            "trusted_web_answer_denies_source_access",
        ):
            OpenAITrustedWebProviderV1(
                client,
                self.settings(),
            ).search(
                query="What is the current news about OpenAI?",
                policy=policy,
                actor_user_id=ACTOR,
                safety_secret=SECRET,
            )


    def test_provider_accepts_custom_instructions_for_current_news(self) -> None:
        client = FakeClient(FakeResponse("https://openai.com/news/example"))
        policy = route_trusted_web_query("What just happened with OpenAI?")
        OpenAITrustedWebProviderV1(
            client,
            self.settings(),
        ).search(
            query="What just happened with OpenAI?",
            policy=policy,
            actor_user_id=ACTOR,
            safety_secret=SECRET,
            instructions="current-news-test-instructions",
        )
        self.assertEqual(
            client.responses.kwargs["instructions"],
            "current-news-test-instructions",
        )

    def test_provider_fails_closed_on_non_allowlisted_source(self) -> None:
        client = FakeClient(FakeResponse("https://example.com/research"))
        policy = route_trusted_web_query("Does creatine improve strength?")
        with self.assertRaisesRegex(
            TrustedWebProviderSecurityError,
            "source_domain_not_allowed",
        ):
            OpenAITrustedWebProviderV1(
                client,
                self.settings(),
            ).search(
                query="Does creatine improve strength?",
                policy=policy,
                actor_user_id=ACTOR,
                safety_secret=SECRET,
            )

    def test_provider_fails_closed_on_untrusted_answer_link(self) -> None:
        client = FakeClient(
            FakeResponse(
                "https://pubmed.ncbi.nlm.nih.gov/12345678/",
                "Read [this](https://example.com/injected) for details.",
            )
        )
        policy = route_trusted_web_query("Does creatine improve strength?")
        with self.assertRaisesRegex(
            TrustedWebProviderSecurityError,
            "trusted_web_answer_link_not_allowed",
        ):
            OpenAITrustedWebProviderV1(
                client,
                self.settings(),
            ).search(
                query="Does creatine improve strength?",
                policy=policy,
                actor_user_id=ACTOR,
                safety_secret=SECRET,
            )

    def test_provider_allows_answer_link_on_policy_domain(self) -> None:
        client = FakeClient(
            FakeResponse(
                "https://pubmed.ncbi.nlm.nih.gov/12345678/",
                "See [the study](https://pubmed.ncbi.nlm.nih.gov/12345678/).",
            )
        )
        policy = route_trusted_web_query("Does creatine improve strength?")
        result = OpenAITrustedWebProviderV1(
            client,
            self.settings(),
        ).search(
            query="Does creatine improve strength?",
            policy=policy,
            actor_user_id=ACTOR,
            safety_secret=SECRET,
        )
        self.assertIn("pubmed.ncbi.nlm.nih.gov", result.answer_text)

    def test_provider_fails_closed_on_allowlisted_but_uncited_answer_link(
        self,
    ) -> None:
        client = FakeClient(
            FakeResponse(
                "https://pubmed.ncbi.nlm.nih.gov/12345678/",
                "See [another study](https://pubmed.ncbi.nlm.nih.gov/99999999/).",
            )
        )
        policy = route_trusted_web_query("Does creatine improve strength?")
        with self.assertRaisesRegex(
            TrustedWebProviderSecurityError,
            "trusted_web_answer_link_not_cited",
        ):
            OpenAITrustedWebProviderV1(
                client,
                self.settings(),
            ).search(
                query="Does creatine improve strength?",
                policy=policy,
                actor_user_id=ACTOR,
                safety_secret=SECRET,
            )


    def test_pubmed_synthesis_fails_closed_when_answer_denies_sources(self) -> None:
        client = FakeClient(
            TextOnlyFakeResponse(
                "I cannot access sources, but creatine may help strength [PMID:123]."
            )
        )
        record = NCBIResearchRecordV1(
            pmid="123",
            title="Creatine and resistance training review",
            journal="Sports Medicine",
            publication_date="2025",
            publication_types=("Systematic Review",),
            abstract="Human review abstract.",
        )
        with self.assertRaisesRegex(
            TrustedWebProviderSecurityError,
            "trusted_web_answer_denies_source_access",
        ):
            OpenAITrustedWebProviderV1(
                client,
                self.settings(),
            ).synthesize_from_pubmed_records(
                query="Does creatine help strength?",
                records=(record,),
                actor_user_id=ACTOR,
                safety_secret=SECRET,
            )


    def test_pubmed_synthesis_does_not_use_web_search_and_requires_inline_pmid(self) -> None:
        client = FakeClient(TextOnlyFakeResponse())
        record = NCBIResearchRecordV1(
            pmid="123",
            title="Creatine and resistance training review",
            journal="Sports Medicine",
            publication_date="2025",
            publication_types=("Systematic Review",),
            abstract="Human review abstract.",
        )
        result = OpenAITrustedWebProviderV1(
            client,
            self.settings(),
        ).synthesize_from_pubmed_records(
            query="Does creatine help strength?",
            records=(record,),
            actor_user_id=ACTOR,
            safety_secret=SECRET,
        )
        kwargs = client.responses.kwargs
        self.assertNotIn("tools", kwargs)
        self.assertFalse(kwargs["store"])
        self.assertIn("Use only the supplied ODS and PubMed records", kwargs["instructions"])
        self.assertEqual(
            result.cited_sources[0].authority_type,
            "pubmed_research",
        )
        self.assertEqual(
            result.cited_sources[0].evidence_type,
            "systematic_review",
        )
        self.assertEqual(result.cited_sources[0].source_id, "PMID:123")
        self.assertEqual(result.consulted_sources, result.cited_sources)


if __name__ == "__main__":
    unittest.main()
