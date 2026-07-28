from __future__ import annotations

import unittest
import uuid

from scripts.memory_v1_v5_2_exact_terminal_batch import (
    exact_disposition_items,
    exact_terminal_items,
    validate_batch,
)


PACKET_A = "11111111-1111-4111-8111-111111111111"
PACKET_B = "22222222-2222-4222-8222-222222222222"
SHA_A = "a" * 64
SHA_B = "b" * 64


class ExactTerminalBatchTest(unittest.TestCase):
    def test_parses_disjoint_exact_items(self) -> None:
        terminal = exact_terminal_items([f"{PACKET_A}:{SHA_A}"])
        dispositions = exact_disposition_items(
            [f"{PACKET_B}:{SHA_B}:deferral_only_review_unresolved"]
        )
        validate_batch(terminal, dispositions)
        self.assertEqual(terminal[uuid.UUID(PACKET_A)], SHA_A)
        self.assertEqual(
            dispositions[uuid.UUID(PACKET_B)],
            (SHA_B, "deferral_only_review_unresolved"),
        )

    def test_rejects_overlap(self) -> None:
        terminal = exact_terminal_items([f"{PACKET_A}:{SHA_A}"])
        dispositions = exact_disposition_items(
            [f"{PACKET_A}:{SHA_A}:deferral_only_no_stage"]
        )
        with self.assertRaises(RuntimeError):
            validate_batch(terminal, dispositions)

    def test_rejects_invalid_reason_and_hash(self) -> None:
        with self.assertRaises(RuntimeError):
            exact_disposition_items([f"{PACKET_A}:{SHA_A}:not_allowed"])
        with self.assertRaises(RuntimeError):
            exact_terminal_items([f"{PACKET_A}:short"])


if __name__ == "__main__":
    unittest.main()
