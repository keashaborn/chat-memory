from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID

from seebx.adapters.lifeswitch_openai_chat import OpenAIChatCompletionsAdapterV3
from seebx.capabilities.conversation.lifeswitch_finalization import finalize_trusted_response_v3
from seebx.adapters.conversation_persistence import (
    ConversationPersistenceError,
    persist_conversation_response,
)
from tests.test_zep_sync_postgres import USER_MESSAGE
from tests.test_lifeswitch_answer_provenance_receipt_v1 import (
    ACTOR,
    ANSWER,
    NOW,
    new_plan,
)
from tests.test_openai_chat_provider_v1 import FakeClient, provider_response
from tests.test_response_orchestration_v0_2 import THREAD
from tests.test_conversation_persistence_generic import (
    FakeConnection,
    finalized_response,
)
from tests.test_conversation_persistence_lifeswitch import Conn


OTHER_ACTOR = UUID("80000000-0000-4000-8000-000000000099")


async def finalized_lifeswitch_response():
    plan = await new_plan("What were my macros Monday?")
    response = OpenAIChatCompletionsAdapterV3(
        FakeClient(provider_response(content="Monday used your LifeSwitch log."))
    ).complete(plan)
    return finalize_trusted_response_v3(
        trusted_plan=plan,
        provider_response=response,
        answer_id=ANSWER,
        created_at=NOW,
    )


class ConversationPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_generic_and_lifeswitch_responses_share_one_transaction(self) -> None:
        generic_conn = FakeConnection()
        await persist_conversation_response(
            generic_conn,
            owner_user_id=ACTOR,
            thread_id=THREAD,
            request_id="persistence-request",
            finalized=await finalized_response(),
        )
        generic_chat_call = next(
            call
            for call in generic_conn.execute_calls
            if "INSERT INTO public.chat_log" in call[0]
        )
        self.assertEqual(
            generic_chat_call[1][5],
            ["assistant", "chat", "server_attested", "resse_v0_2"],
        )

        lifeswitch_conn = Conn()
        with patch(
            "seebx.adapters.conversation_persistence.enqueue_zep_turn",
            new_callable=AsyncMock,
        ) as enqueue:
            await persist_conversation_response(
                lifeswitch_conn,
                owner_user_id=ACTOR,
                thread_id=THREAD,
                request_id="request-123",
                finalized=await finalized_lifeswitch_response(),
                zep_sync_user_message_id=USER_MESSAGE,
            )
        enqueue.assert_awaited_once_with(
            lifeswitch_conn,
            owner_user_id=ACTOR,
            thread_id=THREAD,
            user_message_id=USER_MESSAGE,
            assistant_message_id=ANSWER,
        )
        lifeswitch_chat_call = next(
            call
            for call in lifeswitch_conn.calls
            if "INSERT INTO public.chat_log" in call[0]
        )
        self.assertEqual(
            lifeswitch_chat_call[1][5],
            ["assistant", "chat", "server_attested", "response_v3"],
        )

        for calls in (generic_conn.execute_calls, lifeswitch_conn.calls):
            sql = "\n".join(query for query, _ in calls)
            self.assertEqual(sql.count("INSERT INTO public.chat_log"), 1)
            self.assertEqual(
                sql.count("chat_integrity.assistant_transcript_attestation_v1"),
                1,
            )
            self.assertNotIn("final_answer_lifeswitch_binding_v1", sql)
            self.assertNotIn("final_answer_lifeswitch_provenance_receipt_v1", sql)

    async def test_lifeswitch_owner_mismatch_fails_before_transaction(self) -> None:
        conn = Conn()
        with self.assertRaises(ConversationPersistenceError) as raised:
            await persist_conversation_response(
                conn,
                owner_user_id=OTHER_ACTOR,
                thread_id=THREAD,
                request_id="request-123",
                finalized=await finalized_lifeswitch_response(),
            )
        self.assertEqual(raised.exception.stage, "validation")
        self.assertEqual(conn.calls, [])

    async def test_unknown_finalization_contract_fails_closed(self) -> None:
        conn = Conn()
        with self.assertRaises(ConversationPersistenceError) as raised:
            await persist_conversation_response(
                conn,
                owner_user_id=ACTOR,
                thread_id=THREAD,
                request_id="request-123",
                finalized=object(),  # type: ignore[arg-type]
            )
        self.assertEqual(raised.exception.stage, "validation")
        self.assertEqual(conn.calls, [])


if __name__ == "__main__":
    unittest.main()
