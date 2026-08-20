from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from uuid import UUID

from seebx.adapters.conversation_threads import (
    ARCHIVE_THREAD_SQL,
    CREATE_THREAD_SQL,
    FETCH_THREAD_TITLE_STATE_SQL,
    FETCH_THREAD_TITLE_TRANSCRIPT_SQL,
    LIST_VISIBLE_THREADS_SQL,
    RENAME_THREAD_MANUAL_SQL,
    SET_THREAD_PINNED_SQL,
    THREAD_BELONGS_TO_OWNER_SQL,
    UPDATE_THREAD_AUTOMATIC_TITLE_SQL,
    archive_thread,
    create_and_select_thread,
    create_thread,
    fetch_thread_title_state,
    fetch_thread_title_transcript,
    list_visible_threads,
    rename_thread_manual,
    set_thread_pinned,
    thread_belongs_to_owner,
    update_thread_automatic_title,
)


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
THREAD = UUID("5240822d-ac9a-4096-95aa-e2b24d36ef50")


class RecordingTransaction:
    def __init__(self, connection) -> None:
        self.connection = connection

    async def __aenter__(self):
        if self.connection.transaction_depth != 0:
            raise AssertionError("nested transaction is not expected")
        self.connection.transaction_depth = 1
        self.connection.transaction_entries += 1

    async def __aexit__(self, exc_type, exc, tb):
        self.connection.transaction_depth = 0
        self.connection.transaction_exit_type = exc_type


class CreateAndSelectConnection:
    def __init__(self) -> None:
        self.transaction_depth = 0
        self.transaction_entries = 0
        self.transaction_exit_type = object()
        self.execute_calls = []
        self.fetchrow_calls = []
        self.created = {"id": THREAD, "title": "New chat"}

    def transaction(self):
        return RecordingTransaction(self)

    async def execute(self, query, *args):
        if self.transaction_depth != 1:
            raise AssertionError("write escaped transaction")
        self.execute_calls.append((query, args))
        return "OK"

    async def fetchrow(self, query, *args):
        if self.transaction_depth != 1:
            raise AssertionError("read escaped transaction")
        self.fetchrow_calls.append((query, args))
        if query == CREATE_THREAD_SQL:
            return self.created
        if "FROM public.threads" in query and "AND id=$2" in query:
            return {
                "id": THREAD,
                "title": "New chat",
                "updated_at": SimpleNamespace(
                    isoformat=lambda: "2026-08-20T00:00:00+00:00"
                ),
            }
        raise AssertionError("unexpected fetchrow")


class ConversationThreadsAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_and_select_is_one_adapter_transaction(self) -> None:
        connection = CreateAndSelectConnection()
        result = await create_and_select_thread(
            connection,
            owner_user_id=OWNER,
            title="New chat",
        )
        self.assertIs(result, connection.created)
        self.assertEqual(connection.transaction_entries, 1)
        self.assertEqual(connection.transaction_depth, 0)
        self.assertIsNone(connection.transaction_exit_type)
        self.assertEqual(
            connection.fetchrow_calls[0],
            (CREATE_THREAD_SQL, (OWNER, OWNER, "New chat")),
        )
        self.assertTrue(
            any(
                "INSERT INTO public.active_thread_selection" in query
                for query, _args in connection.execute_calls
            )
        )

    async def test_all_thread_queries_are_owner_scoped_and_argument_stable(self) -> None:
        created = {"id": THREAD}
        renamed = {"title": "Manual", "title_source": "manual"}
        pinned = {"pinned_at": object()}
        state = {"title": "New chat", "title_source": "placeholder"}
        automatic = {"title": "Automatic", "title_source": "automatic"}
        listed = ({"id": THREAD},)
        transcript = ({"source": "frontend/chat:user", "text": "Question"},)
        connection = SimpleNamespace(
            execute=AsyncMock(return_value="UPDATE 1"),
            fetch=AsyncMock(side_effect=(listed, transcript)),
            fetchrow=AsyncMock(
                side_effect=(created, renamed, pinned, state, automatic)
            ),
            fetchval=AsyncMock(return_value=1),
        )

        self.assertTrue(
            await thread_belongs_to_owner(
                connection,
                owner_user_id=OWNER,
                thread_id=THREAD,
            )
        )
        self.assertIs(
            await create_thread(connection, owner_user_id=OWNER, title="New chat"),
            created,
        )
        self.assertEqual(
            await list_visible_threads(connection, owner_user_id=OWNER),
            listed,
        )
        self.assertIs(
            await rename_thread_manual(
                connection,
                owner_user_id=OWNER,
                thread_id=THREAD,
                title="Manual",
            ),
            renamed,
        )
        self.assertIs(
            await set_thread_pinned(
                connection,
                owner_user_id=OWNER,
                thread_id=THREAD,
                pinned=True,
            ),
            pinned,
        )
        self.assertIs(
            await fetch_thread_title_state(
                connection,
                owner_user_id=OWNER,
                thread_id=THREAD,
            ),
            state,
        )
        self.assertEqual(
            await fetch_thread_title_transcript(
                connection,
                owner_user_id=OWNER,
                thread_id=THREAD,
            ),
            transcript,
        )
        self.assertIs(
            await update_thread_automatic_title(
                connection,
                owner_user_id=OWNER,
                thread_id=THREAD,
                title="Automatic",
            ),
            automatic,
        )
        self.assertEqual(
            await archive_thread(
                connection,
                owner_user_id=OWNER,
                thread_id=THREAD,
            ),
            "UPDATE 1",
        )

        self.assertEqual(
            [call.args for call in connection.fetchrow.await_args_list],
            [
                (CREATE_THREAD_SQL, OWNER, OWNER, "New chat"),
                (RENAME_THREAD_MANUAL_SQL, "Manual", OWNER, THREAD),
                (SET_THREAD_PINNED_SQL, True, OWNER, THREAD),
                (FETCH_THREAD_TITLE_STATE_SQL, OWNER, THREAD),
                (UPDATE_THREAD_AUTOMATIC_TITLE_SQL, "Automatic", OWNER, THREAD),
            ],
        )
        self.assertEqual(
            [call.args for call in connection.fetch.await_args_list],
            [
                (LIST_VISIBLE_THREADS_SQL, OWNER),
                (FETCH_THREAD_TITLE_TRANSCRIPT_SQL, OWNER, THREAD),
            ],
        )
        connection.execute.assert_awaited_once_with(
            ARCHIVE_THREAD_SQL,
            OWNER,
            THREAD,
        )
        connection.fetchval.assert_awaited_once_with(
            THREAD_BELONGS_TO_OWNER_SQL,
            THREAD,
            OWNER,
        )

        for query in (
            THREAD_BELONGS_TO_OWNER_SQL,
            LIST_VISIBLE_THREADS_SQL,
            RENAME_THREAD_MANUAL_SQL,
            SET_THREAD_PINNED_SQL,
            FETCH_THREAD_TITLE_STATE_SQL,
            FETCH_THREAD_TITLE_TRANSCRIPT_SQL,
            UPDATE_THREAD_AUTOMATIC_TITLE_SQL,
            ARCHIVE_THREAD_SQL,
        ):
            self.assertIn("owner_user_id", query)


if __name__ == "__main__":
    unittest.main()
