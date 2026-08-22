from __future__ import annotations

from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

from seebx.adapters.conversation_attachments import (
    BIND_ATTACHMENTS_TO_MESSAGE_SQL,
    CREATE_ATTACHMENT_SQL,
    DELETE_ATTACHMENT_SQL,
    FETCH_ATTACHMENT_BINDINGS_SQL,
    FETCH_ATTACHMENT_STATUS_SQL,
    FETCH_RETRY_CANDIDATE_SQL,
    OWNER_CONTEXT_SQL,
    UPDATE_RETRY_STATUS_SQL,
    ConversationAttachmentPostgresStore,
    bind_attachments_to_message,
    fetch_attachment_bindings,
    fetch_ready_message_attachments,
)


OWNER = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
THREAD = UUID("5240822d-ac9a-4096-95aa-e2b24d36ef50")
MESSAGE = UUID("6240822d-ac9a-4096-95aa-e2b24d36ef50")
ATTACHMENT = UUID("7240822d-ac9a-4096-95aa-e2b24d36ef50")


class ConversationAttachmentAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_is_owner_bound_and_preserves_requested_order(self) -> None:
        records = ({"id": ATTACHMENT},)
        connection = SimpleNamespace(
            execute=AsyncMock(return_value="SELECT 1"),
            fetch=AsyncMock(return_value=records),
        )

        result = await fetch_ready_message_attachments(
            connection,
            owner_user_id=OWNER,
            thread_id=THREAD,
            message_id=MESSAGE,
            attachment_ids=(ATTACHMENT,),
        )

        self.assertEqual(result, records)
        connection.execute.assert_awaited_once_with(
            OWNER_CONTEXT_SQL,
            str(OWNER),
        )
        query, owner, thread, message, attachment_ids = (
            connection.fetch.await_args.args
        )
        self.assertEqual((owner, thread, message), (OWNER, THREAD, MESSAGE))
        self.assertEqual(attachment_ids, [ATTACHMENT])
        self.assertIn("message.owner_user_id=attachment.owner_user_id", query)
        self.assertIn("message.thread_id=attachment.thread_id", query)
        self.assertIn("attachment.owner_user_id=$1", query)
        self.assertIn("attachment.thread_id=$2", query)
        self.assertIn("attachment.message_id=$3", query)
        self.assertIn("attachment.status='ready'", query)
        self.assertIn("attachment.deleted_at IS NULL", query)
        self.assertIn("attachment.content IS NOT NULL", query)
        self.assertIn("array_position($4::uuid[], attachment.id)", query)

    async def test_message_binding_queries_are_owner_thread_bound_and_ordered(self) -> None:
        records = ({"id": ATTACHMENT},)
        connection = SimpleNamespace(
            fetch=AsyncMock(side_effect=(records, records)),
        )

        candidates = await fetch_attachment_bindings(
            connection,
            owner_user_id=OWNER,
            thread_id=THREAD,
            attachment_ids=(ATTACHMENT,),
        )
        bound = await bind_attachments_to_message(
            connection,
            message_id=MESSAGE,
            owner_user_id=OWNER,
            thread_id=THREAD,
            attachment_ids=(ATTACHMENT,),
        )

        self.assertEqual(candidates, records)
        self.assertEqual(bound, records)
        candidate_call, bind_call = connection.fetch.await_args_list
        self.assertEqual(
            candidate_call.args,
            (FETCH_ATTACHMENT_BINDINGS_SQL, OWNER, THREAD, [ATTACHMENT]),
        )
        self.assertEqual(
            bind_call.args,
            (
                BIND_ATTACHMENTS_TO_MESSAGE_SQL,
                MESSAGE,
                OWNER,
                THREAD,
                [ATTACHMENT],
            ),
        )
        self.assertIn("owner_user_id=$1", FETCH_ATTACHMENT_BINDINGS_SQL)
        self.assertIn("thread_id=$2", FETCH_ATTACHMENT_BINDINGS_SQL)
        self.assertIn(
            "ORDER BY array_position($3::uuid[],id)",
            FETCH_ATTACHMENT_BINDINGS_SQL,
        )
        self.assertIn("owner_user_id=$2", BIND_ATTACHMENTS_TO_MESSAGE_SQL)
        self.assertIn("thread_id=$3", BIND_ATTACHMENTS_TO_MESSAGE_SQL)
        self.assertIn("message_id IS NULL", BIND_ATTACHMENTS_TO_MESSAGE_SQL)
        self.assertIn("status='ready'", BIND_ATTACHMENTS_TO_MESSAGE_SQL)
        self.assertIn("deleted_at IS NULL", BIND_ATTACHMENTS_TO_MESSAGE_SQL)

    def test_log_route_delegates_attachment_sql_to_adapter(self) -> None:
        root = Path(__file__).resolve().parents[1]
        log_route = (
            root / "seebx/capabilities/conversation/transcript_routes.py"
        ).read_text(encoding="utf-8")

        adapter_source = (
            root / "seebx/adapters/conversation_persistence.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn("conversation.chat_attachments", log_route)
        self.assertNotIn("fetch_attachment_bindings(", log_route)
        self.assertNotIn("bind_attachments_to_message(", log_route)
        self.assertIn("persist_user_transcript(", log_route)
        self.assertIn("fetch_attachment_bindings(", adapter_source)
        self.assertIn("bind_attachments_to_message(", adapter_source)

    async def test_crud_store_owns_sql_actor_context_and_lifetime(self) -> None:
        records = tuple({"index": index} for index in range(5))
        connection = SimpleNamespace(
            execute=AsyncMock(return_value="SELECT 1"),
            fetchrow=AsyncMock(side_effect=records),
            close=AsyncMock(return_value=None),
        )
        connect = AsyncMock(return_value=connection)
        store = ConversationAttachmentPostgresStore(
            dsn="postgresql://bounded-test",
            connect_factory=connect,
        )

        async with store.owner_connection(OWNER) as owned:
            self.assertIs(owned, connection)
            created = await store.create_attachment(
                owned,
                attachment_id=ATTACHMENT,
                owner_user_id=OWNER,
                thread_id=THREAD,
                filename="bounded.md",
                media_type="text/markdown",
                content="bounded",
                content_sha256="a" * 64,
                byte_size=7,
            )
            status = await store.fetch_attachment_status(
                owned,
                attachment_id=ATTACHMENT,
                owner_user_id=OWNER,
            )
            retry = await store.fetch_retry_candidate(
                owned,
                attachment_id=ATTACHMENT,
                owner_user_id=OWNER,
            )
            updated = await store.update_retry_status(
                owned,
                attachment_id=ATTACHMENT,
                owner_user_id=OWNER,
                status="ready",
            )
            deleted = await store.delete_attachment(
                owned,
                attachment_id=ATTACHMENT,
                owner_user_id=OWNER,
            )

        self.assertEqual(
            (created, status, retry, updated, deleted),
            records,
        )
        connect.assert_awaited_once_with("postgresql://bounded-test")
        connection.execute.assert_awaited_once_with(
            OWNER_CONTEXT_SQL,
            str(OWNER),
        )
        connection.close.assert_awaited_once_with()
        queries = tuple(
            call.args[0] for call in connection.fetchrow.await_args_list
        )
        self.assertEqual(
            queries,
            (
                CREATE_ATTACHMENT_SQL,
                FETCH_ATTACHMENT_STATUS_SQL,
                FETCH_RETRY_CANDIDATE_SQL,
                UPDATE_RETRY_STATUS_SQL,
                DELETE_ATTACHMENT_SQL,
            ),
        )


if __name__ == "__main__":
    unittest.main()
