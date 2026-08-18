from __future__ import annotations

from datetime import datetime
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID

import asyncpg

from seebx.adapters.conversation_persistence import (
    CREATE_USER_TRANSCRIPT_THREAD_SQL,
    FETCH_ATTACHMENT_REPLAY_SQL,
    FETCH_CONFLICTING_SUBMISSION_SQL,
    FETCH_EXACT_SUBMISSION_SQL,
    FETCH_USER_TRANSCRIPT_THREAD_SQL,
    INSERT_USER_TRANSCRIPT_SQL,
    TOUCH_USER_TRANSCRIPT_THREAD_SQL,
    UserTranscriptPersistenceError,
    persist_user_transcript,
)


OWNER = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
THREAD = UUID("5240822d-ac9a-4096-95aa-e2b24d36ef50")
SUBMISSION = UUID("7240822d-ac9a-4096-95aa-e2b24d36ef50")
ATTACHMENT = UUID("8240822d-ac9a-4096-95aa-e2b24d36ef50")
EXISTING = UUID("9240822d-ac9a-4096-95aa-e2b24d36ef50")


class FakeTransaction:
    def __init__(self) -> None:
        self.started = False
        self.committed = False
        self.rolled_back = False

    async def start(self) -> None:
        self.started = True

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class FakeConnection:
    def __init__(self) -> None:
        self.thread_row: object | None = {"owner_user_id": OWNER}
        self.attachment_replay: object | None = None
        self.exact_submission: object | None = None
        self.conflicting_submission: object | None = None
        self.insert_error: Exception | None = None
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []
        self.transaction_value = FakeTransaction()

    def transaction(self) -> FakeTransaction:
        return self.transaction_value

    async def fetchrow(self, query: str, *args: object) -> object | None:
        if query == FETCH_USER_TRANSCRIPT_THREAD_SQL:
            return self.thread_row
        if query == FETCH_ATTACHMENT_REPLAY_SQL:
            return self.attachment_replay
        if query == FETCH_EXACT_SUBMISSION_SQL:
            return self.exact_submission
        if query == FETCH_CONFLICTING_SUBMISSION_SQL:
            return self.conflicting_submission
        raise AssertionError(query)

    async def execute(self, query: str, *args: object) -> str:
        self.execute_calls.append((query, args))
        if query == INSERT_USER_TRANSCRIPT_SQL and self.insert_error is not None:
            raise self.insert_error
        return "OK"


async def persist(
    conn: FakeConnection,
    *,
    attachment_ids: tuple[UUID, ...] = (),
) -> object:
    return await persist_user_transcript(
        conn,
        owner_user_id=OWNER,
        user_id_alias=str(OWNER),
        source="frontend/chat:user",
        text="Tell me about my animals.",
        tags=["user", "chat"],
        thread_id=THREAD,
        vantage_id="default",
        request_id="synthetic-request",
        message_id=SUBMISSION,
        submission_id=SUBMISSION,
        created_at=datetime(2026, 8, 18, 15, 0, 0),
        attachment_ids=attachment_ids,
    )


class UserTranscriptPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_transcript_commits_insert_and_thread_touch(self) -> None:
        conn = FakeConnection()
        result = await persist(conn)

        self.assertEqual(result.message_id, SUBMISSION)
        self.assertFalse(result.replayed)
        self.assertTrue(conn.transaction_value.started)
        self.assertTrue(conn.transaction_value.committed)
        self.assertFalse(conn.transaction_value.rolled_back)
        statements = [query for query, _ in conn.execute_calls]
        self.assertIn(INSERT_USER_TRANSCRIPT_SQL, statements)
        self.assertIn(TOUCH_USER_TRANSCRIPT_THREAD_SQL, statements)

    async def test_missing_thread_is_created_inside_same_transaction(self) -> None:
        conn = FakeConnection()
        conn.thread_row = None

        await persist(conn)

        statements = [query for query, _ in conn.execute_calls]
        self.assertIn(CREATE_USER_TRANSCRIPT_THREAD_SQL, statements)
        self.assertTrue(conn.transaction_value.committed)

    async def test_exact_submission_retry_replays_without_insert(self) -> None:
        conn = FakeConnection()
        conn.exact_submission = {"id": SUBMISSION}

        result = await persist(conn)

        self.assertTrue(result.replayed)
        self.assertTrue(conn.transaction_value.committed)
        self.assertNotIn(
            INSERT_USER_TRANSCRIPT_SQL,
            [query for query, _ in conn.execute_calls],
        )

    async def test_submission_reuse_for_different_payload_rolls_back(self) -> None:
        conn = FakeConnection()
        conn.conflicting_submission = {"id": SUBMISSION}

        with self.assertRaises(UserTranscriptPersistenceError) as raised:
            await persist(conn)

        self.assertTrue(raised.exception.conflict)
        self.assertEqual(raised.exception.code, "submission_conflict")
        self.assertTrue(conn.transaction_value.rolled_back)
        self.assertFalse(conn.transaction_value.committed)

    async def test_bound_attachment_replays_matching_message(self) -> None:
        conn = FakeConnection()
        conn.attachment_replay = {"id": EXISTING}
        rows = [{
            "status": "ready",
            "deleted_at": None,
            "message_id": EXISTING,
        }]
        with patch(
            "seebx.adapters.conversation_persistence.fetch_attachment_bindings",
            new=AsyncMock(return_value=rows),
        ):
            result = await persist(conn, attachment_ids=(ATTACHMENT,))

        self.assertEqual(result.message_id, EXISTING)
        self.assertTrue(result.replayed)
        self.assertTrue(conn.transaction_value.committed)

    async def test_unready_attachment_fails_closed_and_rolls_back(self) -> None:
        conn = FakeConnection()
        rows = [{
            "status": "pending",
            "deleted_at": None,
            "message_id": None,
        }]
        with (
            patch(
                "seebx.adapters.conversation_persistence.fetch_attachment_bindings",
                new=AsyncMock(return_value=rows),
            ),
            self.assertRaises(UserTranscriptPersistenceError) as raised,
        ):
            await persist(conn, attachment_ids=(ATTACHMENT,))

        self.assertTrue(raised.exception.conflict)
        self.assertEqual(raised.exception.code, "attachment_binding_failed")
        self.assertTrue(conn.transaction_value.rolled_back)

    async def test_fresh_attachment_is_bound_before_commit(self) -> None:
        conn = FakeConnection()
        rows = [{
            "status": "ready",
            "deleted_at": None,
            "message_id": None,
        }]
        fetch = AsyncMock(return_value=rows)
        bind = AsyncMock(return_value=[{"id": ATTACHMENT}])
        with (
            patch(
                "seebx.adapters.conversation_persistence.fetch_attachment_bindings",
                new=fetch,
            ),
            patch(
                "seebx.adapters.conversation_persistence.bind_attachments_to_message",
                new=bind,
            ),
        ):
            result = await persist(conn, attachment_ids=(ATTACHMENT,))

        self.assertFalse(result.replayed)
        bind.assert_awaited_once()
        self.assertTrue(conn.transaction_value.committed)

    async def test_database_failure_is_unavailable_and_rolls_back(self) -> None:
        conn = FakeConnection()
        conn.insert_error = RuntimeError("synthetic database failure")

        with self.assertRaises(UserTranscriptPersistenceError) as raised:
            await persist(conn)

        self.assertFalse(raised.exception.conflict)
        self.assertEqual(raised.exception.code, "transcript_write_failed")
        self.assertTrue(conn.transaction_value.rolled_back)

    async def test_submission_unique_violation_retains_conflict_contract(self) -> None:
        conn = FakeConnection()
        conn.insert_error = asyncpg.UniqueViolationError("synthetic duplicate")

        with self.assertRaises(UserTranscriptPersistenceError) as raised:
            await persist(conn)

        self.assertTrue(raised.exception.conflict)
        self.assertEqual(raised.exception.code, "submission_conflict")
        self.assertTrue(conn.transaction_value.rolled_back)

    def test_log_route_contains_no_sql_or_transaction_ownership(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "app.py").read_text(encoding="utf-8")
        route = source.split('@app.post("/log")', 1)[1].split(
            '@app.post("/threads/new")', 1
        )[0]

        self.assertIn("persist_user_transcript(", route)
        self.assertNotIn("conn.fetchrow(", route)
        self.assertNotIn("conn.execute(", route)
        self.assertNotIn("conn.transaction()", route)
        self.assertNotIn("transaction.commit()", route)
        self.assertNotIn("transaction.rollback()", route)


if __name__ == "__main__":
    unittest.main()
