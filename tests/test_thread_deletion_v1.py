from __future__ import annotations

import unittest
import contextlib
import os
import pathlib
import tempfile
import uuid
from typing import Any
from unittest import mock

from rag_engine.thread_deletion_v1 import (
    ThreadDeletionV1Error,
    delete_thread_v1,
    memory_source_lock_key_v1,
)
from rag_engine import thread_deletion_v1 as thread_deletion
from rag_engine.memory_v1_qdrant_rebuild_contract_v1 import (
    RebuildContractError,
    qdrant_mutation_lock,
)


OWNER = uuid.UUID("10000000-0000-4000-8000-000000000001")
THREAD = uuid.UUID("20000000-0000-4000-8000-000000000001")
USER_MESSAGE = uuid.UUID("30000000-0000-4000-8000-000000000001")
ASSISTANT_MESSAGE = uuid.UUID("30000000-0000-4000-8000-000000000002")
EVIDENCE = uuid.UUID("40000000-0000-4000-8000-000000000001")
CLAIM = uuid.UUID("50000000-0000-4000-8000-000000000001")


class FakeTransaction:
    def __init__(self, conn: "FakeConnection") -> None:
        self.conn = conn

    async def __aenter__(self) -> None:
        self.conn.transaction_entered += 1

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.conn.transaction_exited += 1
        self.conn.transaction_error = exc


class FakeConnection:
    def __init__(
        self,
        *,
        has_thread: bool = True,
        has_project_binding: bool = False,
    ) -> None:
        self.has_thread = has_thread
        self.has_project_binding = has_project_binding
        self.transaction_entered = 0
        self.transaction_exited = 0
        self.transaction_error: BaseException | None = None
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.lifecycle_calls = 0

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append((query, args))
        normalized = " ".join(query.split())
        if normalized.startswith("SELECT set_config") or "pg_advisory_xact_lock" in query:
            return "SELECT 1"
        if "memory.retrieval_trace" in query:
            return "DELETE 2"
        if "memory.final_answer_memory_binding_v1" in query:
            return "DELETE 1"
        if "memory.assistant_transcript_attestation_v1" in query:
            return "DELETE 1"
        if "public.telemetry_event" in query:
            return "DELETE 3"
        if "public.chat_log" in query:
            return "DELETE 2"
        if "public.threads" in query:
            return "DELETE 1"
        raise AssertionError(f"unexpected execute: {query}")

    async def fetchval(self, query: str, *args: Any) -> uuid.UUID | None:
        if "FROM public.threads" in query:
            return THREAD if self.has_thread else None
        if "memory.project_thread_binding_event" in query:
            return self.has_project_binding
        raise AssertionError(f"unexpected fetchval: {query}")

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM public.chat_log" in query:
            return [{"id": USER_MESSAGE}, {"id": ASSISTANT_MESSAGE}]
        if "FROM memory.consolidation_job" in query:
            return [{"job_id": uuid.UUID(int=9)}]
        if "FROM memory.evidence" in query:
            return [{"evidence_id": EVIDENCE}]
        if "FROM memory.claim_evidence AS linked" in query:
            return [{"claim_id": CLAIM}]
        raise AssertionError(f"unexpected fetch: {query}")

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        if "memory.transition_evidence_lifecycle" not in query:
            raise AssertionError(f"unexpected fetchrow: {query}")
        self.lifecycle_calls += 1
        return {"outcome": "applied", "resulting_status": "deleted"}


class FakeQdrant:
    def __init__(self, *, leave_raw: bool = False) -> None:
        self.leave_raw = leave_raw
        self.delete_calls: list[dict[str, Any]] = []
        self.scroll_calls: list[dict[str, Any]] = []

    def delete(self, **kwargs: Any) -> None:
        self.delete_calls.append(kwargs)

    def scroll(self, **kwargs: Any) -> tuple[list[object], None]:
        self.scroll_calls.append(kwargs)
        if self.leave_raw and kwargs["collection_name"] == "memory_raw":
            return ([object()], None)
        return ([], None)


