from __future__ import annotations

import unittest
import uuid
from typing import Any

from rag_engine.thread_deletion_v1 import memory_source_lock_key_v1
from scripts.memory_v1_consolidation_worker import (
    acquire_source_erasure_lock,
    release_source_erasure_lock,
)


OWNER = uuid.UUID("10000000-0000-4000-8000-000000000001")
SOURCE = "30000000-0000-4000-8000-000000000001"


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append((query, args))
        return "SELECT 1"

    async def fetchval(self, query: str, *args: Any) -> bool:
        self.calls.append((query, args))
        return True


class ConsolidationErasureLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_uses_same_owner_source_lock_as_deletion(self) -> None:
        conn = FakeConnection()
        expected = memory_source_lock_key_v1(
            OWNER,
            "public.chat_log",
            SOURCE,
        )

        await acquire_source_erasure_lock(
            conn,
            owner=OWNER,
            source_system="public.chat_log",
            source_external_id=SOURCE,
        )
        await release_source_erasure_lock(
            conn,
            owner=OWNER,
            source_system="public.chat_log",
            source_external_id=SOURCE,
        )

        self.assertEqual(len(conn.calls), 2)
        self.assertIn("pg_advisory_lock", conn.calls[0][0])
        self.assertIn("pg_advisory_unlock", conn.calls[1][0])
        self.assertEqual(conn.calls[0][1], (expected,))
        self.assertEqual(conn.calls[1][1], (expected,))


if __name__ == "__main__":
    unittest.main()
