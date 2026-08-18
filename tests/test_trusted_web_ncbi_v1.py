from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET

from seebx.capabilities.search.ncbi import (
    NCBIClientError,
    NCBIPubMedClientV1,
    NCBIResearchRecordV1,
    _build_terms,
    _normalize_pubmed_query,
    classify_publication_types,
    format_ncbi_records_for_model,
    trusted_web_topic_uses_ncbi,
)
from seebx.capabilities.search.policy import TrustedWebTopicV1


class FakeNCBIClient(NCBIPubMedClientV1):
    def _request_xml(self, endpoint: str, params: dict[str, str]) -> ET.Element:
        if endpoint == "esearch.fcgi":
            return ET.fromstring(
                "<eSearchResult><IdList><Id>123</Id><Id>456</Id></IdList></eSearchResult>"
            )
        return ET.fromstring(
            """
            <PubmedArticleSet>
              <PubmedArticle>
                <MedlineCitation>
                  <PMID>123</PMID>
                  <Article>
                    <ArticleTitle>Creatine and resistance training review</ArticleTitle>
                    <Journal><Title>Sports Medicine</Title><JournalIssue><PubDate><Year>2025</Year></PubDate></JournalIssue></Journal>
                    <Abstract><AbstractText>Human review abstract.</AbstractText></Abstract>
                    <PublicationTypeList><PublicationType>Systematic Review</PublicationType></PublicationTypeList>
                  </Article>
                </MedlineCitation>
              </PubmedArticle>
            </PubmedArticleSet>
            """
        )


class FallbackNCBIClient(FakeNCBIClient):
    terms: list[str] = []

    def _request_xml(self, endpoint: str, params: dict[str, str]) -> ET.Element:
        if endpoint != "esearch.fcgi":
            return super()._request_xml(endpoint, params)
        self.terms.append(params["term"])
        if "[Publication Type]" in params["term"]:
            return ET.fromstring("<eSearchResult><IdList /></eSearchResult>")
        return ET.fromstring(
            "<eSearchResult><IdList><Id>123</Id></IdList></eSearchResult>"
        )


class ConceptFilteringNCBIClient(NCBIPubMedClientV1):
    def _esearch(self, query: str) -> tuple[str, ...]:
        return ("101", "202")

    def _efetch(
        self,
        ids: tuple[str, ...],
    ) -> tuple[NCBIResearchRecordV1, ...]:
        return (
            NCBIResearchRecordV1(
                pmid="101",
                title="Resistance training frequency and muscular strength",
                abstract=(
                    "This review evaluates resistance training frequency "
                    "in healthy adults."
                ),
                publication_types=("Systematic Review",),
            ),
            NCBIResearchRecordV1(
                pmid="202",
                title="Antimicrobial stewardship",
                abstract=(
                    "This review discusses antimicrobial resistance and "
                    "clinical training programs."
                ),
                publication_types=("Review",),
            ),
        )


class TrustedWebNCBIV1Tests(unittest.TestCase):
    def test_research_topics_use_ncbi(self) -> None:
        self.assertTrue(trusted_web_topic_uses_ncbi(TrustedWebTopicV1.TRAINING_EVIDENCE))
        self.assertFalse(trusted_web_topic_uses_ncbi(TrustedWebTopicV1.MEDICAL_ADJACENT))

    def test_publication_type_classification_prefers_review_quality(self) -> None:
        self.assertEqual(
            classify_publication_types(("Systematic Review",)),
            "systematic_review",
        )
        self.assertEqual(
            classify_publication_types(("Randomized Controlled Trial",)),
            "randomized_controlled_trial",
        )

    def test_ncbi_records_are_formatted_as_bounded_model_context(self) -> None:
        records = FakeNCBIClient().search("creatine strength")
        self.assertEqual(records[0].source_id, "PMID:123")
        context = format_ncbi_records_for_model("creatine strength", records)
        self.assertIn("Use only the PubMed records below", context)
        self.assertIn("[PMID:123]", context)
        self.assertIn("Systematic Review", context)

    def test_natural_language_health_query_removes_pubmed_noise(self) -> None:
        query = (
            "Is creatine safe for someone with kidney disease? "
            "Cite current evidence."
        )
        self.assertEqual(
            _normalize_pubmed_query(query),
            (
                "creatine creatine supplementation safety adverse effects "
                "kidney disease renal function"
            ),
        )

    def test_pubmed_search_relaxes_publication_type_but_keeps_human_filter(
        self,
    ) -> None:
        query = "creatine safety in kidney disease"
        terms = _build_terms(query)
        self.assertEqual(len(terms), 2)
        self.assertIn("[Publication Type]", terms[0])
        self.assertNotIn("[Publication Type]", terms[1])
        self.assertTrue(all("humans[MeSH Terms]" in term for term in terms))

        client = FallbackNCBIClient()
        client.terms.clear()
        records = client.search(query)
        self.assertEqual(records[0].pmid, "123")
        self.assertEqual(client.terms, list(terms))

    def test_resistance_training_query_requires_title_abstract_concepts(
        self,
    ) -> None:
        terms = _build_terms(
            "Find evidence about resistance training frequency."
        )
        self.assertTrue(
            all(
                '"resistance training"[Title/Abstract]' in term
                for term in terms
            )
        )
        self.assertTrue(
            all("frequency[Title/Abstract]" in term for term in terms)
        )
        self.assertTrue(all("find" not in term.lower() for term in terms))

        records = ConceptFilteringNCBIClient().search(
            "Find evidence about resistance training frequency."
        )
        self.assertEqual([record.pmid for record in records], ["101"])

    def test_behavior_query_uses_self_monitoring_and_adherence_concepts(
        self,
    ) -> None:
        terms = _build_terms(
            "Find evidence about self-monitoring and adherence."
        )
        self.assertTrue(
            all('"self-monitoring"[Title/Abstract]' in term for term in terms)
        )
        self.assertTrue(
            all("adherence[Title/Abstract]" in term for term in terms)
        )
        with self.assertRaisesRegex(
            NCBIClientError,
            "ncbi_no_relevant_records",
        ):
            ConceptFilteringNCBIClient().search(
                "Find evidence about self-monitoring and adherence."
            )


if __name__ == "__main__":
    unittest.main()
