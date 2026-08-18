from __future__ import annotations

import unittest
from uuid import UUID

from seebx.adapters.openai_chat import OpenAIChatCompletionsAdapterV1
from seebx.capabilities.conversation.finalization import (
    AssistantOutputKind,
    ResponseFinalizationError,
    finalize_trusted_response_v1,
)
from seebx.contracts.search import (
    TEXT_SEARCH_AUTHORIZATION_BASIS,
    SearchCapabilityManifestV1,
)
from tests.test_openai_chat_provider_v1 import FakeClient, provider_response
from tests.test_prompt_assembler_v1 import successor_memory_block
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
    async def plan(
        self,
        message: str,
        *,
        with_successor_memory: bool = False,
        with_search_capability: bool = False,
    ):
        request = trusted_request(
            authenticated_actor_user_id=ACTOR,
            request_id=(
                "request-123" if with_successor_memory else "finalization-request"
            ),
            conversation=messages(message),
            successor_memory_context_block=(
                successor_memory_block(message)
                if with_successor_memory
                else None
            ),
            search_capability_manifest=(
                SearchCapabilityManifestV1.create(
                    authorization_basis=TEXT_SEARCH_AUTHORIZATION_BASIS,
                )
                if with_search_capability
                else None
            ),
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
        self.assertNotIn("memory_binding", finalized.model_dump(mode="json"))

    async def test_successor_memory_context_has_no_legacy_binding(self) -> None:
        message = "What is the relevant project constraint?"
        plan = await self.plan(message, with_successor_memory=True)
        provider = OpenAIChatCompletionsAdapterV1(
            FakeClient(provider_response(content="Use the governed constraint."))
        ).complete(plan)

        finalized = finalize_trusted_response_v1(
            trusted_plan=plan,
            provider_response=provider,
            answer_id=ANSWER,
            created_at=NOW,
        )

        self.assertNotIn("memory_binding", finalized.model_dump(mode="json"))

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

    async def test_search_manifest_rejects_overbroad_capability_answer(self) -> None:
        plan = await self.plan(
            "What can you research?",
            with_search_capability=True,
        )
        response = OpenAIChatCompletionsAdapterV1(
            FakeClient(
                provider_response(
                    content=(
                        "I can also perform other supported fact-checking or "
                        "source-verification tasks."
                    )
                )
            )
        ).complete(plan)

        with self.assertRaises(ResponseFinalizationError):
            finalize_trusted_response_v1(
                trusted_plan=plan,
                provider_response=response,
                answer_id=ANSWER,
                created_at=NOW,
            )

    async def test_no_manifest_preserves_ordinary_answer_behavior(self) -> None:
        plan = await self.plan("Repeat this sentence.")
        response = OpenAIChatCompletionsAdapterV1(
            FakeClient(
                provider_response(
                    content=(
                        "Other supported fact-checking or source-verification "
                        "tasks."
                    )
                )
            )
        ).complete(plan)

        finalized = finalize_trusted_response_v1(
            trusted_plan=plan,
            provider_response=response,
            answer_id=ANSWER,
            created_at=NOW,
        )
        self.assertIn("fact-checking", finalized.assistant_text)


if __name__ == "__main__":
    unittest.main()
