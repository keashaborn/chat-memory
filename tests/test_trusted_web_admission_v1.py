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
    def test_current_news_qualifies_cited_and_rejects_noisy_sources(self) -> None:
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
        self.assertEqual(
            result.citation_evidence_contract,
            "citation_evidence_v1",
        )
        self.assertEqual(result.exact_page_source_count, 2)
        self.assertEqual(result.freshness_status, "unverified")

    def test_generic_cited_current_news_page_fails_closed(self) -> None:
        cited = (source("https://openai.com/news?author=someone"),)
        with self.assertRaisesRegex(
            TrustedWebProviderSecurityError,
            "citation_evidence_cited_generic_index",
        ):
            admit_trusted_web_sources_v1(
                cited_sources=cited,
                consulted_sources=cited,
                max_sources=10,
                policy_pack="current_news",
            )

    def test_status_history_is_a_generic_current_news_index(self) -> None:
        cited = (
            source(
                "https://status.openai.com/history?utm_source=openai"
            ),
        )
        with self.assertRaisesRegex(
            TrustedWebProviderSecurityError,
            "citation_evidence_cited_generic_index",
        ):
            admit_trusted_web_sources_v1(
                cited_sources=cited,
                consulted_sources=cited,
                max_sources=10,
                policy_pack="current_news",
            )

    def test_generic_cited_health_page_fails_closed(self) -> None:
        cited = (source("https://www.who.int/news-room/headlines"),)
        with self.assertRaisesRegex(
            TrustedWebProviderSecurityError,
            "citation_evidence_cited_generic_index",
        ):
            admit_trusted_web_sources_v1(
                cited_sources=cited,
                consulted_sources=cited,
                max_sources=5,
                policy_pack="trusted_health",
            )

    def test_current_reference_ranks_ahead_of_archived_guidance(self) -> None:
        cited = (
            source(
                "https://dietaryguidelines.gov/sites/default/files/2020-12/"
                "Dietary_Guidelines_for_Americans_2020-2025.pdf"
            ),
            source(
                "https://odphp.health.gov/our-work/nutrition-physical-activity/"
                "dietary-guidelines/current-dietary-guidelines"
            ),
        )
        result = admit_trusted_web_sources_v1(
            cited_sources=cited,
            consulted_sources=cited,
            max_sources=5,
            policy_pack="trusted_health",
        )
        self.assertEqual(
            result.validated_cited_sources[0].freshness_status,
            "current_reference",
        )
        self.assertEqual(
            result.validated_cited_sources[1].freshness_status,
            "historical",
        )
        self.assertEqual(result.archived_source_count, 1)
        self.assertEqual(result.freshness_status, "mixed")

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
