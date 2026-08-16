from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from uuid import UUID

from pydantic import ValidationError

from rag_engine.prompt_assembler_v1 import ContextKind
from rag_engine.response_conversation_snapshot_v1 import (
    create_current_only_conversation_snapshot_v1,
)
from rag_engine.response_orchestration_v0_2 import (
    ResponseOrchestrationError,
    SanitizedResponseShadowTraceV0_2,
    TRUSTED_SAFETY_ASSESSOR_COMPONENTS_V0_2,
    TrustedPolicySignalsEnvelopeV0_2,
    TrustedResponseOrchestratorV0_2,
    TrustedResponsePlanV0_2,
    TrustedResponseRequestV0_2,
    _plan_sha256,
)
from rag_engine.response_policy_v0_2 import (
    ConversationRole,
    FMLevel,
    GateState,
    ResponseMode,
    ResponsePolicyConversationMessageV0_2,
    ResponsePolicyInputV0_2,
    ResponsePolicySignalsV0_2,
    SafetyAssessmentV0_2,
)
from rag_engine.response_source_awareness_v1 import MemorySourceStatusV1
from tests.test_prompt_assembler_v1 import successor_memory_block


ACTOR = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
OTHER_ACTOR = UUID("557ea042-cb82-48f8-9429-472e96c957ef")
CORRELATION = UUID("1f694b80-e215-4182-b631-2f4c89ff2229")
THREAD = UUID("d776c8ef-7f3d-45b2-8820-4be87b7ca19d")
NOW = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)


def messages(
    current: str,
    *prior: tuple[ConversationRole, str],
) -> tuple[ResponsePolicyConversationMessageV0_2, ...]:
    return tuple(
        ResponsePolicyConversationMessageV0_2(role=role, content=content)
        for role, content in (
            *prior,
            (ConversationRole.USER, current),
        )
    )


def trusted_request(**kwargs: object) -> TrustedResponseRequestV0_2:
    actor = kwargs.pop("authenticated_actor_user_id")
    request_id = kwargs.pop("request_id")
    conversation = kwargs.pop("conversation")
    signals = kwargs.pop("trusted_policy_signals", None)
    assert isinstance(actor, UUID)
    assert isinstance(request_id, str)
    assert isinstance(conversation, tuple) and len(conversation) == 1
    snapshot = create_current_only_conversation_snapshot_v1(
        authenticated_actor_user_id=actor,
        thread_id=THREAD,
        current_request_id=request_id,
        current_message=conversation[-1].content,
    )
    signal_envelope = TrustedPolicySignalsEnvelopeV0_2.create(
        conversation_snapshot=snapshot,
        signals=signals if isinstance(signals, ResponsePolicySignalsV0_2) else None,
    )
    if kwargs.get("successor_memory_context_block") is not None:
        kwargs.setdefault("memory_source_status", MemorySourceStatusV1.SELECTED)
    return TrustedResponseRequestV0_2.create_from_snapshot(
        authenticated_actor_user_id=actor,
        conversation_snapshot=snapshot,
        trusted_policy_signals_envelope=signal_envelope,
        **kwargs,
    )


class FixedSafetyProvider:
    def __init__(
        self,
        *,
        gate: GateState = GateState.PASS,
        action: bool = False,
        reasons: tuple[str, ...] = (),
        components: tuple[str, ...] = TRUSTED_SAFETY_ASSESSOR_COMPONENTS_V0_2,
    ) -> None:
        self.gate = gate
        self.action = action
        self.reasons = reasons
        self.components = components
        self.requests: list[ResponsePolicyInputV0_2] = []

    def assess(self, request: ResponsePolicyInputV0_2) -> SafetyAssessmentV0_2:
        self.requests.append(request)
        return SafetyAssessmentV0_2.create(
            request,
            high_stakes_gate=self.gate,
            safety_action_required=self.action,
            reason_codes=self.reasons,
            assessor_components=self.components,
        )


