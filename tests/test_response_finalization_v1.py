from __future__ import annotations

import asyncio
import unittest
from uuid import UUID

from rag_engine.openai_chat_provider_v1 import OpenAIChatCompletionsAdapterV1
from rag_engine.response_finalization_v1 import (
    AssistantOutputKind,
    ResponseFinalizationError,
    finalize_trusted_response_v1,
)
from tests.test_openai_chat_provider_v1 import FakeClient, provider_response
from tests.test_prompt_assembler_v1 import governed_memory
from tests.test_response_orchestration_v0_2 import (
    ACTOR,
    NOW,
    FixedSafetyProvider,
    messages,
    orchestrator,
    trusted_request,
)


ANSWER = UUID("90000000-0000-4000-8000-000000000001")


class ResponseFinalizationV1Tests(unittest.IsolatedAsyncioTestCase):
    async def plan(self, message: str, *, with_memory: bool = False):
        memory_input = None
        memory_application = None
        if with_memory:
            memory_input, memory_application = await asyncio.to_thread(
                governed_memory, message
            )
        request = trusted_request(
            authenticated_actor_user_id=ACTOR,
            request_id=("request-123" if with_memory else "finalization-request"),
            conversation=messages(message),
            memory_input=memory_input,
            memory_application=memory_application,
        )
        return await orchestrator(FixedSafetyProvider()).build_plan(request)

    async def test_attests_exact_provider_output_without_raw_text_in_attestation(self) -> None:
        plan = await self.plan("What should I prioritize today?")
        provider = OpenAIChatCompletionsAdapterV1(
            FakeClient(provider_response(content="Do the smallest useful action."))
        ).complete(plan)

        finalized = finalize_trusted_response_v1(
            trusted_plan=plan,
            provider_response=provider,
            answer_id=ANSWER,
            created_at=NOW,
        )

        self.assertEqual(finalized.output_kind, AssistantOutputKind.CONTENT)
        self.assertEqual(finalized.assistant_text, "Do the smallest useful action.")
        self.assertEqual(finalized.attestation.answer_id, ANSWER)
        self.assertEqual(finalized.attestation.trusted_plan_sha256, plan.plan_sha256)
        self.assertEqual(
            finalized.attestation.provider_response_sha256,
            provider.response_sha256,
        )
        self.assertNotIn(finalized.assistant_text, finalized.attestation.model_dump_json())
        self.assertIsNone(finalized.memory_binding)

    async def test_binds_injected_memory_as_answer_model_exposed(self) -> None:
        message = "What is the relevant project constraint?"
        plan = await self.plan(message, with_memory=True)
        provider = OpenAIChatCompletionsAdapterV1(
            FakeClient(provider_response(content="Use the governed constraint."))
        ).complete(plan)

        finalized = finalize_trusted_response_v1(
            trusted_plan=plan,
            provider_response=provider,
            answer_id=ANSWER,
            created_at=NOW,
        )

        self.assertIsNotNone(finalized.memory_binding)
        binding = finalized.memory_binding
        assert binding is not None
        application = plan.assembled_prompt.source_request.memory_application
        assert application is not None
        self.assertEqual(binding.injected_count, len(application.injected_records))
        self.assertEqual(binding.exposed_count, len(application.injected_records))
        self.assertEqual(binding.outcome, "exposed")

    async def test_rejects_response_bound_to_another_plan(self) -> None:
        first = await self.plan("First question")
        second = await self.plan("Second question")
        response = OpenAIChatCompletionsAdapterV1(
            FakeClient(provider_response(content="First answer"))
        ).complete(first)

        with self.assertRaises(ResponseFinalizationError):
            finalize_trusted_response_v1(
                trusted_plan=second,
                provider_response=response,
                answer_id=ANSWER,
                created_at=NOW,
            )


if __name__ == "__main__":
    unittest.main()
