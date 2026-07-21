from __future__ import annotations

import unittest
from typing import Any
from uuid import UUID

from rag_engine.openai_chat_provider_v1 import OpenAIChatCompletionsAdapterV1
from rag_engine.response_finalization_v1 import finalize_trusted_response_v1
from rag_engine.response_persistence_v1 import (
    ResponsePersistenceError,
    persist_finalized_response_v1,
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
    def __init__(self, *, owns_thread: bool = True) -> None:
        self.owns_thread = owns_thread
        self.entered = 0
        self.exited = 0
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append((query, args))
        return "INSERT 0 1"

    async def fetchval(self, query: str, *args: Any) -> bool:
        if "FROM public.threads" not in query:
            raise AssertionError(f"unexpected fetchval: {query}")
        return self.owns_thread


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


class ResponsePersistenceV1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_persists_answer_then_attestation_atomically(self) -> None:
        conn = FakeConnection()
        finalized = await finalized_response()

        await persist_finalized_response_v1(
            conn,
            owner_user_id=ACTOR,
            thread_id=THREAD,
            request_id="persistence-request",
            finalized=finalized,
        )

        self.assertEqual((conn.entered, conn.exited), (1, 1))
        sql = "\n".join(query for query, _ in conn.execute_calls)
        self.assertIn("INSERT INTO public.chat_log", sql)
        self.assertIn("$2::text", sql)
        self.assertIn("memory.assistant_transcript_attestation_v1", sql)
        self.assertNotIn("memory.final_answer_memory_binding_v1", sql)
        self.assertIn("UPDATE public.threads", sql)
        self.assertEqual(conn.execute_calls[0][1], (str(ACTOR),))

    async def test_absent_owner_thread_fails_closed(self) -> None:
        conn = FakeConnection(owns_thread=False)
        finalized = await finalized_response()

        with self.assertRaisesRegex(
            ResponsePersistenceError,
            "finalized response persistence failed",
        ):
            await persist_finalized_response_v1(
                conn,
                owner_user_id=ACTOR,
                thread_id=THREAD,
                request_id="persistence-request",
                finalized=finalized,
            )

        sql = "\n".join(query for query, _ in conn.execute_calls)
        self.assertNotIn("INSERT INTO public.chat_log", sql)


if __name__ == "__main__":
    unittest.main()
