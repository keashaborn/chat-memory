from __future__ import annotations

import unittest
from typing import Any

from seebx.adapters.lifeswitch_openai_chat import OpenAIChatCompletionsAdapterV3
from seebx.capabilities.conversation.lifeswitch_finalization import finalize_trusted_response_v3
from seebx.adapters.conversation_persistence import (
    ConversationPersistenceError,
    persist_conversation_response,
)
from tests.test_lifeswitch_answer_provenance_receipt_v1 import ACTOR, ANSWER, NOW, new_plan
from tests.test_openai_chat_provider_v1 import FakeClient, provider_response
from tests.test_response_orchestration_v0_2 import THREAD


class Transaction:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        return None


class Conn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.binding_manifest = None
        self.receipt_manifest = None

    def transaction(self):
        return Transaction()

    async def execute(self, sql: str, *args: Any):
        self.calls.append((sql, args))
        lower = sql.lower()
        if "final_answer_lifeswitch_binding_v1" in lower and "insert into" in lower:
            self.binding_manifest = args[-1]
        if "final_answer_lifeswitch_provenance_receipt_v1" in lower and "insert into" in lower:
            self.receipt_manifest = args[-1]
        return "OK"

    async def fetchval(self, sql: str, *args: Any):
        lower = sql.lower()
        if "from conversation.threads" in lower:
            return True
        if "final_answer_lifeswitch_provenance_receipt_v1" in lower:
            return self.receipt_manifest
        if "final_answer_lifeswitch_binding_v1" in lower:
            return self.binding_manifest
        raise AssertionError(sql)

    async def fetchrow(self, sql: str, *args: Any):
        if "from conversation.threads" in sql.lower():
            return {"id": THREAD, "title": "Thread", "updated_at": NOW}
        raise AssertionError(sql)


class ConversationPersistenceLifeSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def test_retired_lifeswitch_records_are_not_persisted(self) -> None:
        plan = await new_plan("What were my macros Monday?")
        response = OpenAIChatCompletionsAdapterV3(
            FakeClient(provider_response(content="Monday used your LifeSwitch log."))
        ).complete(plan)
        finalized = finalize_trusted_response_v3(
            trusted_plan=plan,
            provider_response=response,
            answer_id=ANSWER,
            created_at=NOW,
        )
        conn = Conn()
        await persist_conversation_response(
            conn,
            owner_user_id=ACTOR,
            thread_id=THREAD,
            request_id="request-123",
            finalized=finalized,
        )
        sql = "\n".join(item[0].lower() for item in conn.calls)
        self.assertNotIn("final_answer_lifeswitch_binding_v1", sql)
        self.assertNotIn("final_answer_lifeswitch_provenance_receipt_v1", sql)
        self.assertIn("conversation_integrity.assistant_transcript_attestation_v1", sql)
        self.assertNotIn("memory.assistant_transcript_attestation_v1", sql)

    async def test_request_mismatch_fails_before_transaction(self) -> None:
        plan = await new_plan("What were my macros Monday?")
        response = OpenAIChatCompletionsAdapterV3(
            FakeClient(provider_response(content="Monday used your LifeSwitch log."))
        ).complete(plan)
        finalized = finalize_trusted_response_v3(
            trusted_plan=plan,
            provider_response=response,
            answer_id=ANSWER,
            created_at=NOW,
        )
        conn = Conn()
        with self.assertRaises(ConversationPersistenceError) as raised:
            await persist_conversation_response(
                conn,
                owner_user_id=ACTOR,
                thread_id=THREAD,
                request_id="different-request",
                finalized=finalized,
            )
        self.assertEqual(raised.exception.stage, "validation")
        self.assertEqual(conn.calls, [])


if __name__ == "__main__":
    unittest.main()
