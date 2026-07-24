#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from memory_v1_v5_2_projection_review_manifest import (  # noqa: E402
    ManifestError,
    validate_decisions,
)


OWNER = "11111111-1111-4111-8111-111111111111"
EVIDENCE = "22222222-2222-4222-8222-222222222222"
OBSERVATION = "33333333-3333-4333-8333-333333333333"


def decision_value(decision: str = "deferred") -> dict:
    return {
        "contract_version": "memory_v1_v5_2_projection_review_decisions_v1",
        "owner_user_id": OWNER,
        "evidence_id": EVIDENCE,
        "decisions": [
            {
                "observation_id": OBSERVATION,
                "decision": decision,
                "reason": "source span is ambiguous",
                "reason_codes": [
                    "ambiguous_transcript",
                    "promotion_ineligible_pending_clarification",
                ],
            }
        ],
        "decisions_sha256": "0" * 64,
    }


class ProjectionReviewContractTests(unittest.TestCase):
    def test_deferred_decision_is_accepted(self) -> None:
        result = validate_decisions(decision_value(), OWNER, EVIDENCE)
        self.assertEqual(result[OBSERVATION]["decision"], "deferred")

    def test_rejected_decision_is_accepted(self) -> None:
        result = validate_decisions(decision_value("rejected"), OWNER, EVIDENCE)
        self.assertEqual(result[OBSERVATION]["decision"], "rejected")

    def test_unknown_decision_is_rejected(self) -> None:
        with self.assertRaises(ManifestError):
            validate_decisions(decision_value("approve"), OWNER, EVIDENCE)

    def test_owner_mismatch_is_rejected(self) -> None:
        with self.assertRaises(ManifestError):
            validate_decisions(
                decision_value(),
                "44444444-4444-4444-8444-444444444444",
                EVIDENCE,
            )


if __name__ == "__main__":
    unittest.main()
