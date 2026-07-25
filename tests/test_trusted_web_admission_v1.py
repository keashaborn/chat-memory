from __future__ import annotations

import unittest

from rag_engine.trusted_web_admission_v1 import (
    admit_trusted_web_sources_v1,
)
from rag_engine.trusted_web_provider_v1 import (
    TrustedWebProviderSecurityError,
    TrustedWebSourceV1,
)


def source(url: str) -> TrustedWebSourceV1:
    return TrustedWebSourceV1(
        url=url,
        title=url,
        authority_type="official_web",
        evidence_type="web_source",
    )


class TrustedWebAdmissionV1Tests(unittest.TestCase):
    def test_current_news_preserves_cited_and_rejects_noisy_sources(self) -> None:
        cited = (
            source("https://status.openai.com/incidents/cited"),
            source("https://openai.com/index/cited-article"),
        )
        consulted = cited + (
            source("https://openai.com/news"),
            source("https://status.openai.com/incidents/uncited"),
            source("https://cdn.openai.com/report.pdf"),
            source("https://arstechnica.com/ai/2026/07/relevant-article"),
        )

        result = admit_trusted_web_sources_v1(
            cited_sources=cited,
            consulted_sources=consulted,
            max_sources=10,
            policy_pack="current_news",
        )

        self.assertEqual(
            [item.url for item in result.admitted_sources],
            [
                "https://status.openai.com/incidents/cited",
                "https://openai.com/index/cited-article",
                "https://arstechnica.com/ai/2026/07/relevant-article",
            ],
        )
        self.assertEqual(
            dict(result.rejected_source_reasons),
            {
                "https://openai.com/news": "generic_index",
                "https://status.openai.com/incidents/uncited": "uncited_incident",
                "https://cdn.openai.com/report.pdf": "uncited_document",
            },
        )

    def test_admission_caps_sources_after_preserving_citations(self) -> None:
        cited = (source("https://openai.com/index/cited"),)
        consulted = cited + tuple(
            source(f"https://apnews.com/article/{index}") for index in range(5)
        )
        result = admit_trusted_web_sources_v1(
            cited_sources=cited,
            consulted_sources=consulted,
            max_sources=3,
            policy_pack="current_news",
        )
        self.assertEqual(len(result.admitted_sources), 3)
        self.assertEqual(result.rejected_source_count, 3)
        self.assertTrue(
            all(
                reason == "budget_exceeded"
                for _, reason in result.rejected_source_reasons
            )
        )

    def test_trusted_health_uses_cited_first_budget_without_news_filters(
        self,
    ) -> None:
        cited = (source("https://pubmed.ncbi.nlm.nih.gov/123"),)
        consulted = cited + (
            source("https://ods.od.nih.gov/factsheets/example"),
            source("https://pubmed.ncbi.nlm.nih.gov/456"),
        )
        result = admit_trusted_web_sources_v1(
            cited_sources=cited,
            consulted_sources=consulted,
            max_sources=2,
            policy_pack="trusted_health",
        )
        self.assertEqual(len(result.admitted_sources), 2)
        self.assertEqual(
            result.rejected_source_reasons,
            (("https://pubmed.ncbi.nlm.nih.gov/456", "budget_exceeded"),),
        )

    def test_unconsulted_citation_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            TrustedWebProviderSecurityError,
            "trusted_web_cited_source_not_consulted",
        ):
            admit_trusted_web_sources_v1(
                cited_sources=(source("https://openai.com/index/cited"),),
                consulted_sources=(),
                max_sources=10,
                policy_pack="current_news",
            )

    def test_citations_over_budget_fail_closed(self) -> None:
        cited = tuple(
            source(f"https://apnews.com/article/{index}") for index in range(3)
        )
        with self.assertRaisesRegex(
            TrustedWebProviderSecurityError,
            "trusted_web_cited_sources_exceed_budget",
        ):
            admit_trusted_web_sources_v1(
                cited_sources=cited,
                consulted_sources=cited,
                max_sources=2,
                policy_pack="current_news",
            )


if __name__ == "__main__":
    unittest.main()
