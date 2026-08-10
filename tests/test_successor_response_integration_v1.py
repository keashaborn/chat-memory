from __future__ import annotations

import hashlib
import json
import math
import unittest
from uuid import UUID

from rag_engine.governed_memory.contracts import canonical_json_bytes
from rag_engine.openai_chat_provider_v1 import OpenAIChatRequestV1
from rag_engine.openai_chat_request_v4 import OpenAIChatRequestV4
from rag_engine.prompt_assembler_v1 import (
    ContextKind,
    PromptReferenceContextBlockV1,
    PromptReferenceFragmentV1,
)
from rag_engine.response_composition_root_v0_2 import (
    GovernedMemoryAssemblyV1,
    InactiveResponseCompositionRootV0_2,
)
from rag_engine.response_composition_root_v0_4 import (
    IntegratedLifeSwitchResponseCompositionRootV0_4,
)
from rag_engine.response_lifeswitch_integration_v2 import (
    TrustedLifeSwitchResponsePlanV2,
)
from tests.test_lifeswitch_answer_provenance_receipt_v1 import off_prior
from tests.test_response_composition_root_v0_4 import CurrentProvider, PriorProvider
from tests.test_response_composition_root_v0_2 import (
    ANSWER,
    CORRELATION,
    CombinedOpenAIClient,
    SnapshotConn,
    command,
)
from tests.test_response_lifeswitch_integration_v1 import selected_context
from tests.test_response_orchestration_v0_2 import (
    ACTOR,
    FixedSafetyProvider,
    messages,
    orchestrator,
    trusted_request,
)


SUCCESSOR_CONTENT = json.dumps(
    {
        "contract_version": "governed-memory-answer-context-v1",
        "facts": [
            {
                "claim_id": "ffffffff-ffff-4fff-8fff-ffffffffffff",
                "fact": "Synthetic owner prefers the cobalt interface theme.",
            }
        ],
    },
    ensure_ascii=False,
    allow_nan=False,
    separators=(",", ":"),
    sort_keys=True,
)


def successor_block(request_id: str, query: str) -> PromptReferenceContextBlockV1:
    raw = SUCCESSOR_CONTENT.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    return PromptReferenceContextBlockV1(
        block_id="governed_memory_successor_v1",
        kind=ContextKind.MEMORY,
        source_contract_version="governed-memory-answer-context-v1",
        source_manifest_sha256=digest,
        request_id_sha256=hashlib.sha256(request_id.encode("utf-8")).hexdigest(),
        query_sha256=hashlib.sha256(query.encode("utf-8")).hexdigest(),
        content=SUCCESSOR_CONTENT,
        content_sha256=digest,
        content_bytes=len(raw),
        estimated_tokens=math.ceil(len(raw) / 4),
        fragments=(
            PromptReferenceFragmentV1(
                ordinal=0,
                byte_offset=0,
                byte_length=len(raw),
                content_sha256=digest,
                estimated_tokens=math.ceil(len(raw) / 4),
            ),
        ),
    )


async def successor_base_plan(query: str):
    request_id = "successor-provider-payload"
    return await orchestrator(FixedSafetyProvider()).build_plan(
        trusted_request(
            authenticated_actor_user_id=ACTOR,
            request_id=request_id,
            conversation=messages(query),
            successor_memory_context_block=successor_block(request_id, query),
        )
    )


class OneRequestSuccessorLifecycle:
    def __init__(self) -> None:
        self.selected = False
        self.persisted: list[dict[str, object]] = []
        self.discards = 0

    @property
    def has_selected_claims(self) -> bool:
        return self.selected

    def prepare(self, **kwargs: object) -> GovernedMemoryAssemblyV1:
        snapshot = kwargs["conversation_snapshot"]
        self.selected = True
        return GovernedMemoryAssemblyV1(
            successor_memory_context_block=successor_block(
                snapshot.current_request_id,  # type: ignore[attr-defined]
                snapshot.messages[-1].content,  # type: ignore[attr-defined]
            )
        )

    async def persist_dispatched_answer_binding(self, **kwargs: object) -> None:
        if not self.selected:
            raise AssertionError("successor selection already terminal")
        self.persisted.append(dict(kwargs))
        self.selected = False

    def discard_selected_state(self) -> None:
        self.discards += 1
        self.selected = False


class SuccessorProviderPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_base_v1_payload_contains_exact_successor_memory_once(self) -> None:
        plan = await successor_base_plan("Which interface theme do I prefer?")
        request = OpenAIChatRequestV1.create(trusted_plan=plan)
        named = [
            item
            for item in request.messages
            if item.name == "governed_memory_successor_v1"
        ]
        self.assertEqual(len(named), 1)
        self.assertEqual(named[0].role, "user")
        self.assertEqual(named[0].content, SUCCESSOR_CONTENT)
        self.assertNotIn(SUCCESSOR_CONTENT, request.messages[0].content)
        self.assertEqual(
            request.provider_kwargs_json_bytes().count(
                canonical_json_bytes(SUCCESSOR_CONTENT)
            ),
            1,
        )

    async def test_lifeswitch_v4_payload_preserves_exact_successor_memory_once(
        self,
    ) -> None:
        query = "What were my macros Monday?"
        base = await successor_base_plan(query)
        plan = TrustedLifeSwitchResponsePlanV2.create(
            base_response_plan=base,
            lifeswitch_context=selected_context(base, query),
            prior_lifeswitch_context=off_prior(),
        )
        request = OpenAIChatRequestV4.create(source_plan=plan)
        named = [
            item
            for item in request.messages
            if item.name == "governed_memory_successor_v1"
        ]
        self.assertEqual(len(named), 1)
        self.assertEqual(named[0].role, "user")
        self.assertEqual(named[0].content, SUCCESSOR_CONTENT)
        self.assertEqual(
            request.provider_kwargs_json_bytes().count(
                canonical_json_bytes(SUCCESSOR_CONTENT)
            ),
            1,
        )

    async def test_base_root_dispatches_one_answer_and_exact_binding_payload(
        self,
    ) -> None:
        lifecycle = OneRequestSuccessorLifecycle()
        client = CombinedOpenAIClient()
        root = InactiveResponseCompositionRootV0_2(
            openai_client=client,
            classifier_model="gpt-5.1",
            memory_provider=lifecycle,
            successor_memory_lifecycle=lifecycle,
            answer_id_factory=lambda: ANSWER,
            correlation_id_factory=lambda: CORRELATION,
        )

        execution = await root.execute_detailed(
            SnapshotConn(),
            command("Which interface theme do I prefer?"),
        )

        self.assertEqual(execution.finalized.answer_id, ANSWER)
        self.assertEqual(
            [name for name, _kwargs in client.calls],
            ["classifier", "moderation", "chat"],
        )
        self.assertEqual(len(lifecycle.persisted), 1)
        binding = lifecycle.persisted[0]
        self.assertEqual(binding["answer_id"], ANSWER)
        outbound = binding["outbound_request_bytes"]
        self.assertIsInstance(outbound, bytes)
        self.assertEqual(
            outbound.count(canonical_json_bytes(SUCCESSOR_CONTENT)),  # type: ignore[union-attr]
            1,
        )
        self.assertFalse(lifecycle.has_selected_claims)

    async def test_lifeswitch_v4_dispatches_and_clears_successor_lifecycle(
        self,
    ) -> None:
        query = "What were my macros Monday?"
        context_base = await orchestrator(FixedSafetyProvider()).build_plan(
            trusted_request(
                authenticated_actor_user_id=ACTOR,
                request_id="composition-request",
                conversation=messages(query),
            )
        )
        lifecycle = OneRequestSuccessorLifecycle()
        client = CombinedOpenAIClient()
        base_root = InactiveResponseCompositionRootV0_2(
            openai_client=client,
            classifier_model="gpt-5.1",
            memory_provider=lifecycle,
            successor_memory_lifecycle=lifecycle,
            correlation_id_factory=lambda: CORRELATION,
        )
        root = IntegratedLifeSwitchResponseCompositionRootV0_4(
            base_root=base_root,
            openai_client=client,
            context_provider=CurrentProvider(selected_context(context_base, query)),
            prior_provenance_provider=PriorProvider(),
            answer_id_factory=lambda: ANSWER,
        )

        execution = await root.execute_detailed(SnapshotConn(), command(query))

        self.assertEqual(execution.finalized.answer_id, ANSWER)
        self.assertEqual(
            [name for name, _kwargs in client.calls],
            ["classifier", "moderation", "chat"],
        )
        self.assertEqual(len(lifecycle.persisted), 1)
        outbound = lifecycle.persisted[0]["outbound_request_bytes"]
        self.assertIsInstance(outbound, bytes)
        self.assertEqual(
            outbound.count(canonical_json_bytes(SUCCESSOR_CONTENT)),  # type: ignore[union-attr]
            1,
        )
        self.assertFalse(lifecycle.has_selected_claims)


if __name__ == "__main__":
    unittest.main()
