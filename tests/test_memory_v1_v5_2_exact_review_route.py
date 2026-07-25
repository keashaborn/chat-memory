from __future__ import annotations

import unittest
import uuid

from scripts.memory_v1_v5_2_exact_review_route import (
    exact_packet_ids,
    sanitized_plan,
)


class V52ExactReviewRouteTest(unittest.TestCase):
    def test_exact_packet_ids_accept_a_bounded_unique_set(self) -> None:
        first = "b76915b8-0603-50e8-b263-761da39f5651"
        second = "6ae4a8e6-b207-5201-a997-53fb2363fc9d"
        self.assertEqual(
            exact_packet_ids([first, second]),
            sorted([uuid.UUID(first), uuid.UUID(second)], key=str),
        )
        self.assertEqual(exact_packet_ids([first, first]), [uuid.UUID(first)])

    def test_exact_packet_ids_reject_empty_or_over_bound_sets(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "1-16 unique"):
            exact_packet_ids([])
        values = [
            str(uuid.uuid5(uuid.NAMESPACE_URL, f"packet-{index}"))
            for index in range(17)
        ]
        with self.assertRaisesRegex(RuntimeError, "1-16 unique"):
            exact_packet_ids(values)

    def test_missing_exact_plan_is_sanitized_as_no_work(self) -> None:
        packet_id = uuid.UUID("b76915b8-0603-50e8-b263-761da39f5651")
        value = sanitized_plan(packet_id, None)
        self.assertEqual(value["route"], "no_work")
        self.assertIsNone(value["counts"])
        self.assertNotIn(str(packet_id), str(value))


if __name__ == "__main__":
    unittest.main()
