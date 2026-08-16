from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from rag_engine.chat_history_clear_v1 import (
    ChatHistoryClearError,
    authorization_manifest_sha256,
    clear_chat_history_v1,
)


OWNER = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
THREAD = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
OPERATION = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
AUTHORIZATION = "Bearer aaa.bbb.ccc"
ROOT = Path(__file__).resolve().parents[1]


class FakeConnection:
    def __init__(self, row: dict[str, object] | None = None) -> None:
        self.row = row or {
            "outcome": "cleared",
            "operation_id": OPERATION,
            "scope": "all",
            "deleted_message_count": 4,
            "deleted_thread_count": 2,
            "deleted_outbox_count": 2,
            "receipt_sha256": "a" * 64,
            "completed_at": datetime(2026, 8, 16, tzinfo=UTC),
        }
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[object, ...]]] = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def execute(self, sql: str, *args: object) -> str:
        self.execute_calls.append((sql, args))
        return "SELECT 1"

    async def fetchrow(self, sql: str, *args: object):
        self.fetchrow_calls.append((sql, args))
        return self.row


class ChatHistoryClearTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_scope_binds_owner_and_auth_without_zep(self) -> None:
        connection = FakeConnection()
        result = await clear_chat_history_v1(
            connection,
            owner_user_id=OWNER,
            authorization=AUTHORIZATION,
            operation_id=OPERATION,
            scope="all",
        )
        self.assertEqual(result.deleted_message_count, 4)
        self.assertEqual(connection.execute_calls[0][1], (str(OWNER),))
        self.assertEqual(
            connection.execute_calls[1][1],
            (authorization_manifest_sha256(AUTHORIZATION),),
        )
        self.assertIn("chat_history_private.clear_history", connection.fetchrow_calls[0][0])
        self.assertEqual(
            connection.fetchrow_calls[0][1],
            (OPERATION, "all", None, None),
        )
        self.assertTrue(result.as_dict()["memory_retained"])
        self.assertFalse(result.as_dict()["zep_called"])

    async def test_scope_shape_is_closed(self) -> None:
        with self.assertRaisesRegex(ChatHistoryClearError, "invalid_chat_history_request"):
            await clear_chat_history_v1(
                FakeConnection(),
                owner_user_id=OWNER,
                authorization=AUTHORIZATION,
                operation_id=OPERATION,
                scope="recent",
                thread_id=THREAD,
                recent_window_seconds=3_600,
            )

    async def test_message_tail_uses_anchor_as_operation_id(self) -> None:
        connection = FakeConnection(
            {
                **FakeConnection().row,
                "operation_id": OPERATION,
                "scope": "message_tail",
                "deleted_message_count": 2,
                "deleted_thread_count": 0,
            }
        )
        result = await clear_chat_history_v1(
            connection,
            owner_user_id=OWNER,
            authorization=AUTHORIZATION,
            operation_id=OPERATION,
            scope="message_tail",
            thread_id=THREAD,
        )
        self.assertEqual(
            connection.fetchrow_calls[0][1],
            (OPERATION, THREAD),
        )
        self.assertIn(
            "chat_history_private.clear_message_tail",
            connection.fetchrow_calls[0][0],
        )
        self.assertEqual(result.scope, "message_tail")
        self.assertTrue(result.as_dict()["memory_retained"])
        self.assertFalse(result.as_dict()["zep_called"])

    async def test_authorization_must_be_exact_bearer_jwt(self) -> None:
        with self.assertRaisesRegex(ChatHistoryClearError, "unauthorized"):
            authorization_manifest_sha256("Bearer not-a-jwt")

    def test_migration_cannot_touch_memory_zep_or_lifeswitch(self) -> None:
        migration = (
            ROOT
            / "chat-history-migrations/0001_owner_clear/forward.pgsql"
        ).read_text()
        self.assertIn("CREATE FUNCTION chat_history_private.clear_history", migration)
        self.assertIn("DELETE FROM public.chat_log", migration)
        self.assertIn("DELETE FROM memory_ingest_private.memory_ingest_outbox", migration)
        for forbidden in (
            "memory.claim",
            "memory.evidence",
            "memory.entity",
            "lifeswitch_nutrition",
            "lifeswitch_training",
            "lifeswitch_measurement",
            "api.getzep.com",
            "zep_",
        ):
            self.assertNotIn(forbidden, migration.casefold())

        message_tail = (
            ROOT / "chat-history-migrations/0002_message_tail/forward.pgsql"
        ).read_text()
        self.assertIn("clear_message_tail", message_tail)
        self.assertIn("'message_tail'::text", message_tail)
        for forbidden in (
            "memory.claim",
            "memory.evidence",
            "memory.entity",
            "lifeswitch_nutrition",
            "lifeswitch_training",
            "lifeswitch_measurement",
            "api.getzep.com",
            "zep_",
        ):
            self.assertNotIn(forbidden, message_tail.casefold())


if __name__ == "__main__":
    unittest.main()
