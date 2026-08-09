from __future__ import annotations

from datetime import datetime, timezone
import unittest
from uuid import UUID

from rag_engine.memory_v1_projection import (
    ProjectionError,
    claim_exact_projection_job,
)


OWNER = UUID("11111111-1111-4111-8111-111111111111")
OUTBOX = UUID("22222222-2222-4222-8222-222222222222")
CLAIM = UUID("33333333-3333-4333-8333-333333333333")
LEASE = UUID("44444444-4444-4444-8444-444444444444")
PAYLOAD_HASH = "a" * 64


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _Connection:
    def __init__(self, *, return_row: bool = True) -> None:
        self.return_row = return_row
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.query = ""
        self.args: tuple[object, ...] = ()

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def execute(self, query: str, *args: object) -> str:
        self.executed.append((query, args))
        return "SELECT 1"

    async def fetchrow(self, query: str, *args: object):
        self.query = query
        self.args = args
        if not self.return_row:
            return None
        return {
            "outbox_id": OUTBOX,
            "aggregate_id": CLAIM,
            "operation": "upsert",
            "payload": {"claim_id": str(CLAIM), "revision_number": 2},
            "attempts": 1,
            "lease_token": LEASE,
            "worker_id": "exact-pilot",
            "lease_expires_at": datetime.now(timezone.utc),
        }


class ExactProjectionOutboxV1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_claim_is_exact_id_owner_claim_and_payload_bound(self) -> None:
        conn = _Connection()
        job = await claim_exact_projection_job(
            conn,
            OWNER,
            outbox_id=OUTBOX,
            claim_id=CLAIM,
            expected_payload_sha256=PAYLOAD_HASH,
            worker_id="exact-pilot",
        )
        self.assertIsNotNone(job)
        self.assertEqual(job.outbox_id, OUTBOX)
        self.assertEqual(job.claim_id, CLAIM)
        self.assertEqual(
            conn.args,
            (OWNER, OUTBOX, CLAIM, PAYLOAD_HASH, "exact-pilot", 600, 8),
        )
        query = " ".join(conn.query.split())
        for contract in (
            "outbox.owner_user_id=$1",
            "outbox.outbox_id=$2",
            "outbox.aggregate_id=$3",
            "outbox.operation='upsert'",
            "outbox.status='pending'",
            "memory.v5_digest_text(",
            "memory.v5_canonical_json_text(outbox.payload)",
            ")=$4",
        ):
            self.assertIn(contract, query)
        self.assertNotIn("LIMIT", query.upper())
        self.assertNotIn("SKIP LOCKED", query.upper())

    async def test_unavailable_exact_target_does_not_fall_back_to_backlog(self) -> None:
        conn = _Connection(return_row=False)
        job = await claim_exact_projection_job(
            conn,
            OWNER,
            outbox_id=OUTBOX,
            claim_id=CLAIM,
            expected_payload_sha256=PAYLOAD_HASH,
            worker_id="exact-pilot",
        )
        self.assertIsNone(job)
        self.assertEqual(conn.query.upper().count("UPDATE MEMORY.PROJECTION_OUTBOX"), 1)

    async def test_invalid_identity_or_hash_fails_before_database_access(self) -> None:
        conn = _Connection()
        with self.assertRaises(ProjectionError):
            await claim_exact_projection_job(
                conn,
                OWNER,
                outbox_id="not-a-uuid",
                claim_id=CLAIM,
                expected_payload_sha256=PAYLOAD_HASH,
                worker_id="exact-pilot",
            )
        with self.assertRaises(ProjectionError):
            await claim_exact_projection_job(
                conn,
                OWNER,
                outbox_id=OUTBOX,
                claim_id=CLAIM,
                expected_payload_sha256="bad",
                worker_id="exact-pilot",
            )
        self.assertEqual(conn.query, "")


if __name__ == "__main__":
    unittest.main()
