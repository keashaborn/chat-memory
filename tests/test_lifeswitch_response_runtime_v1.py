from __future__ import annotations

import asyncio
import unittest
from typing import Any
from uuid import UUID

from rag_engine.openai_chat_request_v3 import (
    OpenAIChatCompletionsAdapterV2,
    OpenAIChatRequestV3,
)
from rag_engine.lifeswitch_response_context_provider_v1 import (
    LifeSwitchResponseContextProviderV1,
)
from rag_engine.response_composition_root_v0_3 import (
    InactiveLifeSwitchResponseCompositionRootV0_3,
)
from rag_engine.response_conversation_snapshot_v1 import (
    create_current_only_conversation_snapshot_v1,
)
from rag_engine.response_finalization_v2 import (
    LifeSwitchResponseFinalizationError,
    finalize_trusted_response_v2,
)
from rag_engine.response_lifeswitch_integration_v1 import (
    TrustedLifeSwitchResponsePlanV1,
)
from rag_engine.response_persistence_v2 import (
    ResponsePersistenceV2Error,
    persist_finalized_response_v2,
)
from tests.test_openai_chat_provider_v1 import FakeClient, provider_response
from tests.test_prompt_assembler_v1 import governed_memory
from tests.test_response_lifeswitch_integration_v1 import selected_context
from tests.test_response_orchestration_v0_2 import (
    ACTOR,
    NOW,
    THREAD,
    FixedSafetyProvider,
    messages,
    orchestrator,
    trusted_request,
)


ANSWER = UUID("90000000-0000-4000-8000-000000000021")


async def response_plan(*, with_memory: bool = False):
    message = "Was I low on protein Monday?"
    memory_input = None
    memory_application = None
    if with_memory:
        memory_input, memory_application = await asyncio.to_thread(
            governed_memory,
            message,
        )
    base = await orchestrator(FixedSafetyProvider()).build_plan(
        trusted_request(
            authenticated_actor_user_id=ACTOR,
            request_id="request-123",
            conversation=messages(message),
            memory_input=memory_input,
            memory_application=memory_application,
        )
    )
    return TrustedLifeSwitchResponsePlanV1.create(
        base_response_plan=base,
        lifeswitch_context=selected_context(base, message),
    )


async def base_plan(message: str):
    return await orchestrator(FixedSafetyProvider()).build_plan(
        trusted_request(
            authenticated_actor_user_id=ACTOR,
            request_id="request-123",
            conversation=messages(message),
        )
    )


def snapshot(message: str):
    return create_current_only_conversation_snapshot_v1(
        authenticated_actor_user_id=ACTOR,
        thread_id=THREAD,
        current_request_id="request-123",
        current_message=message,
    )


class NeverReadSession:
    def __init__(self) -> None:
        self.calls = 0

    async def select(self, **kwargs: Any):
        self.calls += 1
        raise AssertionError("OFF must not open a LifeSwitch read session")


class FixedContextProvider:
    def __init__(self, value: Any) -> None:
        self.value = value

    async def prepare(self, **kwargs: Any):
        return self.value


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
        self.binding_hash: str | None = None

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append((query, args))
        if (
            "final_answer_lifeswitch_binding_v1" in query
            and "insert" in query.lower()
        ):
            self.binding_hash = args[-1]
        return "INSERT 0 1"

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "FROM public.threads" in query:
            return self.owns_thread
        if "final_answer_lifeswitch_binding_v1" in query:
            return self.binding_hash
        raise AssertionError(f"unexpected fetchval: {query}")

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        if "FROM public.threads" not in query:
            raise AssertionError(f"unexpected fetchrow: {query}")
        return {"id": THREAD, "title": "Test thread", "updated_at": NOW}