class ThreadDeletionV1Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.lock_patch = mock.patch.object(
            thread_deletion,
            "qdrant_mutation_lock",
            side_effect=lambda **kwargs: contextlib.nullcontext(),
        )
        self.lock_patch.start()
        self.addCleanup(self.lock_patch.stop)
        self.collection_patch = mock.patch.dict(
            os.environ, {"MEMORY_V1_COLLECTION": "memory_claim_v1"}, clear=False
        )
        self.collection_patch.start()
        self.addCleanup(self.collection_patch.stop)

    def test_exclusive_alias_lock_blocks_thread_deletion_qdrant_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "qdrant.lock"
            exclusive = qdrant_mutation_lock(
                exclusive=True, timeout_seconds=0.0, path=path
            )
            exclusive.acquire()
            qdrant = FakeQdrant()
            try:
                with mock.patch.object(
                    thread_deletion,
                    "qdrant_mutation_lock",
                    side_effect=lambda **kwargs: qdrant_mutation_lock(
                        exclusive=False, timeout_seconds=0.0, path=path
                    ),
                ), self.assertRaises(RebuildContractError):
                    thread_deletion._delete_and_verify_qdrant(
                        qdrant, OWNER, THREAD, [CLAIM]
                    )
            finally:
                exclusive.release()
            self.assertEqual(qdrant.delete_calls, [])

    def test_source_lock_identity_is_owner_and_source_scoped(self) -> None:
        self.assertEqual(
            memory_source_lock_key_v1(
                OWNER,
                "public.chat_log",
                USER_MESSAGE,
            ),
            f"{OWNER}:public.chat_log:{USER_MESSAGE}",
        )

    async def test_deletes_every_active_surface_and_verifies_qdrant(self) -> None:
        conn = FakeConnection()
        qdrant = FakeQdrant()

        result = await delete_thread_v1(
            conn,
            qdrant,
            owner_user_id=OWNER,
            thread_id=THREAD,
        )

        self.assertEqual(result.status, "completed")
        self.assertTrue(result.qdrant_verified)
        self.assertTrue(result.retained_audit_tombstones)
        self.assertEqual(result.counts.governed_evidence_tombstoned, 1)
        self.assertEqual(
            result.counts.unsupported_claim_projections_verified_absent,
            1,
        )
        self.assertEqual(result.counts.retrieval_traces_deleted, 2)
        self.assertEqual(result.counts.answer_bindings_deleted, 1)
        self.assertEqual(result.counts.attestations_deleted, 1)
        self.assertEqual(result.counts.telemetry_events_deleted, 3)
        self.assertEqual(result.counts.transcript_rows_deleted, 2)
        self.assertEqual(result.counts.threads_deleted, 1)
        self.assertEqual(conn.lifecycle_calls, 1)
        self.assertEqual(
            [call["collection_name"] for call in qdrant.delete_calls],
            ["memory_raw", "memory_claim_v1"],
        )
        self.assertEqual((conn.transaction_entered, conn.transaction_exited), (1, 1))
        self.assertIsNone(conn.transaction_error)

        sql = "\n".join(query for query, _ in conn.execute_calls)
        expected_order = [
            "memory.retrieval_trace",
            "memory.final_answer_memory_binding_v1",
            "memory.assistant_transcript_attestation_v1",
            "public.telemetry_event",
            "public.chat_log",
            "public.threads",
        ]
        positions = [sql.index(fragment) for fragment in expected_order]
        self.assertEqual(positions, sorted(positions))

    async def test_qdrant_verification_failure_aborts_database_deletes(self) -> None:
        conn = FakeConnection()

        with self.assertRaises(ThreadDeletionV1Error) as raised:
            await delete_thread_v1(
                conn,
                FakeQdrant(leave_raw=True),
                owner_user_id=OWNER,
                thread_id=THREAD,
            )

        self.assertEqual(raised.exception.code, "qdrant_raw_cleanup_not_verified")
        self.assertTrue(raised.exception.retryable)
        sql = "\n".join(query for query, _ in conn.execute_calls)
        self.assertNotIn("DELETE FROM", sql)
        self.assertIs(conn.transaction_error, raised.exception)

    async def test_claim_cleanup_uses_the_authoritative_collection_setting(self) -> None:
        conn = FakeConnection()
        qdrant = FakeQdrant()
        with mock.patch.dict(
            os.environ,
            {"MEMORY_V1_COLLECTION": "memory_claim_v1_active"},
            clear=False,
        ):
            await delete_thread_v1(
                conn,
                qdrant,
                owner_user_id=OWNER,
                thread_id=THREAD,
            )
        self.assertEqual(
            [call["collection_name"] for call in qdrant.delete_calls],
            ["memory_raw", "memory_claim_v1_active"],
        )
        self.assertEqual(
            [call["collection_name"] for call in qdrant.scroll_calls],
            ["memory_raw", "memory_claim_v1_active"],
        )

    async def test_missing_authoritative_collection_fails_before_cleanup(self) -> None:
        conn = FakeConnection()
        qdrant = FakeQdrant()
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(
            ThreadDeletionV1Error
        ) as raised:
            await delete_thread_v1(
                conn,
                qdrant,
                owner_user_id=OWNER,
                thread_id=THREAD,
            )
        self.assertEqual(raised.exception.code, "invalid_memory_claim_collection")
        self.assertEqual(qdrant.delete_calls, [])

    async def test_absent_owner_thread_fails_before_any_cleanup(self) -> None:
        conn = FakeConnection(has_thread=False)
        qdrant = FakeQdrant()

        with self.assertRaises(ThreadDeletionV1Error) as raised:
            await delete_thread_v1(
                conn,
                qdrant,
                owner_user_id=OWNER,
                thread_id=THREAD,
            )

        self.assertEqual(raised.exception.code, "thread_not_found")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(qdrant.delete_calls, [])

    async def test_project_bound_thread_requires_governed_erasure(self) -> None:
        conn = FakeConnection(has_project_binding=True)
        qdrant = FakeQdrant()

        with self.assertRaises(ThreadDeletionV1Error) as raised:
            await delete_thread_v1(
                conn,
                qdrant,
                owner_user_id=OWNER,
                thread_id=THREAD,
            )

        self.assertEqual(
            raised.exception.code,
            "governed_project_thread_erasure_required",
        )
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(qdrant.delete_calls, [])


if __name__ == "__main__":
    unittest.main()