class AsyncSafetyProvider(FixedSafetyProvider):
    async def assess(
        self, request: ResponsePolicyInputV0_2
    ) -> SafetyAssessmentV0_2:
        return super().assess(request)


class WrongBindingSafetyProvider:
    def assess(self, request: ResponsePolicyInputV0_2) -> SafetyAssessmentV0_2:
        other = ResponsePolicyInputV0_2.create(
            request_id="other-request",
            conversation=request.conversation,
        )
        return SafetyAssessmentV0_2.create(
            other,
            assessor_components=TRUSTED_SAFETY_ASSESSOR_COMPONENTS_V0_2,
        )


class BrokenSafetyProvider:
    def assess(self, request: ResponsePolicyInputV0_2) -> SafetyAssessmentV0_2:
        del request
        raise TimeoutError("private provider failure details")


def orchestrator(provider: object) -> TrustedResponseOrchestratorV0_2:
    return TrustedResponseOrchestratorV0_2(
        provider,  # type: ignore[arg-type]
        clock=lambda: NOW,
        correlation_id_factory=lambda: CORRELATION,
    )


class TrustedResponseOrchestrationV0_2Tests(unittest.IsolatedAsyncioTestCase):
    async def test_ordinary_plan_is_content_free_in_shadow_trace(self) -> None:
        private_text = "PRIVATE-CONTENT-SENTINEL: what time is it?"
        provider = FixedSafetyProvider()
        request = trusted_request(
            authenticated_actor_user_id=ACTOR,
            request_id="ordinary-request",
            conversation=messages(private_text),
            request_field_names=("mix", "roleplay", "vantage_id"),
        )
        plan = await orchestrator(provider).build_plan(request)

        self.assertEqual(plan.policy_decision.response_mode, ResponseMode.ORDINARY)
        self.assertEqual(plan.policy_decision.fm_effective_level, FMLevel.OFF)
        self.assertEqual(plan.fm_selection.status, "OFF")
        self.assertEqual(plan.assembled_prompt.context_blocks, ())
        self.assertEqual(
            plan.policy_decision.ignored_legacy_request_fields,
            ("mix", "roleplay", "vantage_id"),
        )
        serialized = json.dumps(plan.shadow_trace.public_dict(), sort_keys=True)
        self.assertNotIn(private_text, serialized)
        self.assertNotIn(str(ACTOR), serialized)
        self.assertNotIn("actor_user_id", serialized)
        self.assertNotIn(private_text, repr(plan))
        self.assertEqual(len(provider.requests), 1)

    async def test_classifier_unavailable_trace_is_degraded_not_high_stakes(self) -> None:
        request = trusted_request(
            authenticated_actor_user_id=ACTOR,
            request_id="classifier-unavailable-request",
            conversation=messages("What is 2 + 2?"),
            trusted_policy_signals=ResponsePolicySignalsV0_2(
                domain_risk_gate=GateState.UNCERTAIN,
                domain_risk_reason_codes=("domain_classifier_unavailable",),
                fm_application_gate=GateState.UNCERTAIN,
            ),
        )

        plan = await orchestrator(FixedSafetyProvider()).build_plan(request)

        self.assertEqual(plan.policy_decision.response_mode, ResponseMode.ORDINARY)
        self.assertEqual(plan.policy_decision.high_stakes_gate, GateState.PASS)
        self.assertEqual(plan.policy_decision.interaction.value, "DIRECT")
        self.assertEqual(plan.policy_decision.fm_effective_level, FMLevel.OFF)
        self.assertEqual(plan.fm_selection.status, "OFF")
        self.assertEqual(plan.assembled_prompt.context_blocks, ())
        self.assertEqual(plan.shadow_trace.high_stakes_gate, GateState.PASS.value)
        self.assertFalse(plan.shadow_trace.safety_action_required)
        self.assertIn(
            "domain_risk:domain_classifier_unavailable",
            plan.shadow_trace.safety_reason_codes,
        )
        self.assertIn(
            "domain_classifier_unavailable",
            plan.shadow_trace.mode_reason_codes,
        )

    async def test_explicit_response_language_is_bound_into_typed_prompt(self) -> None:
        request = trusted_request(
            authenticated_actor_user_id=ACTOR,
            request_id="spanish-language-request",
            conversation=messages("¿Cómo estás?"),
            response_language="es",
        )

        plan = await orchestrator(FixedSafetyProvider()).build_plan(request)

        self.assertEqual(plan.policy_decision.response_mode, ResponseMode.ORDINARY)
        self.assertIn("Reply in Spanish", plan.assembled_prompt.system_prompt)
        self.assertIn(
            "Language choice never weakens safety",
            plan.assembled_prompt.system_prompt,
        )

    async def test_explicit_rm_is_a_separate_reference_data_block(self) -> None:
        request = trusted_request(
            authenticated_actor_user_id=ACTOR,
            request_id="rm-request",
            conversation=messages(
                "Explain Relational Monism and relational distinction."
            ),
        )
        plan = await orchestrator(FixedSafetyProvider()).build_plan(request)

        self.assertEqual(plan.policy_decision.response_mode, ResponseMode.FM_EXPLICIT)
        self.assertEqual(plan.policy_decision.fm_effective_level, FMLevel.EXPLICIT)
        self.assertEqual(plan.fm_selection.status, "SELECTED")
        self.assertGreater(len(plan.fm_selection.selected_record_ids), 0)
        self.assertEqual(len(plan.assembled_prompt.context_blocks), 1)
        block = plan.assembled_prompt.context_blocks[0]
        self.assertEqual(block.kind, ContextKind.RELATIONAL_MONISM)
        self.assertEqual(block.block_id, "relational_monism_v0_4")
        self.assertEqual(
            plan.fm_selection.active_philosophy_id,
            "relational_monism_v0_4",
        )
        self.assertEqual(block.authority, "reference_data")
        self.assertNotIn(block.content, plan.assembled_prompt.system_prompt)

    async def test_technical_mode_suppresses_fm_even_with_fm_terms(self) -> None:
        request = trusted_request(
            authenticated_actor_user_id=ACTOR,
            request_id="technical-request",
            conversation=messages(
                "Implement the Python API that stores Relational Monism records."
            ),
        )
        plan = await orchestrator(FixedSafetyProvider()).build_plan(request)
        self.assertEqual(plan.policy_decision.response_mode, ResponseMode.TECHNICAL)
        self.assertEqual(plan.policy_decision.fm_effective_level, FMLevel.OFF)
        self.assertEqual(plan.fm_selection.status, "OFF")
        self.assertEqual(plan.assembled_prompt.context_blocks, ())

    async def test_moderation_trigger_has_precedence_and_no_fm_context(self) -> None:
        request = trusted_request(
            authenticated_actor_user_id=ACTOR,
            request_id="safety-request",
            conversation=messages("Explain Relational Monism."),
            trusted_policy_signals=ResponsePolicySignalsV0_2(
                technical=True,
                fm_explicit=True,
                coaching=True,
            ),
        )
        plan = await orchestrator(
            FixedSafetyProvider(
                gate=GateState.TRIGGERED,
                action=True,
                reasons=("moderation:self_harm_intent",),
            )
        ).build_plan(request)

        self.assertEqual(plan.policy_decision.response_mode, ResponseMode.HIGH_STAKES)
        self.assertEqual(plan.policy_decision.fm_effective_level, FMLevel.OFF)
        self.assertEqual(plan.fm_selection.status, "OFF")
        self.assertEqual(plan.assembled_prompt.context_blocks, ())
        self.assertTrue(plan.shadow_trace.safety_action_required)

    async def test_uncertain_safety_fails_closed_to_high_stakes(self) -> None:
        request = trusted_request(
            authenticated_actor_user_id=ACTOR,
            request_id="uncertain-request",
            conversation=messages("Explain Relational Monism."),
        )
        plan = await orchestrator(
            AsyncSafetyProvider(
                gate=GateState.UNCERTAIN,
                reasons=("moderation_unavailable",),
            )
        ).build_plan(request)
        self.assertEqual(plan.policy_decision.response_mode, ResponseMode.HIGH_STAKES)
        self.assertEqual(plan.policy_decision.high_stakes_gate, GateState.UNCERTAIN)
        self.assertEqual(plan.policy_decision.fm_effective_level, FMLevel.OFF)

    async def test_coaching_uses_bounded_light_fm(self) -> None:
        request = trusted_request(
            authenticated_actor_user_id=ACTOR,
            request_id="coaching-request",
            conversation=messages("Help me stop missing workouts."),
        )
        plan = await orchestrator(FixedSafetyProvider()).build_plan(request)
        self.assertEqual(plan.policy_decision.response_mode, ResponseMode.COACHING)
        self.assertEqual(plan.policy_decision.fm_effective_level, FMLevel.LIGHT)
        self.assertLessEqual(plan.fm_selection.max_records, 3)
        self.assertLessEqual(plan.fm_selection.used_tokens, plan.fm_selection.token_budget)

    async def test_fm_opt_out_is_trusted_signal_not_browser_lens_value(self) -> None:
        request = trusted_request(
            authenticated_actor_user_id=ACTOR,
            request_id="opt-out-request",
            conversation=messages("Explain Relational Monism."),
            request_field_names=("fm_lens", "mix"),
            trusted_policy_signals=ResponsePolicySignalsV0_2(
                user_fm_opt_out=True
            ),
        )
        plan = await orchestrator(FixedSafetyProvider()).build_plan(request)
        self.assertTrue(plan.policy_decision.user_opt_out_applied)
        self.assertEqual(plan.policy_decision.fm_effective_level, FMLevel.OFF)
        self.assertEqual(plan.fm_selection.status, "OFF")

    async def test_wrong_safety_binding_fails_with_generic_error(self) -> None:
        request = trusted_request(
            authenticated_actor_user_id=ACTOR,
            request_id="binding-request",
            conversation=messages("Hello."),
        )
        with self.assertRaisesRegex(
            ResponseOrchestrationError,
            "trusted response orchestration failed",
        ):
            await orchestrator(WrongBindingSafetyProvider()).build_plan(request)

    async def test_provider_exception_details_are_not_exposed(self) -> None:
        request = trusted_request(
            authenticated_actor_user_id=ACTOR,
            request_id="broken-request",
            conversation=messages("Hello."),
        )
        with self.assertRaises(ResponseOrchestrationError) as raised:
            await orchestrator(BrokenSafetyProvider()).build_plan(request)
        self.assertNotIn("private provider failure details", str(raised.exception))

    async def test_untrusted_safety_assessor_bundle_is_rejected(self) -> None:
        request = trusted_request(
            authenticated_actor_user_id=ACTOR,
            request_id="untrusted-assessor-request",
            conversation=messages("Hello."),
        )
        with self.assertRaisesRegex(
            ResponseOrchestrationError,
            "untrusted assessor bundle",
        ):
            await orchestrator(
                FixedSafetyProvider(components=("untrusted_pass_provider",))
            ).build_plan(request)

    def test_request_rejects_unsorted_duplicate_field_names(self) -> None:
        snapshot = create_current_only_conversation_snapshot_v1(
            authenticated_actor_user_id=ACTOR,
            thread_id=THREAD,
            current_request_id="bad-fields-request",
            current_message="Hello.",
        )
        with self.assertRaises(ValidationError):
            TrustedResponseRequestV0_2(
                authenticated_actor_user_id=ACTOR,
                conversation_snapshot=snapshot,
                trusted_policy_signals_envelope=(
                    TrustedPolicySignalsEnvelopeV0_2.create(
                        conversation_snapshot=snapshot,
                    )
                ),
                legacy_request_field_names=("mix", "mix"),
            )

    def test_request_rejects_snapshot_from_another_actor(self) -> None:
        snapshot = create_current_only_conversation_snapshot_v1(
            authenticated_actor_user_id=OTHER_ACTOR,
            thread_id=THREAD,
            current_request_id="cross-owner-request",
            current_message="Hello.",
        )
        with self.assertRaises(ValidationError):
            TrustedResponseRequestV0_2.create_from_snapshot(
                authenticated_actor_user_id=ACTOR,
                conversation_snapshot=snapshot,
            )

    def test_request_rejects_policy_signals_from_another_snapshot(self) -> None:
        first_snapshot = create_current_only_conversation_snapshot_v1(
            authenticated_actor_user_id=ACTOR,
            thread_id=THREAD,
            current_request_id="first-signal-request",
            current_message="First message.",
        )
        stale_envelope = TrustedPolicySignalsEnvelopeV0_2.create(
            conversation_snapshot=first_snapshot,
            signals=ResponsePolicySignalsV0_2(coaching=True),
        )
        second_snapshot = create_current_only_conversation_snapshot_v1(
            authenticated_actor_user_id=ACTOR,
            thread_id=THREAD,
            current_request_id="second-signal-request",
            current_message="Second message.",
        )
        with self.assertRaises(ValidationError):
            TrustedResponseRequestV0_2.create_from_snapshot(
                authenticated_actor_user_id=ACTOR,
                conversation_snapshot=second_snapshot,
                trusted_policy_signals_envelope=stale_envelope,
            )

    def test_successor_memory_must_bind_request_and_current_query(self) -> None:
        current = "What is the relevant project constraint?"
        memory_block = successor_memory_block(current)
        valid = trusted_request(
            authenticated_actor_user_id=ACTOR,
            request_id="request-123",
            conversation=messages(current),
            successor_memory_context_block=memory_block,
        )
        self.assertEqual(valid.successor_memory_context_block, memory_block)

        for update in (
            {
                "thread_id": THREAD,
                "request_id": "another-request",
                "current_message": current,
            },
            {
                "thread_id": THREAD,
                "request_id": "request-123",
                "current_message": "A different current query.",
            },
        ):
            with self.subTest(update=update):
                snapshot = create_current_only_conversation_snapshot_v1(
                    authenticated_actor_user_id=ACTOR,
                    thread_id=update["thread_id"],
                    current_request_id=update["request_id"],
                    current_message=update["current_message"],
                )
                with self.assertRaises(ValidationError):
                    TrustedResponseRequestV0_2(
                        authenticated_actor_user_id=ACTOR,
                        conversation_snapshot=snapshot,
                        trusted_policy_signals_envelope=(
                            TrustedPolicySignalsEnvelopeV0_2.create(
                                conversation_snapshot=snapshot,
                            )
                        ),
                        successor_memory_context_block=memory_block,
                    )

    def test_zep_memory_is_accepted_when_bound_to_request_and_query(self) -> None:
        current = "What is my temporary recall phrase?"
        memory_block = successor_memory_block(
            current,
            block_id="zep_memory_v1",
            source_contract_version="zep-cloud-context-v1",
        )
        valid = trusted_request(
            authenticated_actor_user_id=ACTOR,
            request_id="request-123",
            conversation=messages(current),
            successor_memory_context_block=memory_block,
        )
        self.assertEqual(valid.successor_memory_context_block, memory_block)

    async def test_trace_manifest_rejects_semantic_tampering(self) -> None:
        request = trusted_request(
            authenticated_actor_user_id=ACTOR,
            request_id="trace-request",
            conversation=messages("Hello."),
        )
        plan = await orchestrator(FixedSafetyProvider()).build_plan(request)
        tampered = plan.shadow_trace.model_dump(mode="json")
        tampered["response_mode"] = ResponseMode.HIGH_STAKES.value
        with self.assertRaises(ValidationError):
            SanitizedResponseShadowTraceV0_2.model_validate(tampered)

    async def test_plan_rejects_rehashed_policy_signal_tampering(self) -> None:
        plan = await orchestrator(FixedSafetyProvider()).build_plan(
            trusted_request(
                authenticated_actor_user_id=ACTOR,
                request_id="signal-binding-request",
                conversation=messages("Hello."),
            )
        )
        forged_envelope = TrustedPolicySignalsEnvelopeV0_2.create(
            conversation_snapshot=plan.source_snapshot,
            signals=ResponsePolicySignalsV0_2(coaching=True),
        )
        forged = plan.model_copy(
            update={"policy_signals_envelope": forged_envelope}
        )
        forged = forged.model_copy(
            update={
                "plan_sha256": _plan_sha256(
                    actor=forged.authenticated_actor_user_id,
                    thread_id=forged.thread_id,
                    conversation_snapshot_sha256=(
                        forged.conversation_snapshot_sha256
                    ),
                    policy_input=forged.policy_input,
                    safety=forged.safety_assessment,
                    signals_envelope=forged.policy_signals_envelope,
                    decision=forged.policy_decision,
                    prompt=forged.policy_prompt,
                    fm=forged.fm_selection,
                    assembled=forged.assembled_prompt,
                    trace=forged.shadow_trace,
                )
            }
        )
        with self.assertRaises(ValidationError):
            TrustedResponsePlanV0_2.model_validate_json(
                forged.model_dump_json()
            )

    async def test_actor_identity_never_enters_public_shadow_trace(self) -> None:
        first = await orchestrator(FixedSafetyProvider()).build_plan(
            trusted_request(
                authenticated_actor_user_id=ACTOR,
                request_id="owner-one",
                conversation=messages("Hello."),
            )
        )
        second = await orchestrator(FixedSafetyProvider()).build_plan(
            trusted_request(
                authenticated_actor_user_id=OTHER_ACTOR,
                request_id="owner-two",
                conversation=messages("Hello."),
            )
        )
        first_trace = json.dumps(first.shadow_trace.public_dict(), sort_keys=True)
        second_trace = json.dumps(second.shadow_trace.public_dict(), sort_keys=True)
        self.assertNotIn(str(ACTOR), first_trace)
        self.assertNotIn(str(OTHER_ACTOR), second_trace)
        self.assertNotIn("actor_user_id", first_trace)
        self.assertNotIn("actor_user_id", second_trace)
        prohibited = {
            "request_id_sha256",
            "conversation_snapshot_sha256",
            "request_sha256",
            "current_message_sha256",
            "conversation_sha256",
            "safety_assessment_sha256",
            "policy_decision_sha256",
            "policy_prompt_sha256",
            "fm_selection_sha256",
            "memory_assembly_input_sha256",
            "memory_application_manifest_sha256",
            "assembly_sha256",
            "system_prompt_sha256",
        }
        self.assertTrue(prohibited.isdisjoint(first.shadow_trace.public_dict()))

    async def test_naive_shadow_clock_fails_closed(self) -> None:
        bad_clock = TrustedResponseOrchestratorV0_2(
            FixedSafetyProvider(),
            clock=lambda: datetime(2026, 7, 20, 15, 0),
            correlation_id_factory=lambda: CORRELATION,
        )
        with self.assertRaises(ResponseOrchestrationError):
            await bad_clock.build_plan(
                trusted_request(
                    authenticated_actor_user_id=ACTOR,
                    request_id="naive-clock",
                    conversation=messages("Hello."),
                )
            )


if __name__ == "__main__":
    unittest.main()
