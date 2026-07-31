from __future__ import annotations

import unittest

from scripts.memory_v1_v5_local_claim_projection import (
    LocalClaimProjectionError,
    outcome_spec,
)


class LocalClaimProjectionV2Test(unittest.TestCase):
    def test_stage_path_is_unchanged(self) -> None:
        self.assertEqual(
            outcome_spec("stage_manual_review_plan"),
            (5, "manual_review_claim_plan_staged"),
        )

    def test_already_materialized_is_one_terminal_row(self) -> None:
        self.assertEqual(
            outcome_spec("already_materialized"),
            (1, "already_materialized"),
        )

    def test_unknown_decision_fails_closed(self) -> None:
        with self.assertRaises(LocalClaimProjectionError):
            outcome_spec("unexpected")


if __name__ == "__main__":
    unittest.main()
