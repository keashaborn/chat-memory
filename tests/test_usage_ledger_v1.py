from __future__ import annotations

import unittest
from typing import Any
from uuid import UUID

from rag_engine.openai_chat_provider_v1 import OpenAIChatCompletionsAdapterV1
from rag_engine.usage_ledger_v1 import (
    AdminUsageSummaryRequestV1,
    UsageLedgerError,
    persist_openai_chat_usage_v1,
)
from tests.test_openai_chat_provider_v1 import FakeClient, provider_response
from tests.test_response_orchestration_v0_2 import (
    ACTOR,
    FixedSafetyProvider,
    messages,
    orchestrator,
    trusted_request,
)


ANSWER = UUID("90000000-0000-4000-8000-000000000088")


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


class FakeConnection:
    def __init__(
        self,
        *,
        insert_row: dict[str, Any] | None,
        existing_row: dict[str, Any] | None = None,
    ) -> None:
        self.insert_row = insert_row
        self.existing_row = existing_row
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append((query, args))
        return "SELECT 1"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrow_calls.append((query, args))
        if "insert into lifeswitch_usage.ai_usage_event_v1" in query:
            return self.insert_row
        return self.existing_row


async def chat_response():
    plan = await orchestrator(FixedSafetyProvider()).build_plan(
        trusted_request(
            authenticated_actor_user_id=ACTOR,
            request_id="usage-ledger-request",
            conversation=messages("Give me a concise response."),
        )
    )
    raw = provider_response(prompt_tokens=120, completion_tokens=20)
    raw["usage"]["prompt_tokens_details"] = {"cached_tokens": 40}
    raw["usage"]["completion_tokens_details"] = {"reasoning_tokens": 12}
    return OpenAIChatCompletionsAdapterV1(FakeClient(raw)).complete(plan)


class UsageLedgerV1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_persists_content_free_exact_provider_usage(self) -> None:
        response = await chat_response()
        conn = FakeConnection(insert_row={"ai_usage_event_id": ANSWER})

        await persist_openai_chat_usage_v1(
            conn,
            owner_user_id=ACTOR,
            answer_id=ANSWER,
            source_channel="chat",
            provider_response=response,
        )

        self.assertEqual(conn.execute_calls[0][1], (str(ACTOR),))
        insert = conn.fetchrow_calls[0]
        self.assertIn("lifeswitch_usage.ai_usage_event_v1", insert[0])
        self.assertNotIn(response.content or "", insert[0])
        self.assertEqual(
            insert[1],
            (
                ACTOR,
                ANSWER,
                "chat",
                response.response_id,
                response.requested_model,
                response.model,
                120,
                40,
                20,
                12,
                140,
            ),
        )

    async def test_conflicting_provider_response_fails_closed(self) -> None:
        response = await chat_response()
        conn = FakeConnection(
            insert_row=None,
            existing_row={
                "owner_user_id": ACTOR,
                "answer_id": ANSWER,
                "source_channel": "chat",
                "provider_response_id": response.response_id,
                "requested_model": response.requested_model,
                "returned_model": response.model,
                "input_tokens": 121,
                "cached_input_tokens": 40,
                "output_tokens": 20,
                "reasoning_output_tokens": 12,
                "total_tokens": 141,
            },
        )
        with self.assertRaisesRegex(
            UsageLedgerError,
            "provider response usage conflict",
        ):
            await persist_openai_chat_usage_v1(
                conn,
                owner_user_id=ACTOR,
                answer_id=ANSWER,
                source_channel="chat",
                provider_response=response,
            )

    async def test_rejects_unknown_source_channel(self) -> None:
        response = await chat_response()
        with self.assertRaisesRegex(UsageLedgerError, "invalid source channel"):
            await persist_openai_chat_usage_v1(
                FakeConnection(insert_row={}),
                owner_user_id=ACTOR,
                answer_id=ANSWER,
                source_channel="background",
                provider_response=response,
            )

    def test_admin_summary_request_is_bounded_and_unique(self) -> None:
        valid = AdminUsageSummaryRequestV1(
            target_user_ids=[ACTOR],
            window_days=90,
        )
        self.assertEqual(valid.window_days, 90)
        with self.assertRaises(ValueError):
            AdminUsageSummaryRequestV1(
                target_user_ids=[ACTOR, ACTOR],
                window_days=30,
            )
        with self.assertRaises(ValueError):
            AdminUsageSummaryRequestV1(
                target_user_ids=[ACTOR],
                window_days=365,
            )


if __name__ == "__main__":
    unittest.main()
