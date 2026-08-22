from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from uuid import UUID

from seebx.adapters.conversation_history import (
    THREAD_MESSAGE_ROWS_SQL,
    fetch_thread_message_rows,
)


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
THREAD = UUID("5240822d-ac9a-4096-95aa-e2b24d36ef50")


class ConversationHistoryAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_is_owner_thread_bound_and_preserves_query_contract(self) -> None:
        records = ({"id": UUID("6240822d-ac9a-4096-95aa-e2b24d36ef50")},)
        connection = SimpleNamespace(fetch=AsyncMock(return_value=records))

        result = await fetch_thread_message_rows(
            connection,
            owner_user_id=OWNER,
            thread_id=THREAD,
            limit=200,
        )

        self.assertEqual(result, records)
        connection.fetch.assert_awaited_once_with(
            THREAD_MESSAGE_ROWS_SQL,
            OWNER,
            THREAD,
            200,
        )
        self.assertIn(
            "web.owner_user_id=log.owner_user_id",
            THREAD_MESSAGE_ROWS_SQL,
        )
        self.assertIn("web.thread_id=log.thread_id", THREAD_MESSAGE_ROWS_SQL)
        self.assertIn(
            "attachment.owner_user_id=log.owner_user_id",
            THREAD_MESSAGE_ROWS_SQL,
        )
        self.assertIn(
            "attachment.thread_id=log.thread_id",
            THREAD_MESSAGE_ROWS_SQL,
        )
        self.assertIn("attachment.message_id=log.id", THREAD_MESSAGE_ROWS_SQL)
        self.assertIn(
            "ORDER BY attachment.created_at,attachment.id",
            THREAD_MESSAGE_ROWS_SQL,
        )
        self.assertIn(
            "WHERE log.owner_user_id=$1 AND log.thread_id=$2",
            THREAD_MESSAGE_ROWS_SQL,
        )
        self.assertIn("ORDER BY log.created_at ASC", THREAD_MESSAGE_ROWS_SQL)
        self.assertIn("LIMIT $3", THREAD_MESSAGE_ROWS_SQL)

    def test_app_route_is_sql_free_and_delegates_history_read(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (
            root / "seebx/capabilities/conversation/thread_routes.py"
        ).read_text(encoding="utf-8")
        route = source.split(
            '@router.get("/threads/{thread_id}/messages")',
            1,
        )[1].split(
            '@router.post("/threads/{thread_id}/rename")',
            1,
        )[0]

        self.assertIn("await fetch_thread_message_rows(", route)
        self.assertNotIn("conversation.chat_attachments", route)
        self.assertNotIn("trusted_web.response_transcript_v1", route)
        self.assertNotIn("SELECT ", route)
        self.assertIn("WEB_ASSISTANT_SOURCE", route)
        self.assertIn('"attachments": r["attachments"] or []', route)
        self.assertNotIn("conversation.chat_attachments", source)


if __name__ == "__main__":
    unittest.main()
