from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
MODULE_PATH = (
    SCRIPTS
    / "memory_v1_v5_2_evidence_context_stance_claim_apply_manifest.py"
)
SPEC = importlib.util.spec_from_file_location(
    "evidence_context_stance_claim_apply_manifest",
    MODULE_PATH,
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class EvidenceContextStanceClaimApplyManifestTest(unittest.TestCase):
    def test_exact_reviewed_stance_target_is_hash_locked(self) -> None:
        self.assertEqual(
            MODULE.base.TARGETS,
            {
                "0685b77e-de79-5216-b58d-6d7e464166e6": {
                    "observation_id": (
                        "87ce1a11-01ae-4d6f-80ea-8e62b5b43cff"
                    ),
                    "predicate": "stance.reported",
                    "canonical_text": (
                        'The user reports this position: '
                        '"Fractal Monism will help people in life."'
                    ),
                },
            },
        )
        canonical = next(iter(MODULE.base.TARGETS.values()))[
            "canonical_text"
        ]
        self.assertEqual(
            hashlib.sha256(canonical.encode()).hexdigest(),
            "48202a3979224024549a894df7eb71cf4772fc474ab199d5cb93aafa5725ab90",
        )

    def test_assessment_supports_attribution_not_external_truth(self) -> None:
        assessment = MODULE.base.ASSESSMENT
        self.assertEqual(assessment["action"], "promote_supported")
        self.assertIn(
            "attributed_belief_only",
            assessment["reason_codes"],
        )
        self.assertIn(
            "does not establish",
            assessment["rationale"],
        )

    def test_one_observation_budget_defers_projection(self) -> None:
        self.assertEqual(
            MODULE.base.EXPECTED_ROWS_PER_ITEM["claim_observation"],
            1,
        )
        self.assertEqual(
            MODULE.base.EXPECTED_ROWS_PER_ITEM["projection_outbox"],
            0,
        )
        self.assertEqual(
            sum(MODULE.base.EXPECTED_ROWS_PER_ITEM.values()),
            11,
        )


if __name__ == "__main__":
    unittest.main()
