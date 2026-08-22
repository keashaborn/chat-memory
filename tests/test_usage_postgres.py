from __future__ import annotations

import unittest
from typing import Any
from uuid import UUID

from seebx.adapters.lifeswitch_openai_chat import OpenAIChatCompletionsAdapterV3
from seebx.adapters.openai_chat import OpenAIChatCompletionsAdapterV1
from seebx.adapters.usage_postgres import (
    USAGE_EVENT_TABLE,
    USAGE_WRITER_ROLE,
    UsagePersistenceError,
    persist_openai_chat_usage,
)
from tests.test_lifeswitch_answer_provenance_receipt_v1 import new_plan
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
        if "insert into usage.ai_usage_event_v1" in query:
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


async def chat_response_v3():
    plan = await new_plan("Was I low on protein Monday?")
    raw = provider_response(prompt_tokens=120, completion_tokens=20)
    raw["usage"]["prompt_tokens_details"] = {"cached_tokens": 40}
    raw["usage"]["completion_tokens_details"] = {"reasoning_tokens": 12}
    return OpenAIChatCompletionsAdapterV3(FakeClient(raw)).complete(plan)


class UsagePostgresTests(unittest.IsolatedAsyncioTestCase):
    def test_uses_clean_platform_database_contract(self) -> None:
        self.assertEqual(USAGE_EVENT_TABLE, "usage.ai_usage_event_v1")
        self.assertEqual(USAGE_WRITER_ROLE, "seebx_usage_writer_v1")

    async def test_v3_response_uses_the_same_canonical_writer(self) -> None:
        response = await chat_response_v3()
        conn = FakeConnection(insert_row={"ai_usage_event_id": ANSWER})

        await persist_openai_chat_usage(
            conn,
            owner_user_id=ACTOR,
            answer_id=ANSWER,
            source_channel="chat",
            provider_response=response,
        )

        insert = conn.fetchrow_calls[0]
        self.assertEqual(insert[1][4], response.response_id)
        self.assertEqual(insert[1][7:12], (120, 40, 20, 12, 140))

    async def test_persists_content_free_exact_provider_usage(self) -> None:
        response = await chat_response()
        conn = FakeConnection(insert_row={"ai_usage_event_id": ANSWER})

        await persist_openai_chat_usage(
            conn,
            owner_user_id=ACTOR,
            answer_id=ANSWER,
            source_channel="chat",
            provider_response=response,
        )

        self.assertIn("set local role seebx_usage_writer_v1", conn.execute_calls[0][0])
        self.assertEqual(conn.execute_calls[1][1], (str(ACTOR),))
        insert = conn.fetchrow_calls[0]
        self.assertIn("usage.ai_usage_event_v1", insert[0])
        self.assertNotIn("lifeswitch_usage", insert[0])
        self.assertNotIn(response.content or "", insert[0])
        self.assertEqual(
            insert[1],
            (
                ACTOR,
                ANSWER,
                "openai_chat_completions_v1",
                "chat",
                response.response_id,
                response.requested_model,
                response.model,
                120,
                40,
                20,
                12,
                140,
                str(ANSWER),
                1,
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
                "helper": "openai_chat_completions_v1",
                "idempotency_key": str(ANSWER),
                "event_schema_version": 1,
            },
        )
        with self.assertRaisesRegex(
            UsagePersistenceError,
            "provider response usage conflict",
        ):
            await persist_openai_chat_usage(
                conn,
                owner_user_id=ACTOR,
                answer_id=ANSWER,
                source_channel="chat",
                provider_response=response,
            )

    async def test_rejects_unknown_source_channel(self) -> None:
        response = await chat_response()
        with self.assertRaisesRegex(
            UsagePersistenceError,
            "invalid source channel",
        ):
            await persist_openai_chat_usage(
                FakeConnection(insert_row={}),
                owner_user_id=ACTOR,
                answer_id=ANSWER,
                source_channel="background",
                provider_response=response,
            )

    async def test_rejects_non_provider_response(self) -> None:
        with self.assertRaisesRegex(
            UsagePersistenceError,
            "OpenAI usage response is invalid",
        ):
            await persist_openai_chat_usage(
                FakeConnection(insert_row={}),
                owner_user_id=ACTOR,
                answer_id=ANSWER,
                source_channel="chat",
                provider_response=object(),  # type: ignore[arg-type]
            )
