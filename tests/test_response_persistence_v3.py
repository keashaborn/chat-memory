from __future__ import annotations

import unittest
from typing import Any

from rag_engine.openai_chat_request_v4 import OpenAIChatCompletionsAdapterV3
from rag_engine.response_finalization_v3 import finalize_trusted_response_v3
from rag_engine.response_persistence_v3 import persist_finalized_response_v3
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
        if "from public.threads" in lower:
            return True
        if "final_answer_lifeswitch_provenance_receipt_v1" in lower:
            return self.receipt_manifest
        if "final_answer_lifeswitch_binding_v1" in lower:
            return self.binding_manifest
        raise AssertionError(sql)

    async def fetchrow(self, sql: str, *args: Any):
        if "from public.threads" in sql.lower():
            return {"id": THREAD, "title": "Thread", "updated_at": NOW}
        raise AssertionError(sql)


class ResponsePersistenceV3Tests(unittest.IsolatedAsyncioTestCase):
    async def test_receipt_is_persisted_in_same_transaction_after_binding(self) -> None:
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
        await persist_finalized_response_v3(
            conn,
            owner_user_id=ACTOR,
            thread_id=THREAD,
            request_id="request-123",
            finalized=finalized,
        )
        sql = "\n".join(item[0].lower() for item in conn.calls)
        self.assertLess(
            sql.index("final_answer_lifeswitch_binding_v1"),
            sql.index("final_answer_lifeswitch_provenance_receipt_v1"),
        )
        self.assertIn("assistant_transcript_attestation_v1", sql)


if __name__ == "__main__":
    unittest.main()
