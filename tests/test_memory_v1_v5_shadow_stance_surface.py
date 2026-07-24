from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag_engine.memory_v1_v5_shadow_retrieval import (  # noqa: E402
    evaluate_v5_shadow_claims,
)


OWNER = "11111111-1111-4111-8111-111111111111"
CLAIM = "22222222-2222-4222-8222-222222222222"
EVIDENCE = "33333333-3333-4333-8333-333333333333"
OBSERVATION = "44444444-4444-4444-8444-444444444444"


def record() -> dict[str, object]:
    return {
        "owner_user_id": OWNER,
        "claim_id": CLAIM,
        "canonical_text": 'The user reports this position: "Worrying does not help."',
        "predicate": "stance.reported",
        "status": "supported",
        "sensitivity": "medium",
        "retrieval_policy": {
            "surface_policy": "relevant_recall_or_explicit_recall"
        },
        "project_key": None,
        "component_key": None,
        "evidence_by_stance": {
            "supports": [EVIDENCE],
            "opposes": [],
            "qualifies": [],
            "context": [],
        },
        "observation_ids": [OBSERVATION],
        "importance": 0.5,
        "salience": 0.5,
        "valid_from": None,
        "valid_to": None,
        "metadata": {"memory_contract": "memory_projection_v5"},
        "canonical_key": "v5:stance:reported:worrying",
        "projection_review_decision": "authorized",
        "projection_apply_outcome": "applied",
    }


def evaluate(*, intent: str, explicit_recall: bool) -> dict[str, object]:
    return evaluate_v5_shadow_claims(
        OWNER,
        query="What have I said about worrying?",
        intent=intent,
        domain="personal",
        allowed_predicate_prefixes=["stance.reported"],
        candidate_hits=[{"claim_id": CLAIM, "semantic_score": 1.0}],
        records=[record()],
        max_claims=4,
        max_tokens=500,
        max_sensitivity="restricted",
        explicit_recall=explicit_recall,
    )


class StanceRecallSurfaceTest(unittest.TestCase):
    def test_explicit_recall_selects_reported_stance(self) -> None:
        result = evaluate(intent="specific_recall", explicit_recall=True)
        self.assertEqual(result["selected_count"], 1)
        self.assertEqual(
            result["claims"][0]["use_instruction"],
            "use_only_for_relevant_or_explicit_recall",
        )

    def test_relevant_recall_selects_reported_stance(self) -> None:
        result = evaluate(intent="personal_recall", explicit_recall=False)
        self.assertEqual(result["selected_count"], 1)

    def test_non_recall_turn_rejects_reported_stance_surface(self) -> None:
        result = evaluate(intent="general", explicit_recall=False)
        self.assertEqual(result["selected_count"], 0)
        self.assertEqual(result["rejected_counts"], {"surface_policy": 1})


if __name__ == "__main__":
    unittest.main()
