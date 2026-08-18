from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from rag_engine.governed_memory_provider_v1 import (
    LiveGovernedMemoryAssemblyProviderV1,
    _memory_selection_budget_policy_v1,
)
from rag_engine.memory_v1_entity_scope_resolver_v2 import (
    EntityScopeResolutionError,
)
from seebx.capabilities.conversation.snapshot import (
    create_current_only_conversation_snapshot_v1,
)
from rag_engine.response_policy_v0_2 import ResponsePolicySignalsV0_2
from tests.test_memory_v1_selection_envelope_v1 import (
    FakeProvider,
    MemoryLane,
    lane_result,
)


ACTOR = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
OTHER_ACTOR = UUID("557ea042-cb82-48f8-9429-472e96c957ef")
THREAD = UUID("d776c8ef-7f3d-45b2-8820-4be87b7ca19d")


class FakeQdrant:
    def close(self) -> None:
        return None


class GovernedMemoryProviderV1Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.activation = patch.dict(
            os.environ,
            {
                "MEMORY_V1_GOVERNED_ACTIVE": "1",
                "MEMORY_V1_GOVERNED_ACTIVE_USER_IDS": str(ACTOR),
                "MEMORY_V1_SHADOW_ALL_AUTHENTICATED": "0",
                "MEMORY_V1_SHADOW_USER_IDS": str(ACTOR),
                "MEMORY_V1_SHADOW_MAX_SENSITIVITY": "medium",
                "MEMORY_V1_GOVERNED_EXPLICIT_HIGH_USER_IDS": str(ACTOR),
                "MEMORY_V1_GOVERNED_EXPLICIT_RECALL_MAX_SENSITIVITY": "high",
            },
            clear=False,
        )
        self.activation.start()

    def tearDown(self) -> None:
        self.activation.stop()

    def test_broad_profile_budget_uses_existing_global_ceiling(self) -> None:
        standard = _memory_selection_budget_policy_v1(
            broad_profile_recall=False,
        )
        broad = _memory_selection_budget_policy_v1(
            broad_profile_recall=True,
        )
        standard_claim = next(
            item for item in standard.lane_limits if item.lane is MemoryLane.CLAIM
        )
        broad_claim = next(
            item for item in broad.lane_limits if item.lane is MemoryLane.CLAIM
        )

        self.assertEqual(
            (standard_claim.max_records, standard_claim.max_tokens),
            (4, 500),
        )
        self.assertEqual(
            (broad_claim.max_records, broad_claim.max_tokens),
            (4, 360),
        )
        self.assertEqual(
            (broad.max_records, broad.max_tokens),
            (standard.max_records, standard.max_tokens),
        )

    async def test_non_activated_owner_returns_empty_before_classification(self) -> None:
        snapshot = create_current_only_conversation_snapshot_v1(
            authenticated_actor_user_id=OTHER_ACTOR,
            thread_id=THREAD,
            current_request_id="inactive-owner-request",
            current_message="Who is my spouse?",
        )
        provider = LiveGovernedMemoryAssemblyProviderV1(object())

        with patch(
            "rag_engine.governed_memory_provider_v1.classify_memory_intent",
            side_effect=AssertionError("inactive owner must not be classified"),
        ), patch(
            "rag_engine.governed_memory_provider_v1.embed_text",
            side_effect=AssertionError("inactive owner must not be embedded"),
        ), patch(
            "rag_engine.governed_memory_provider_v1.make_qdrant_client",
            side_effect=AssertionError("inactive owner must not reach Qdrant"),
        ):
            result = await provider.prepare(
                authenticated_actor_user_id=OTHER_ACTOR,
                conversation_snapshot=snapshot,
                trusted_policy_signals=ResponsePolicySignalsV0_2(),
            )

        self.assertIsNone(result.memory_input)
        self.assertIsNone(result.memory_application)

    async def test_governed_activation_does_not_depend_on_shadow_audit_allowlist(
        self,
    ) -> None:
        snapshot = create_current_only_conversation_snapshot_v1(
            authenticated_actor_user_id=ACTOR,
            thread_id=THREAD,
            current_request_id="governed-only-activation-request",
            current_message="Explain why the sky looks blue.",
        )
        provider = LiveGovernedMemoryAssemblyProviderV1(object())
        with patch.dict(
            os.environ,
            {
                "MEMORY_V1_SHADOW_ALL_AUTHENTICATED": "0",
                "MEMORY_V1_SHADOW_USER_IDS": "",
            },
            clear=False,
        ), patch(
            "rag_engine.governed_memory_provider_v1.classify_memory_intent",
            return_value={
                "routes": {"governed_claims": False},
                "memory_intent": "none",
                "claim_context": {},
            },
        ) as classify:
            result = await provider.prepare(
                authenticated_actor_user_id=ACTOR,
                conversation_snapshot=snapshot,
                trusted_policy_signals=ResponsePolicySignalsV0_2(),
            )

        classify.assert_called_once()
        self.assertIsNone(result.memory_input)
        self.assertIsNone(result.memory_application)

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

    async def test_owner_without_v5_entities_returns_empty_before_qdrant(
        self,
    ) -> None:
        snapshot = create_current_only_conversation_snapshot_v1(
            authenticated_actor_user_id=ACTOR,
            thread_id=THREAD,
            current_request_id="empty-owner-request",
            current_message="Do you know anything about my pets?",
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
            return_value={"snapshot": None},
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
            "rag_engine.governed_memory_provider_v1.governed_maximum_sensitivity",
            return_value="medium",
        ) as sensitivity_policy, patch(
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
        sensitivity_policy.assert_called_once()
        self.assertEqual(sensitivity_policy.call_args.args[0], ACTOR)

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

    async def test_fm_explicit_prior_stance_recall_applies_only_governed_stance(
        self,
    ) -> None:
        snapshot = create_current_only_conversation_snapshot_v1(
            authenticated_actor_user_id=ACTOR,
            thread_id=THREAD,
            current_request_id="fm-explicit-stance-request",
            current_message=(
                "What have I said about how Fractal Monism can help people?"
            ),
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
                allowed_predicates=("stance.reported",)
            ),
        ), patch(
            "rag_engine.governed_memory_provider_v1.V5ClaimLaneAdapterV2",
            return_value=FakeProvider(lane_result(MemoryLane.CLAIM)),
        ) as claim_adapter:
            result = await provider.prepare(
                authenticated_actor_user_id=ACTOR,
                conversation_snapshot=snapshot,
                trusted_policy_signals=ResponsePolicySignalsV0_2(
                    fm_explicit=True
                ),
            )

        self.assertIsNotNone(result.memory_input)
        self.assertIsNotNone(result.memory_application)
        assert result.memory_application is not None
        self.assertTrue(result.memory_application.memory_content_included)
        self.assertGreater(len(result.memory_application.injected_records), 0)
        self.assertGreater(result.memory_application.actual_prompt_tokens, 0)
        adapter_kwargs = claim_adapter.call_args.kwargs
        self.assertEqual(
            adapter_kwargs["predicate_prefix_resolver"](object()),
            ("stance.reported",),
        )

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
