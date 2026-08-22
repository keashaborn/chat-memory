from __future__ import annotations

import unittest
from typing import Any
from uuid import UUID

from seebx.adapters.openai_chat import OpenAIChatCompletionsAdapterV1
from seebx.capabilities.conversation.finalization import finalize_trusted_response_v1
from seebx.adapters.conversation_persistence import (
    ConversationPersistenceError,
    persist_conversation_response,
)
from tests.test_openai_chat_provider_v1 import FakeClient, provider_response
from tests.test_response_orchestration_v0_2 import (
    ACTOR,
    NOW,
    THREAD,
    FixedSafetyProvider,
    messages,
    orchestrator,
    trusted_request,
)


ANSWER = UUID("90000000-0000-4000-8000-000000000001")


class FakeTransaction:
    def __init__(self, conn: "FakeConnection") -> None:
        self.conn = conn

    async def __aenter__(self) -> None:
        self.conn.entered += 1

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.conn.exited += 1


class FakeConnection:
    def __init__(
        self,
        *,
        owns_thread: bool = True,
        visible_thread: bool = True,
    ) -> None:
        self.owns_thread = owns_thread
        self.visible_thread = visible_thread
        self.entered = 0
        self.exited = 0
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append((query, args))
        return "INSERT 0 1"

    async def fetchval(self, query: str, *args: Any) -> bool:
        if "FROM conversation.threads" not in query:
            raise AssertionError(f"unexpected fetchval: {query}")
        return self.owns_thread

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "FROM conversation.threads" not in query:
            raise AssertionError(f"unexpected fetchrow: {query}")
        if not self.visible_thread:
            return None
        return {"id": THREAD, "title": "Persisted thread", "updated_at": NOW}


async def finalized_response():
    request = trusted_request(
        authenticated_actor_user_id=ACTOR,
        request_id="persistence-request",
        conversation=messages("Give me one bounded answer."),
    )
    plan = await orchestrator(FixedSafetyProvider()).build_plan(request)
    provider = OpenAIChatCompletionsAdapterV1(
        FakeClient(provider_response(content="A bounded persisted answer."))
    ).complete(plan)
    return finalize_trusted_response_v1(
        trusted_plan=plan,
        provider_response=provider,
        answer_id=ANSWER,
        created_at=NOW,
    )


class ConversationPersistenceGenericTests(unittest.IsolatedAsyncioTestCase):
    async def test_persists_answer_then_attestation_atomically(self) -> None:
        conn = FakeConnection()
        finalized = await finalized_response()

        await persist_conversation_response(
            conn,
            owner_user_id=ACTOR,
            thread_id=THREAD,
            request_id="persistence-request",
            finalized=finalized,
        )

        self.assertEqual((conn.entered, conn.exited), (1, 1))
        sql = "\n".join(query for query, _ in conn.execute_calls)
        self.assertIn("INSERT INTO conversation.chat_log", sql)
        self.assertNotIn("vantage_id", sql)
        self.assertNotIn("'RESSE'", sql)
        self.assertIn("conversation_integrity.assistant_transcript_attestation_v1", sql)
        self.assertNotIn("memory.assistant_transcript_attestation_v1", sql)
        self.assertNotIn("memory.final_answer_memory_binding_v1", sql)
        self.assertIn("UPDATE conversation.threads", sql)
        self.assertIn("INSERT INTO conversation.active_thread_selection", sql)
        self.assertEqual(conn.execute_calls[0][1], (str(ACTOR),))
        chat_call = next(
            call for call in conn.execute_calls if "INSERT INTO conversation.chat_log" in call[0]
        )
        self.assertEqual(chat_call[1][1], ACTOR)
        self.assertEqual(chat_call[1][2], str(ACTOR))
        selection_call = next(
            call
            for call in conn.execute_calls
            if "INSERT INTO conversation.active_thread_selection" in call[0]
        )
        self.assertEqual(selection_call[1], (ACTOR, THREAD))

    async def test_absent_owner_thread_fails_closed(self) -> None:
        conn = FakeConnection(owns_thread=False)
        finalized = await finalized_response()

        with self.assertRaisesRegex(
            ConversationPersistenceError,
            "conversation response persistence failed",
        ) as raised:
            await persist_conversation_response(
                conn,
                owner_user_id=ACTOR,
                thread_id=THREAD,
                request_id="persistence-request",
                finalized=finalized,
            )

        self.assertEqual(raised.exception.stage, "thread_owner_check")

        sql = "\n".join(query for query, _ in conn.execute_calls)
        self.assertNotIn("INSERT INTO conversation.chat_log", sql)

    async def test_request_mismatch_fails_before_transaction(self) -> None:
        conn = FakeConnection()
        finalized = await finalized_response()
        with self.assertRaises(ConversationPersistenceError) as raised:
            await persist_conversation_response(
                conn,
                owner_user_id=ACTOR,
                thread_id=THREAD,
                request_id="different-request",
                finalized=finalized,
            )
        self.assertEqual(raised.exception.stage, "validation")
        self.assertEqual(conn.entered, 0)
        self.assertEqual(conn.execute_calls, [])

    async def test_invisible_thread_rolls_back_response_and_promotion(self) -> None:
        conn = FakeConnection(visible_thread=False)
        finalized = await finalized_response()

        with self.assertRaises(ConversationPersistenceError) as raised:
            await persist_conversation_response(
                conn,
                owner_user_id=ACTOR,
                thread_id=THREAD,
                request_id="persistence-request",
                finalized=finalized,
            )

        self.assertEqual(raised.exception.stage, "resume_target_promotion")
        sql = "\n".join(query for query, _ in conn.execute_calls)
        self.assertNotIn("INSERT INTO conversation.active_thread_selection", sql)


if __name__ == "__main__":
    unittest.main()
