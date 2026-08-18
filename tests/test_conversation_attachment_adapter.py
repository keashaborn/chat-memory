from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

from seebx.adapters.conversation_attachments import (
    OWNER_CONTEXT_SQL,
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


if __name__ == "__main__":
    unittest.main()
