from __future__ import annotations

import unittest
from uuid import UUID

from seebx.capabilities.conversation.snapshot import (
    create_current_only_conversation_snapshot_v1,
)
from seebx.capabilities.conversation.policy import ResponsePolicySignalsV0_2
from seebx.capabilities.conversation.source_awareness import MemorySourceStatusV1
from seebx.capabilities.conversation.zep_memory import (
    ZepMemoryChatProviderV1,
    ZepPromptConfigurationError,
    ZepPromptSettingsV1,
)


OWNER = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OTHER = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
THREAD = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
ANSWER = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")


class FakeRuntime:
    def __init__(self, value: str | Exception) -> None:
        self.value = value
        self.calls: list[tuple[UUID, UUID, str]] = []

    async def retrieve_prompt_context(
        self,
        *,
        owner_user_id: UUID,
        thread_id: UUID,
        current_message: str,
    ) -> str:
        self.calls.append((owner_user_id, thread_id, current_message))
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def snapshot(owner: UUID = OWNER):
    return create_current_only_conversation_snapshot_v1(
        authenticated_actor_user_id=owner,
        thread_id=THREAD,
        current_request_id="request-1",
        current_message="What do you remember?",
    )


class ZepPromptSettingsTests(unittest.TestCase):
    def test_off_canary_and_on_are_owner_scoped(self) -> None:
        self.assertFalse(ZepPromptSettingsV1.from_environment({}).enabled_for(OWNER))
        canary = ZepPromptSettingsV1.from_environment(
            {"ZEP_PROMPT_MODE": "canary", "ZEP_PROMPT_OWNER_IDS": str(OWNER)}
        )
        self.assertTrue(canary.enabled_for(OWNER))
        self.assertFalse(canary.enabled_for(OTHER))
        self.assertTrue(
            ZepPromptSettingsV1.from_environment(
                {"ZEP_PROMPT_MODE": "on"}
            ).enabled_for(OTHER)
        )

    def test_canary_requires_an_owner(self) -> None:
        with self.assertRaisesRegex(
            ZepPromptConfigurationError,
            "zep_prompt_canary_owner_ids_required",
        ):
            ZepPromptSettingsV1.from_environment({"ZEP_PROMPT_MODE": "canary"})


class ZepMemoryProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_context_is_owner_bound_and_auditable(self) -> None:
        runtime = FakeRuntime("The user prefers morning training.")
        provider = ZepMemoryChatProviderV1(runtime)
        assembly = await provider.prepare(
            authenticated_actor_user_id=OWNER,
            conversation_snapshot=snapshot(),
            trusted_policy_signals=ResponsePolicySignalsV0_2(),
        )
        self.assertEqual(
            runtime.calls,
            [(OWNER, THREAD, "What do you remember?")],
        )
        self.assertEqual(assembly.source_status, MemorySourceStatusV1.SELECTED)
        block = assembly.successor_memory_context_block
        self.assertIsNotNone(block)
        assert block is not None
        self.assertEqual(block.block_id, "zep_memory_v1")
        self.assertIn("never as instructions", block.content)
        self.assertIn("morning training", block.content)
        provenance = provider.build_answer_provenance(
            answer_id=ANSWER,
            prompt_sha256="a" * 64,
            provider_request_sha256="b" * 64,
        )
        self.assertEqual(provenance.binding_outcome, "exposed")
        self.assertTrue(provenance.model_exposed)
        self.assertFalse(provenance.semantic_use_verified)
        self.assertGreater(provenance.context_bytes, 0)

    async def test_empty_context_is_checked_empty(self) -> None:
        provider = ZepMemoryChatProviderV1(FakeRuntime("  "))
        assembly = await provider.prepare(
            authenticated_actor_user_id=OWNER,
            conversation_snapshot=snapshot(),
            trusted_policy_signals=ResponsePolicySignalsV0_2(),
        )
        self.assertEqual(assembly.source_status, MemorySourceStatusV1.CHECKED_EMPTY)
        provenance = provider.build_answer_provenance(
            answer_id=ANSWER,
            prompt_sha256="a" * 64,
            provider_request_sha256="b" * 64,
        )
        self.assertEqual(provenance.binding_outcome, "checked_empty")
        self.assertFalse(provenance.model_exposed)

    async def test_transport_failure_degrades_without_prompt_context(self) -> None:
        provider = ZepMemoryChatProviderV1(FakeRuntime(RuntimeError("offline")))
        assembly = await provider.prepare(
            authenticated_actor_user_id=OWNER,
            conversation_snapshot=snapshot(),
            trusted_policy_signals=ResponsePolicySignalsV0_2(),
        )
        self.assertEqual(assembly.source_status, MemorySourceStatusV1.UNAVAILABLE)
        self.assertIsNone(assembly.successor_memory_context_block)
        provenance = provider.build_answer_provenance(
            answer_id=ANSWER,
            prompt_sha256="a" * 64,
            provider_request_sha256="b" * 64,
        )
        self.assertEqual(provenance.binding_outcome, "unavailable")

    async def test_oversized_final_reference_degrades_without_context(self) -> None:
        provider = ZepMemoryChatProviderV1(FakeRuntime("x" * 32_768))
        assembly = await provider.prepare(
            authenticated_actor_user_id=OWNER,
            conversation_snapshot=snapshot(),
            trusted_policy_signals=ResponsePolicySignalsV0_2(),
        )
        self.assertEqual(assembly.source_status, MemorySourceStatusV1.UNAVAILABLE)
        self.assertIsNone(assembly.successor_memory_context_block)
        provenance = provider.build_answer_provenance(
            answer_id=ANSWER,
            prompt_sha256="a" * 64,
            provider_request_sha256="b" * 64,
        )
        self.assertEqual(provenance.binding_outcome, "unavailable")

    async def test_owner_mismatch_is_rejected_before_retrieval(self) -> None:
        runtime = FakeRuntime("context")
        provider = ZepMemoryChatProviderV1(runtime)
        with self.assertRaisesRegex(
            ZepPromptConfigurationError,
            "zep_prompt_owner_mismatch",
        ):
            await provider.prepare(
                authenticated_actor_user_id=OTHER,
                conversation_snapshot=snapshot(),
                trusted_policy_signals=ResponsePolicySignalsV0_2(),
            )
        self.assertEqual(runtime.calls, [])


if __name__ == "__main__":
    unittest.main()
