from __future__ import annotations

import unittest

from scripts.memory_v1_evidence_intake_dispatcher import (
    validate_dispatch_transition,
)


def row(index: int, outcome: str = "eligible") -> dict[str, str]:
    return {"evidence_id": f"evidence-{index:03d}", "outcome": outcome}


class ValidateDispatchTransitionTest(unittest.TestCase):
    def test_full_page_can_refill_after_successful_dispatch(self) -> None:
        before = [row(index) for index in range(100)]
        after = [row(index) for index in range(100, 200)]
        validate_dispatch_transition(before, after, limit=100, apply=True)

    def test_partial_page_shrinks_exactly(self) -> None:
        before = [row(1), row(2, "deferred"), row(3, "skipped")]
        after = [row(2, "deferred")]
        validate_dispatch_transition(before, after, limit=100, apply=True)

    def test_dispatched_evidence_must_disappear(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "did not remove"):
            validate_dispatch_transition(
                [row(1)],
                [row(1)],
                limit=100,
                apply=True,
            )

    def test_deferred_evidence_must_remain(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "removed deferred"):
            validate_dispatch_transition(
                [row(1, "deferred"), row(2)],
                [],
                limit=100,
                apply=True,
            )

    def test_partial_page_cannot_gain_unrelated_rows(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "partial intake"):
            validate_dispatch_transition(
                [row(1)],
                [row(2)],
                limit=100,
                apply=True,
            )

    def test_dry_run_is_stable(self) -> None:
        before = [row(1), row(2, "deferred")]
        validate_dispatch_transition(
            before,
            list(before),
            limit=100,
            apply=False,
        )

    def test_dry_run_change_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "dry-run"):
            validate_dispatch_transition(
                [row(1)],
                [row(2)],
                limit=100,
                apply=False,
            )

    def test_duplicate_before_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "duplicate evidence before"):
            validate_dispatch_transition(
                [row(1), row(1)],
                [],
                limit=100,
                apply=True,
            )

    def test_duplicate_after_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "duplicate evidence after"):
            validate_dispatch_transition(
                [row(1)],
                [row(2), row(2)],
                limit=100,
                apply=True,
            )

    def test_empty_plan_is_valid(self) -> None:
        validate_dispatch_transition([], [], limit=100, apply=True)


if __name__ == "__main__":
    unittest.main()
