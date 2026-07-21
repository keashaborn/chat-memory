#!/usr/bin/env python3
from __future__ import annotations

import unittest
import uuid

from scripts.memory_v1_v5_1_legacy_observation_reextract import (
    evidence_ids,
    loopback_dsn,
    safe_plan,
)


class LegacyObservationReextractTests(unittest.TestCase):
    def test_evidence_ids_are_unique_and_deterministically_sorted(self) -> None:
        first = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        second = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.assertEqual(
            evidence_ids([first, second, first]),
            [uuid.UUID(second), uuid.UUID(first)],
        )

    def test_only_loopback_database_is_allowed(self) -> None:
        self.assertEqual(
            loopback_dsn("postgresql://user@127.0.0.1/memory"),
            "postgresql://user@127.0.0.1/memory",
        )
        with self.assertRaisesRegex(RuntimeError, "loopback-only"):
            loopback_dsn("postgresql://user@database.example/memory")

    def test_safe_plan_contains_hashes_and_counts_but_no_owner_or_content(self) -> None:
        rows = [
            {
                "evidence_id": uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
                "evidence_content_sha256": "a" * 64,
                "route": "relational_extraction",
                "legacy_observation_count": 3,
            }
        ]
        result = safe_plan(rows)
        self.assertEqual(result[0]["evidence_content_sha256"], "a" * 64)
        self.assertEqual(result[0]["legacy_observation_count"], 3)
        self.assertNotIn("evidence_id", result[0])
        self.assertNotIn("content", result[0])


if __name__ == "__main__":
    unittest.main()
