from __future__ import annotations

import asyncio
import unittest

from rag_engine.memory_v1_evidence_context_loader_v2 import (
    load_memory_evidence_context_v2,
)
from tests.test_memory_v1_evidence_context_loader_v1 import (
    OWNER,
    TARGET_ID,
    Connection,
    sha,
)


class ContextConnectionV2(Connection):
    def __init__(self) -> None:
        super().__init__()
        self.readonly_entries = 0
        self.prior_rows = [{
            "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "owner_user_id": OWNER,
            "thread_id": self.source["thread_id"],
            "request_id": "prior-request",
            "created_at": "2026-07-02T02:04:41.345358+00:00",
            "source": "frontend/chat:assistant",
            "text": "What philosophy are you referring to?",
        }]

    def transaction(self, *, readonly: bool):
        self.readonly_entries += int(readonly)
        return super().transaction(readonly=readonly)

    async def fetchval(self, query: str, *_args):
        self.queries.append(query)
        if "current_actor_user_id" in query:
            return self.actor
        if "encode(digest(text" in query:
            return sha(self.source["text"])
        raise AssertionError(query)

    async def fetch(self, query: str, *_args):
        self.queries.append(query)
        if "WITH ranked AS" in query:
            return list(reversed(self.evidence))
        if "ORDER BY created_at DESC" in query:
            return list(self.prior_rows)
        raise AssertionError(query)


class EvidenceContextLoaderV2Test(unittest.TestCase):
    def test_loader_uses_readonly_same_owner_thread_context(self) -> None:
        connection = ContextConnectionV2()
        envelope = asyncio.run(
            load_memory_evidence_context_v2(
                connection,
                expected_owner_user_id=OWNER,
                target_evidence_id=TARGET_ID,
                expected_target_content_sha256=connection.target[
                    "content_sha256"
                ],
            )
        )
        self.assertEqual(connection.readonly_entries, 2)
        self.assertEqual(len(envelope.prior_turns), 1)
        self.assertFalse(
            envelope.prior_turns[0].assertion_origin_allowed
        )
        queries = "\n".join(connection.queries)
        self.assertIn("owner_user_id=$1", queries)
        self.assertIn("thread_id=$2", queries)
        self.assertIn("(created_at,id) < ($3,$4)", queries)
        self.assertNotIn("INSERT", queries)
        self.assertNotIn("UPDATE", queries)
        self.assertNotIn("DELETE", queries)


if __name__ == "__main__":
    unittest.main()
