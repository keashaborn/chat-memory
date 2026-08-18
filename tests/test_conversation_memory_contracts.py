from __future__ import annotations

import unittest
from uuid import UUID

from rag_engine.governed_memory.response_provenance import (
    SuccessorMemoryNotApplicableReason,
    build_successor_exposed_provenance_v1,
    build_successor_not_applicable_provenance_v1,
)
from seebx.capabilities.conversation.snapshot import (
    create_current_only_conversation_snapshot_v1,
)
from rag_engine.response_policy_v0_2 import ResponsePolicySignalsV0_2
from rag_engine.response_source_awareness_v1 import MemorySourceStatusV1
from seebx.capabilities.conversation.memory_context import (
    InactiveMemoryContextProviderV1,
    MemoryContextError,
)
from seebx.capabilities.conversation.memory_contracts import (
    MemoryNotApplicableReason,
    build_memory_exposed_provenance_v1,
)


ACTOR = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
OTHER_ACTOR = UUID("2240822d-ac9a-4096-95aa-e2b24d36ef50")
THREAD = UUID("5240822d-ac9a-4096-95aa-e2b24d36ef50")
ANSWER = UUID("90000000-0000-4000-8000-000000000001")
PROMPT_SHA256 = "a" * 64
OUTBOUND = b'{"messages":[]}'


class ConversationMemoryContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_not_applicable_receipt_preserves_legacy_bytes_and_hash(
        self,
    ) -> None:
        legacy = build_successor_not_applicable_provenance_v1(
            reason=SuccessorMemoryNotApplicableReason.NO_STORE,
            answer_id=ANSWER,
            prompt_sha256=PROMPT_SHA256,
            outbound_request_bytes=OUTBOUND,
        )
        current = InactiveMemoryContextProviderV1(
            MemoryNotApplicableReason.NO_STORE
        )
        snapshot = create_current_only_conversation_snapshot_v1(
            authenticated_actor_user_id=ACTOR,
            thread_id=THREAD,
            current_request_id="memory-contract-parity",
            current_message="Do not retain this response.",
        )
        assembly = current.prepare(
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=snapshot,
            trusted_policy_signals=ResponsePolicySignalsV0_2(),
        )
        self.assertIs(assembly.source_status, MemorySourceStatusV1.NOT_APPLICABLE)
        receipt = await current.persist_dispatched_answer_binding(
            answer_id=ANSWER,
            prompt_sha256=PROMPT_SHA256,
            outbound_request_bytes=OUTBOUND,
        )
        self.assertEqual(
            receipt.model_dump(mode="json"),
            legacy.model_dump(mode="json"),
        )
        self.assertEqual(receipt.provenance_sha256, legacy.provenance_sha256)

    def test_exposed_receipt_preserves_legacy_bytes_and_hash(self) -> None:
        binding = {
            "dispatch_state": "dispatched",
            "outcome": "exposed",
            "response_id": str(ANSWER),
            "prompt_sha256": "a" * 64,
            "outbound_request_sha256": "b" * 64,
            "binding_sha256": "c" * 64,
            "selection_manifest_sha256": "d" * 64,
            "injection_manifest_sha256": "c" * 64,
            "selected_count": 1,
            "injected_count": 1,
            "model_exposed_count": 1,
            "injected_claim_ids": ("ffffffff-ffff-4fff-8fff-ffffffffffff",),
            "injected_revision_ids": (
                "12345678-1234-4234-8234-123456789abc",
            ),
        }
        legacy = build_successor_exposed_provenance_v1(binding)
        current = build_memory_exposed_provenance_v1(binding)
        self.assertEqual(
            current.model_dump(mode="json"),
            legacy.model_dump(mode="json"),
        )
        self.assertEqual(current.provenance_sha256, legacy.provenance_sha256)

    def test_inactive_provider_rejects_cross_owner_and_reuse(self) -> None:
        snapshot = create_current_only_conversation_snapshot_v1(
            authenticated_actor_user_id=ACTOR,
            thread_id=THREAD,
            current_request_id="memory-contract-owner",
            current_message="Owner-bound request.",
        )
        provider = InactiveMemoryContextProviderV1(
            MemoryNotApplicableReason.ATTACHMENT
        )
        with self.assertRaisesRegex(
            MemoryContextError,
            "memory_request_binding_mismatch",
        ):
            provider.prepare(
                authenticated_actor_user_id=OTHER_ACTOR,
                conversation_snapshot=snapshot,
                trusted_policy_signals=ResponsePolicySignalsV0_2(),
            )
        provider.prepare(
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=snapshot,
            trusted_policy_signals=ResponsePolicySignalsV0_2(),
        )
        with self.assertRaisesRegex(
            MemoryContextError,
            "memory_context_provider_reused",
        ):
            provider.prepare(
                authenticated_actor_user_id=ACTOR,
                conversation_snapshot=snapshot,
                trusted_policy_signals=ResponsePolicySignalsV0_2(),
            )


if __name__ == "__main__":
    unittest.main()
