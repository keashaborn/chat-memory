from __future__ import annotations

import unittest
import json
from pathlib import Path
from uuid import UUID, uuid4

from seebx.capabilities.search.audit import (
    acquire_trusted_web_rate_limit_v1,
    finish_trusted_web_audit_v1,
    query_sha256,
    start_trusted_web_audit_v1,
)
from seebx.capabilities.search.provider import TrustedWebSourceV1


ACTOR = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")


class FakeConnection:
    def __init__(self, count: int = 1):
        self.count = count
        self.fetchval_calls = []
        self.execute_calls = []

    async def fetchval(self, sql, *args):
        self.fetchval_calls.append((sql, args))
        return self.count

    async def execute(self, sql, *args):
        self.execute_calls.append((sql, args))
        return "INSERT 0 1"


class TrustedWebAuditV1Tests(unittest.IsolatedAsyncioTestCase):
    def test_legacy_evidence_and_audit_modules_are_retired(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        for relative_path in (
            "rag_engine/citation_evidence_v1.py",
            "rag_engine/trusted_web_admission_v1.py",
            "rag_engine/trusted_web_audit_v1.py",
        ):
            with self.subTest(relative_path=relative_path):
                self.assertFalse((repository / relative_path).exists())

    async def test_rate_limit_is_atomic_and_user_scoped(self) -> None:
        conn = FakeConnection(count=6)
        admitted = await acquire_trusted_web_rate_limit_v1(
            conn,
            actor_user_id=ACTOR,
            requests_per_minute=6,
        )
        self.assertTrue(admitted)
        sql, args = conn.fetchval_calls[0]
        self.assertIn("ON CONFLICT", sql)
        self.assertEqual(args, (ACTOR,))

        conn.count = 7
        denied = await acquire_trusted_web_rate_limit_v1(
            conn,
            actor_user_id=ACTOR,
            requests_per_minute=6,
        )
        self.assertFalse(denied)

    async def test_audit_start_receives_hash_not_raw_query(self) -> None:
        conn = FakeConnection()
        raw_query = "Does creatine improve strength?"
        digest = query_sha256(raw_query)
        await start_trusted_web_audit_v1(
            conn,
            search_id=uuid4(),
            actor_user_id=ACTOR,
            request_id="request-test",
            query_hash=digest,
            policy_version="trusted_web_policy_v1",
            topic="supplements",
            disposition="search",
            allowed_domains=("ods.od.nih.gov",),
        )
        _sql, args = conn.execute_calls[0]
        self.assertEqual(len(digest), 64)
        self.assertIn(digest, args)
        self.assertNotIn(raw_query, args)

    async def test_audit_distinguishes_provider_observed_and_admitted(
        self,
    ) -> None:
        conn = FakeConnection()
        cited = TrustedWebSourceV1(
            url="https://openai.com/index/cited",
            title="Cited",
        )
        supporting = TrustedWebSourceV1(
            url="https://apnews.com/article/supporting",
            title="Supporting",
        )
        rejected = TrustedWebSourceV1(
            url="https://openai.com/news",
            title="News hub",
        )
        await finish_trusted_web_audit_v1(
            conn,
            search_id=uuid4(),
            status="completed",
            latency_ms=10,
            sources=(cited, supporting, rejected),
            cited_sources=(cited,),
            admitted_sources=(cited, supporting),
            rejected_source_reasons=((rejected.url, "generic_index"),),
        )
        _sql, args = conn.execute_calls[0]
        metadata = json.loads(args[3])
        self.assertEqual(metadata[0]["citation_status"], "cited")
        self.assertEqual(metadata[0]["admission_reason"], "cited")
        self.assertEqual(metadata[1]["admission_reason"], "policy_relevant")
        self.assertEqual(
            metadata[2]["admission_status"],
            "provider_observed_only",
        )
        self.assertEqual(metadata[2]["admission_reason"], "generic_index")


if __name__ == "__main__":
    unittest.main()
