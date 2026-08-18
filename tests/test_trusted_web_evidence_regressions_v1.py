from __future__ import annotations

import unittest

from seebx.capabilities.search.plan import create_search_plan_v1
from rag_engine.trusted_web_ncbi_v1 import (
    NCBIClientError,
    NCBIPubMedClientV1,
    NCBIResearchRecordV1,
)


RESISTANCE_PROMPT = "Find evidence about resistance training frequency."
BEHAVIOR_PROMPT = "Find evidence about self-monitoring and adherence."


class UserVerifiedEvidenceClient(NCBIPubMedClientV1):
    def _esearch(self, query: str) -> tuple[str, ...]:
        return ("101", "202", "303", "404")

    def _efetch(
        self,
        ids: tuple[str, ...],
    ) -> tuple[NCBIResearchRecordV1, ...]:
        return (
            NCBIResearchRecordV1(
                pmid="101",
                title=(
                    "Resistance training frequency and muscular strength"
                ),
                abstract=(
                    "Resistance training frequency was evaluated in "
                    "healthy adults."
                ),
                publication_types=("Systematic Review",),
            ),
            NCBIResearchRecordV1(
                pmid="202",
                title="Antimicrobial stewardship",
                abstract=(
                    "Antimicrobial resistance and clinical training "
                    "programs were reviewed."
                ),
                publication_types=("Review",),
            ),
            NCBIResearchRecordV1(
                pmid="303",
                title=(
                    "Self-monitoring and adherence in weight management"
                ),
                abstract=(
                    "Self-monitoring and adherence outcomes were "
                    "evaluated in adults."
                ),
                publication_types=("Systematic Review",),
            ),
            NCBIResearchRecordV1(
                pmid="404",
                title="Remote monitoring in hypertension",
                abstract=(
                    "Remote observations and medication compliance were "
                    "described."
                ),
                publication_types=("Review",),
            ),
        )


class UnrelatedOnlyEvidenceClient(UserVerifiedEvidenceClient):
    def _efetch(
        self,
        ids: tuple[str, ...],
    ) -> tuple[NCBIResearchRecordV1, ...]:
        return (
            NCBIResearchRecordV1(
                pmid="202",
                title="Antimicrobial stewardship",
                abstract=(
                    "Antimicrobial resistance and clinical training "
                    "programs were reviewed."
                ),
                publication_types=("Review",),
            ),
        )


class TrustedWebEvidenceRegressionsV1Tests(unittest.TestCase):
    def test_user_verified_prompts_bind_plan_and_relevance_filter(
        self,
    ) -> None:
        cases = (
            (RESISTANCE_PROMPT, "exercise", ["101"]),
            (BEHAVIOR_PROMPT, "behavior_change", ["303"]),
        )
        client = UserVerifiedEvidenceClient()
        for prompt, expected_pack, expected_pmids in cases:
            with self.subTest(prompt=prompt):
                plan = create_search_plan_v1(prompt)
                self.assertEqual(plan.decision, "indexed")
                self.assertEqual(plan.selected_route, "trusted_health")
                self.assertEqual(plan.policy_pack, expected_pack)
                records = client.search(prompt)
                self.assertEqual(
                    [record.pmid for record in records],
                    expected_pmids,
                )

    def test_user_verified_prompts_fail_closed_when_only_unrelated(
        self,
    ) -> None:
        client = UnrelatedOnlyEvidenceClient()
        for prompt in (RESISTANCE_PROMPT, BEHAVIOR_PROMPT):
            with self.subTest(prompt=prompt):
                with self.assertRaisesRegex(
                    NCBIClientError,
                    "ncbi_no_relevant_records",
                ):
                    client.search(prompt)


if __name__ == "__main__":
    unittest.main()