class LifeSwitchResponseRuntimeV1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_adapter_binds_exact_v3_request_and_usage(self) -> None:
        plan = await response_plan()
        expected = OpenAIChatRequestV3.create(source_plan=plan)
        client = FakeClient(
            provider_response(
                content="Monday was low in protein.",
                prompt_tokens=200,
                completion_tokens=9,
            )
        )

        result = OpenAIChatCompletionsAdapterV2(client).complete(plan)

        self.assertEqual(result.provider_request_sha256, expected.request_sha256)
        self.assertEqual(result.content, "Monday was low in protein.")
        self.assertEqual(result.provider_total_tokens, 209)
        self.assertIs(client.completions.calls[0]["store"], False)
        names = tuple(
            item.get("name")
            for item in client.completions.calls[0]["messages"]
            if item.get("name")
        )
        self.assertEqual(names, ("lifeswitch_domain_context_v1",))

    async def test_finalization_keeps_memory_and_lifeswitch_separate(self) -> None:
        plan = await response_plan(with_memory=True)
        response = OpenAIChatCompletionsAdapterV2(
            FakeClient(provider_response(content="Use the recorded Monday totals."))
        ).complete(plan)

        finalized = finalize_trusted_response_v2(
            trusted_plan=plan,
            provider_response=response,
            answer_id=ANSWER,
            created_at=NOW,
        )

        self.assertIsNotNone(finalized.memory_binding)
        self.assertIsNotNone(finalized.lifeswitch_binding)
        assert finalized.lifeswitch_binding is not None
        self.assertEqual(finalized.lifeswitch_binding.record_count, 1)
        self.assertNotEqual(
            finalized.memory_binding.binding_manifest_sha256,
            finalized.lifeswitch_binding.binding_manifest_sha256,
        )

    async def test_finalization_rejects_response_from_another_plan(self) -> None:
        first = await response_plan()
        second_message = "Was I low on protein Monday?"
        second_base = await orchestrator(FixedSafetyProvider()).build_plan(
            trusted_request(
                authenticated_actor_user_id=ACTOR,
                request_id="request-other",
                conversation=messages(second_message),
            )
        )
        second = TrustedLifeSwitchResponsePlanV1.create(
            base_response_plan=second_base,
            lifeswitch_context=selected_context(second_base, second_message),
        )
        response = OpenAIChatCompletionsAdapterV2(
            FakeClient(provider_response(content="First response."))
        ).complete(first)

        with self.assertRaises(LifeSwitchResponseFinalizationError):
            finalize_trusted_response_v2(
                trusted_plan=second,
                provider_response=response,
                answer_id=ANSWER,
                created_at=NOW,
            )

    async def test_persistence_writes_separate_bindings_in_one_transaction(self) -> None:
        plan = await response_plan(with_memory=True)
        response = OpenAIChatCompletionsAdapterV2(
            FakeClient(provider_response(content="Use the recorded totals."))
        ).complete(plan)
        finalized = finalize_trusted_response_v2(
            trusted_plan=plan,
            provider_response=response,
            answer_id=ANSWER,
            created_at=NOW,
        )
        conn = FakeConnection()

        await persist_finalized_response_v2(
            conn,
            owner_user_id=ACTOR,
            thread_id=THREAD,
            request_id="request-123",
            finalized=finalized,
        )

        sql = "\n".join(query for query, _ in conn.execute_calls)
        self.assertEqual((conn.entered, conn.exited), (1, 1))
        self.assertIn("memory.final_answer_memory_binding_v1", sql)
        self.assertIn("lifeswitch_chat.final_answer_lifeswitch_binding_v1", sql)
        self.assertIn("set local role lifeswitch_chat_binding_writer_v1", sql.lower())
        self.assertIn("reset role", sql.lower())

    async def test_persistence_fails_before_writes_for_wrong_request(self) -> None:
        plan = await response_plan()
        response = OpenAIChatCompletionsAdapterV2(
            FakeClient(provider_response(content="A bounded answer."))
        ).complete(plan)
        finalized = finalize_trusted_response_v2(
            trusted_plan=plan,
            provider_response=response,
            answer_id=ANSWER,
            created_at=NOW,
        )
        conn = FakeConnection()

        with self.assertRaises(ResponsePersistenceV2Error) as raised:
            await persist_finalized_response_v2(
                conn,
                owner_user_id=ACTOR,
                thread_id=THREAD,
                request_id="wrong-request",
                finalized=finalized,
            )

        self.assertEqual(raised.exception.stage, "validation")
        self.assertEqual(conn.execute_calls, [])

    async def test_composition_off_performs_zero_lifeswitch_database_reads(self) -> None:
        message = "Who won America's Next Top Model in 2015?"
        base = await base_plan(message)
        session = NeverReadSession()
        context_provider = LifeSwitchResponseContextProviderV1(session)
        root = InactiveLifeSwitchResponseCompositionRootV0_3(
            openai_client=FakeClient(
                provider_response(content="The structured LifeSwitch path was not used.")
            ),
            context_provider=context_provider,
            clock=lambda: NOW,
            answer_id_factory=lambda: ANSWER,
        )

        execution = await root.execute(
            base_response_plan=base,
            conversation_snapshot=snapshot(message),
        )

        self.assertEqual(session.calls, 0)
        self.assertEqual(execution.trusted_plan.lifeswitch_context.status, "OFF")
        self.assertIsNone(execution.finalized.lifeswitch_binding)
        names = tuple(
            item.name
            for item in OpenAIChatRequestV3.create(
                source_plan=execution.trusted_plan
            ).messages
            if item.name is not None
        )
        self.assertNotIn("lifeswitch_domain_context_v1", names)

    async def test_composition_selected_context_is_bound_to_answer(self) -> None:
        message = "Was I low on protein Monday?"
        base = await base_plan(message)
        context = selected_context(base, message)
        root = InactiveLifeSwitchResponseCompositionRootV0_3(
            openai_client=FakeClient(
                provider_response(content="Monday's recorded protein was 142 grams.")
            ),
            context_provider=FixedContextProvider(context),
            clock=lambda: NOW,
            answer_id_factory=lambda: ANSWER,
        )

        execution = await root.execute(
            base_response_plan=base,
            conversation_snapshot=snapshot(message),
        )

        self.assertIsNotNone(execution.finalized.lifeswitch_binding)
        self.assertEqual(
            execution.trusted_plan.assembled_prompt.manifest.lifeswitch_record_count,
            1,
        )
        self.assertGreaterEqual(execution.stage_timings.pipeline_total_ms, 0)


if __name__ == "__main__":
    unittest.main()
