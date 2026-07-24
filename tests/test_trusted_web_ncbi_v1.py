from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET

from rag_engine.trusted_web_ncbi_v1 import (
    NCBIPubMedClientV1,
    classify_publication_types,
    format_ncbi_records_for_model,
    trusted_web_topic_uses_ncbi,
)
from rag_engine.trusted_web_policy_v1 import TrustedWebTopicV1


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


if __name__ == "__main__":
    unittest.main()
