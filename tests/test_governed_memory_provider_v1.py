from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from rag_engine.governed_memory_provider_v1 import (
    LiveGovernedMemoryAssemblyProviderV1,
)
from rag_engine.memory_v1_entity_scope_resolver_v2 import (
    EntityScopeResolutionError,
)
from rag_engine.response_conversation_snapshot_v1 import (
    create_current_only_conversation_snapshot_v1,
)
from rag_engine.response_policy_v0_2 import ResponsePolicySignalsV0_2
from tests.test_memory_v1_selection_envelope_v1 import (
    FakeProvider,
    MemoryLane,
    lane_result,
)


ACTOR = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
THREAD = UUID("d776c8ef-7f3d-45b2-8820-4be87b7ca19d")


class FakeQdrant:
    def close(self) -> None:
        return None


class GovernedMemoryProviderV1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_irrelevant_request_returns_empty_without_external_access(self) -> None:
        snapshot = create_current_only_conversation_snapshot_v1(
            authenticated_actor_user_id=ACTOR,
            thread_id=THREAD,
            current_request_id="ordinary-request",
            current_message="Explain why the sky looks blue.",
        )
        provider = LiveGovernedMemoryAssemblyProviderV1(object())

        with patch(
            "rag_engine.governed_memory_provider_v1.classify_memory_intent",
            return_value={
                "routes": {"governed_claims": False},
                "memory_intent": "none",
                "claim_context": {},
            },
        ), patch(
            "rag_engine.governed_memory_provider_v1.embed_text",
            side_effect=AssertionError("embedding must not run"),
        ), patch(
            "rag_engine.governed_memory_provider_v1.make_qdrant_client",
            side_effect=AssertionError("Qdrant must not be opened"),
        ):
            result = await provider.prepare(
                authenticated_actor_user_id=ACTOR,
                conversation_snapshot=snapshot,
                trusted_policy_signals=ResponsePolicySignalsV0_2(),
            )

        self.assertIsNone(result.memory_input)
        self.assertIsNone(result.memory_application)

    async def test_unresolved_entity_scope_returns_empty_before_qdrant(self) -> None:
        snapshot = create_current_only_conversation_snapshot_v1(
            authenticated_actor_user_id=ACTOR,
            thread_id=THREAD,
            current_request_id="unresolved-pet-request",
            current_message="Do you remember when I lost my pet?",
        )
        provider = LiveGovernedMemoryAssemblyProviderV1(object())

        with patch.dict(
            os.environ,
            {"QDRANT_URL": "http://qdrant.invalid"},
        ), patch(
            "rag_engine.governed_memory_provider_v1.embed_text",
            return_value=[0.125, -0.25, 0.5],
        ), patch(
            "rag_engine.governed_memory_provider_v1.load_governed_entity_scope_snapshot_v2",
            return_value={"snapshot": object()},
        ), patch(
            "rag_engine.governed_memory_provider_v1.resolve_memory_claim_selector_context_v2",
            side_effect=EntityScopeResolutionError("no governed pet"),
        ), patch(
            "rag_engine.governed_memory_provider_v1.make_qdrant_client",
            side_effect=AssertionError("Qdrant must not be opened"),
        ):
            result = await provider.prepare(
                authenticated_actor_user_id=ACTOR,
                conversation_snapshot=snapshot,
                trusted_policy_signals=ResponsePolicySignalsV0_2(),
            )

        self.assertIsNone(result.memory_input)
        self.assertIsNone(result.memory_application)

    async def test_family_profile_recall_selects_and_applies_governed_claims(self) -> None:
        snapshot = create_current_only_conversation_snapshot_v1(
            authenticated_actor_user_id=ACTOR,
            thread_id=THREAD,
            current_request_id="family-request",
            current_message="Can you tell me anything about my family members?",
        )
        provider = LiveGovernedMemoryAssemblyProviderV1(object())

        with patch.dict(
            os.environ,
            {"QDRANT_URL": "http://qdrant.invalid"},
        ), patch(
            "rag_engine.governed_memory_provider_v1.embed_text",
            return_value=[0.125, -0.25, 0.5],
        ), patch(
            "rag_engine.governed_memory_provider_v1.make_qdrant_client",
            return_value=FakeQdrant(),
        ), patch(
            "rag_engine.governed_memory_provider_v1.ClaimVectorIndex",
            return_value=object(),
        ), patch(
            "rag_engine.governed_memory_provider_v1.load_governed_entity_scope_snapshot_v2",
            return_value={"snapshot": object()},
        ), patch(
            "rag_engine.governed_memory_provider_v1.resolve_memory_claim_selector_context_v2",
            return_value=SimpleNamespace(
                allowed_predicates=("identity.name", "relationship.parent_of")
            ),
        ), patch(
            "rag_engine.governed_memory_provider_v1.V5ClaimLaneAdapterV2",
            return_value=FakeProvider(lane_result(MemoryLane.CLAIM)),
        ) as claim_adapter:
            result = await provider.prepare(
                authenticated_actor_user_id=ACTOR,
                conversation_snapshot=snapshot,
                trusted_policy_signals=ResponsePolicySignalsV0_2(),
            )

        self.assertIsNotNone(result.memory_input)
        self.assertIsNotNone(result.memory_application)
        assert result.memory_application is not None
        self.assertTrue(result.memory_application.memory_content_included)
        self.assertGreater(len(result.memory_application.injected_records), 0)
        self.assertGreater(result.memory_application.actual_prompt_tokens, 0)
        adapter_kwargs = claim_adapter.call_args.kwargs
        self.assertEqual(adapter_kwargs["candidate_limit"], 100)
        self.assertEqual(adapter_kwargs["minimum_semantic_score"], 0.0)
        self.assertEqual(adapter_kwargs["relative_semantic_ratio"], 0.0)

    async def test_specific_family_recall_retains_normal_semantic_budget(self) -> None:
        snapshot = create_current_only_conversation_snapshot_v1(
            authenticated_actor_user_id=ACTOR,
            thread_id=THREAD,
            current_request_id="specific-family-request",
            current_message="Who is my dad?",
        )
        provider = LiveGovernedMemoryAssemblyProviderV1(object())

        with patch.dict(
            os.environ,
            {"QDRANT_URL": "http://qdrant.invalid"},
        ), patch(
            "rag_engine.governed_memory_provider_v1.embed_text",
            return_value=[0.125, -0.25, 0.5],
        ), patch(
            "rag_engine.governed_memory_provider_v1.make_qdrant_client",
            return_value=FakeQdrant(),
        ), patch(
            "rag_engine.governed_memory_provider_v1.ClaimVectorIndex",
            return_value=object(),
        ), patch(
            "rag_engine.governed_memory_provider_v1.load_governed_entity_scope_snapshot_v2",
            return_value={"snapshot": object()},
        ), patch(
            "rag_engine.governed_memory_provider_v1.resolve_memory_claim_selector_context_v2",
            return_value=SimpleNamespace(
                allowed_predicates=("identity.name", "relationship.parent_of")
            ),
        ), patch(
            "rag_engine.governed_memory_provider_v1.V5ClaimLaneAdapterV2",
            return_value=FakeProvider(lane_result(MemoryLane.CLAIM)),
        ) as claim_adapter:
            result = await provider.prepare(
                authenticated_actor_user_id=ACTOR,
                conversation_snapshot=snapshot,
                trusted_policy_signals=ResponsePolicySignalsV0_2(),
            )

        self.assertIsNotNone(result.memory_input)
        adapter_kwargs = claim_adapter.call_args.kwargs
        self.assertEqual(adapter_kwargs["candidate_limit"], 24)
        self.assertEqual(adapter_kwargs["minimum_semantic_score"], 0.20)
        self.assertEqual(adapter_kwargs["relative_semantic_ratio"], 0.40)

    async def test_trusted_suppression_signals_block_family_memory_before_external_access(self) -> None:
        snapshot = create_current_only_conversation_snapshot_v1(
            authenticated_actor_user_id=ACTOR,
            thread_id=THREAD,
            current_request_id="technical-family-request",
            current_message="Can you tell me anything about my family members?",
        )
        for signals in (
            ResponsePolicySignalsV0_2(technical=True),
            ResponsePolicySignalsV0_2(fm_explicit=True),
        ):
            with self.subTest(signals=signals):
                provider = LiveGovernedMemoryAssemblyProviderV1(object())
                with patch(
                    "rag_engine.governed_memory_provider_v1.embed_text",
                    side_effect=AssertionError("embedding must not run"),
                ), patch(
                    "rag_engine.governed_memory_provider_v1.make_qdrant_client",
                    side_effect=AssertionError("Qdrant must not be opened"),
                ):
                    result = await provider.prepare(
                        authenticated_actor_user_id=ACTOR,
                        conversation_snapshot=snapshot,
                        trusted_policy_signals=signals,
                    )

                self.assertIsNone(result.memory_input)
                self.assertIsNone(result.memory_application)

    async def test_governed_claim_route_without_predicates_fails_closed(self) -> None:
        snapshot = create_current_only_conversation_snapshot_v1(
            authenticated_actor_user_id=ACTOR,
            thread_id=THREAD,
            current_request_id="invalid-policy-request",
            current_message="What do you remember about me?",
        )
        provider = LiveGovernedMemoryAssemblyProviderV1(object())

        with patch(
            "rag_engine.governed_memory_provider_v1.classify_memory_intent",
            return_value={
                "routes": {"governed_claims": True},
                "memory_intent": "personal_recall",
                "claim_context": {"allowed_predicates": []},
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "predicate allowlist"):
                await provider.prepare(
                    authenticated_actor_user_id=ACTOR,
                    conversation_snapshot=snapshot,
                    trusted_policy_signals=ResponsePolicySignalsV0_2(),
                )


if __name__ == "__main__":
    unittest.main()
