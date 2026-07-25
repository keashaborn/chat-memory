from __future__ import annotations

import unittest

from scripts.memory_v1_v5_2_atom_admission_manifest_v2 import (
    CASES,
    deterministic_id,
)


class AtomAdmissionManifestV2Test(unittest.TestCase):
    def test_ids_are_deterministic_and_role_separated(self) -> None:
        proposal_hash = "a" * 64
        values = {
            deterministic_id(CASES[0], proposal_hash, role)
            for role in (
                "proposal-operation",
                "proposal",
                "review-operation",
                "review",
                "apply-operation",
                "apply",
            )
        }
        self.assertEqual(len(values), 6)
        self.assertEqual(
            deterministic_id(CASES[0], proposal_hash, "proposal"),
            deterministic_id(CASES[0], proposal_hash, "proposal"),
        )

    def test_exact_two_cases_have_two_governed_observations(self) -> None:
        self.assertEqual(len(CASES), 2)
        self.assertTrue(
            all(
                case["expected_counts"]["admitted_observation_count"] == 2
                for case in CASES
            )
        )


if __name__ == "__main__":
    unittest.main()
