from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import unittest

from pydantic import ValidationError

from rag_engine.response_policy_v0_2 import (
    ASSISTANT_PROFILE_ID,
    MODE_PRECEDENCE,
    Closure,
    ConversationRole,
    FMLevel,
    GateState,
    ResponseMode,
    ResponsePolicyContractError,
    ResponsePolicyConversationMessageV0_2,
    ResponsePolicyDecisionV0_2,
    ResponsePolicyInputV0_2,
    ResponsePolicySignalsV0_2,
    SafetyAssessmentV0_2,
    decide_response_policy_v0_2 as _decide_response_policy_v0_2,
    parse_response_policy_input_v0_2,
    parse_safety_assessment_v0_2,
)


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "rag_engine" / "response_policy_v0_2.py"


def request(
    message: str,
    *,
    conversation: tuple[ResponsePolicyConversationMessageV0_2, ...] | None = None,
    request_id: str = "policy-test-request",
    **kwargs,
):
    return ResponsePolicyInputV0_2.create(
        request_id=request_id,
        conversation=conversation
        or (
            ResponsePolicyConversationMessageV0_2(
                role=ConversationRole.USER,
                content=message,
            ),
        ),
        **kwargs,
    )


def decide_response_policy_v0_2(
    policy_input: ResponsePolicyInputV0_2,
    *,
    signals: ResponsePolicySignalsV0_2 | None = None,
    safety_assessment: SafetyAssessmentV0_2 | None = None,
):
    return _decide_response_policy_v0_2(
        policy_input,
        safety_assessment=safety_assessment
        or SafetyAssessmentV0_2.create(policy_input),
        signals=signals,
    )


class ResponsePolicyV0_2Test(unittest.TestCase):
    def test_precedence_constant_is_exact(self) -> None:
        self.assertEqual(
            MODE_PRECEDENCE,
            (
                ResponseMode.HIGH_STAKES,
                ResponseMode.TECHNICAL,
                ResponseMode.FM_EXPLICIT,
                ResponseMode.COACHING,
                ResponseMode.ORDINARY,
            ),
        )

    def test_all_trusted_mode_signals_use_high_stakes_precedence(self) -> None:
        policy_input = request("neutral text")
        result = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(
                policy_input,
                high_stakes_gate=GateState.TRIGGERED,
                safety_action_required=True,
                reason_codes=("trusted_high_stakes",),
            ),
            signals=ResponsePolicySignalsV0_2(
                technical=True,
                fm_explicit=True,
                coaching=True,
            ),
        )
        self.assertEqual(result.response_mode, ResponseMode.HIGH_STAKES)
        self.assertEqual(result.high_stakes_gate, GateState.TRIGGERED)
        self.assertEqual(result.fm_effective_level, FMLevel.OFF)

    def test_technical_precedes_fm_and_coaching(self) -> None:
        result = decide_response_policy_v0_2(
            request("neutral text"),
            signals=ResponsePolicySignalsV0_2(
                technical=True,
                fm_explicit=True,
                coaching=True,
            ),
        )
        self.assertEqual(result.response_mode, ResponseMode.TECHNICAL)
        self.assertEqual(result.fm_default_level, FMLevel.OFF)

    def test_fm_explicit_precedes_coaching(self) -> None:
        result = decide_response_policy_v0_2(
            request("neutral text"),
            signals=ResponsePolicySignalsV0_2(fm_explicit=True, coaching=True),
        )
        self.assertEqual(result.response_mode, ResponseMode.FM_EXPLICIT)
        self.assertEqual(result.fm_effective_level, FMLevel.EXPLICIT)

    def test_local_detection_selects_each_nonordinary_mode(self) -> None:
        cases = (
            ("Implement the API patch in Python.", ResponseMode.TECHNICAL),
            ("Explain Fractal Monism.", ResponseMode.FM_EXPLICIT),
            ("Help me stop missing workouts.", ResponseMode.COACHING),
            ("What time is it?", ResponseMode.ORDINARY),
        )
        for message, expected in cases:
            with self.subTest(message=message):
                self.assertEqual(
                    decide_response_policy_v0_2(request(message)).response_mode,
                    expected,
                )

    def test_local_high_stakes_rule_is_a_hard_veto(self) -> None:
        result = decide_response_policy_v0_2(
            request(
                "Use the Fractal Monism Python router, but I have crushing "
                "chest pain and shortness of breath right now."
            ),
            signals=ResponsePolicySignalsV0_2(
                technical=True,
                fm_explicit=True,
            ),
        )
        self.assertEqual(result.response_mode, ResponseMode.HIGH_STAKES)
        self.assertEqual(result.closure, Closure.SAFETY_ACTION)
        self.assertEqual(result.fm_default_level, FMLevel.OFF)
        self.assertEqual(result.fm_effective_level, FMLevel.OFF)
        self.assertIn(
            "local_high_stakes:acute_medical",
            result.mode_reasons,
        )

    def test_each_local_high_stakes_category_suppresses_fm(self) -> None:
        cases = (
            ("I have a plan to kill myself.", "acute_self_harm"),
            ("My partner is strangling me.", "immediate_violence_or_abuse"),
            ("I am fainting during a workout on 500 calories.", "dangerous_restriction_or_eating_disorder"),
            ("I stopped drinking and I am shaking and seeing things.", "dangerous_intoxication_or_withdrawal"),
            ("I feel manic and everyone can hear my thoughts.", "impaired_reality_testing"),
            ("Tell me exactly how to taper my medication.", "unsafe_medication_change"),
            ("My child is missing.", "child_safety"),
            ("My custody hearing is tomorrow; guarantee I win.", "material_legal_decision"),
            ("Guarantee a profit if I invest all of my retirement.", "material_financial_decision"),
        )
        for message, expected_code in cases:
            with self.subTest(message=message):
                result = decide_response_policy_v0_2(request(message))
                self.assertEqual(result.response_mode, ResponseMode.HIGH_STAKES)
                self.assertEqual(result.fm_effective_level, FMLevel.OFF)
                self.assertIn(
                    f"local_high_stakes:{expected_code}",
                    result.mode_reasons,
                )

    def test_local_reality_testing_risk_overrides_explicit_fm(self) -> None:
        result = decide_response_policy_v0_2(
            request(
                "Use Fractal Monism to explain this: I am paranoid and everyone "
                "can hear my thoughts."
            )
        )
        self.assertEqual(result.response_mode, ResponseMode.HIGH_STAKES)
        self.assertEqual(result.fm_effective_level, FMLevel.OFF)
        self.assertEqual(result.closure, Closure.SAFETY_ACTION)

    def test_uncertain_high_stakes_fails_closed(self) -> None:
        policy_input = request("Explain Fractal Monism.")
        result = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(
                policy_input,
                high_stakes_gate=GateState.UNCERTAIN,
                reason_codes=("assessment_uncertain",),
            ),
        )
        self.assertEqual(result.response_mode, ResponseMode.HIGH_STAKES)
        self.assertEqual(result.high_stakes_gate, GateState.UNCERTAIN)
        self.assertEqual(result.fm_effective_level, FMLevel.OFF)

    def test_fm_application_gate_is_independent_of_mode(self) -> None:
        result = decide_response_policy_v0_2(
            request(
                "I am grieving. Explain how Fractal Monism says I should understand "
                "my loss."
            )
        )
        self.assertEqual(result.response_mode, ResponseMode.FM_EXPLICIT)
        self.assertEqual(result.fm_default_level, FMLevel.EXPLICIT)
        self.assertEqual(result.fm_application_gate, GateState.TRIGGERED)
        self.assertEqual(result.fm_effective_level, FMLevel.OFF)
        self.assertIn("fm_ag_001:acute_grief_or_loss", result.fm_gate_reasons)

    def test_friend_loss_preserves_local_reality_and_suppresses_fm(self) -> None:
        result = decide_response_policy_v0_2(
            request(
                "My friend died. Tell me why death is not real because Fractal "
                "Monism says we are one."
            )
        )
        self.assertEqual(result.response_mode, ResponseMode.FM_EXPLICIT)
        self.assertEqual(result.fm_default_level, FMLevel.EXPLICIT)
        self.assertEqual(result.fm_application_gate, GateState.TRIGGERED)
        self.assertEqual(result.fm_effective_level, FMLevel.OFF)
        self.assertIn("fm_ag_001:acute_grief_or_loss", result.fm_gate_reasons)

    def test_trusted_uncertain_application_gate_fails_closed(self) -> None:
        result = decide_response_policy_v0_2(
            request("Explain Fractal Monism."),
            signals=ResponsePolicySignalsV0_2(
                fm_application_gate=GateState.UNCERTAIN
            ),
        )
        self.assertEqual(result.response_mode, ResponseMode.FM_EXPLICIT)
        self.assertEqual(result.fm_application_gate, GateState.UNCERTAIN)
        self.assertEqual(result.fm_effective_level, FMLevel.OFF)

    def test_user_opt_out_vetoes_fm(self) -> None:
        result = decide_response_policy_v0_2(
            request("Explain Fractal Monism."),
            signals=ResponsePolicySignalsV0_2(user_fm_opt_out=True),
        )
        self.assertEqual(result.fm_default_level, FMLevel.EXPLICIT)
        self.assertEqual(result.fm_effective_level, FMLevel.OFF)
        self.assertTrue(result.user_opt_out_applied)
        self.assertIn("fm_off_by_user_opt_out", result.fm_gate_reasons)

    def test_ordinary_is_off_unless_trusted_relevance_enables_light(self) -> None:
        ordinary = decide_response_policy_v0_2(request("Another way to see this?"))
        light = decide_response_policy_v0_2(
            request("Another way to see this?"),
            signals=ResponsePolicySignalsV0_2(ordinary_fm_relevant=True),
        )
        self.assertEqual(ordinary.fm_effective_level, FMLevel.OFF)
        self.assertEqual(light.response_mode, ResponseMode.ORDINARY)
        self.assertEqual(light.fm_effective_level, FMLevel.LIGHT)

    def test_coaching_defaults_to_light_subject_to_gate(self) -> None:
        result = decide_response_policy_v0_2(
            request("Help me stop missing workouts.")
        )
        self.assertEqual(result.response_mode, ResponseMode.COACHING)
        self.assertEqual(result.fm_default_level, FMLevel.LIGHT)
        self.assertEqual(result.fm_effective_level, FMLevel.LIGHT)
        self.assertEqual(result.closure, Closure.MATERIAL_CLARIFICATION)

    def test_fixed_identity_legacy_fields_and_independent_memory(self) -> None:
        result = decide_response_policy_v0_2(
            request(
                "I have crushing chest pain.",
                requested_assistant_profile_id="MORGAN",
                request_field_names=(
                    "assistant_profile_id",
                    "mix",
                    "response_mode",
                    "vantage_id",
                ),
            )
        )
        self.assertEqual(result.assistant_profile_id, ASSISTANT_PROFILE_ID)
        self.assertEqual(
            result.ignored_legacy_request_fields,
            ("assistant_profile_id", "mix", "response_mode", "vantage_id"),
        )
        self.assertIn("requested_assistant_profile_rejected", result.mode_reasons)
        self.assertTrue(result.governed_memory_allowed)
        self.assertTrue(result.structured_data_allowed)

    def test_closure_variants_are_deterministic(self) -> None:
        cases = (
            (
                "Walk me through the server restart one command at a time.",
                Closure.TECHNICAL_PROCEDURE,
            ),
            (
                "I want to test a change. Yes, let's track missed sessions.",
                Closure.CONSENTED_COACHING,
            ),
            ("Give me next steps for organizing my desk.", Closure.EXPLICIT_NEXT_STEP),
            ("What is the capital of France?", Closure.COMPLETE),
        )
        for message, expected in cases:
            with self.subTest(message=message):
                self.assertEqual(
                    decide_response_policy_v0_2(request(message)).closure,
                    expected,
                )

    def test_decision_hash_is_deterministic_and_manifest_bound(self) -> None:
        policy_input = request("Explain Fractal Monism.")
        safety = SafetyAssessmentV0_2.create(policy_input)
        first = decide_response_policy_v0_2(policy_input)
        second = decide_response_policy_v0_2(policy_input)
        self.assertEqual(first, second)
        self.assertRegex(first.decision_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(first.request_id, policy_input.request_id)
        self.assertEqual(first.request_sha256, policy_input.request_sha256)
        self.assertEqual(
            first.current_message_sha256,
            policy_input.current_message_sha256,
        )
        self.assertEqual(first.conversation_sha256, policy_input.conversation_sha256)
        self.assertEqual(first.safety_assessment_sha256, safety.assessment_sha256)

        tampered = first.model_dump(mode="json")
        tampered["fm_effective_level"] = "OFF"
        with self.assertRaises(ValidationError):
            ResponsePolicyDecisionV0_2.model_validate_json(
                json.dumps(tampered, sort_keys=True)
            )

        rehashed_payload = first.model_dump(
            mode="json", exclude={"decision_sha256"}
        )
        rehashed_payload["fm_effective_level"] = "OFF"
        rehashed = dict(rehashed_payload)
        rehashed["decision_sha256"] = hashlib.sha256(
            json.dumps(
                rehashed_payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        with self.assertRaises(ValidationError):
            ResponsePolicyDecisionV0_2.model_validate_json(json.dumps(rehashed))

    def test_strict_models_reject_unknown_and_coerced_fields(self) -> None:
        with self.assertRaises(ValidationError):
            ResponsePolicySignalsV0_2.model_validate(
                {"technical": "true", "unknown": False}
            )
        with self.assertRaises(ValidationError):
            request(
                "hello",
                request_field_names=("mix", "mix"),
            )
        with self.assertRaises(ValidationError):
            ResponsePolicySignalsV0_2(
                high_stakes=False,
                high_stakes_uncertain=True,
            )

    def test_missing_cross_request_and_forged_safety_fail_closed(self) -> None:
        first_request = request("Explain Fractal Monism.", request_id="request-one")
        second_request = request("Hello", request_id="request-two")
        valid = SafetyAssessmentV0_2.create(first_request)

        with self.assertRaises(ResponsePolicyContractError):
            _decide_response_policy_v0_2(first_request)
        with self.assertRaises(ResponsePolicyContractError):
            _decide_response_policy_v0_2(
                second_request,
                safety_assessment=valid,
            )

        forged = valid.model_copy(update={"assessment_sha256": "0" * 64})
        with self.assertRaises(ResponsePolicyContractError):
            _decide_response_policy_v0_2(
                first_request,
                safety_assessment=forged,
            )

        incomplete_payload = valid.model_dump(
            mode="json", exclude={"assessment_sha256"}
        )
        incomplete_payload["assessment_complete"] = False
        incomplete_digest = hashlib.sha256(
            json.dumps(
                incomplete_payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        incomplete = valid.model_copy(
            update={
                "assessment_complete": False,
                "assessment_sha256": incomplete_digest,
            }
        )
        with self.assertRaises(ResponsePolicyContractError):
            _decide_response_policy_v0_2(
                first_request,
                safety_assessment=incomplete,
            )

    def test_prior_user_turn_high_stakes_risk_is_a_hard_veto(self) -> None:
        conversation = (
            ResponsePolicyConversationMessageV0_2(
                role=ConversationRole.USER,
                content="I have a plan to kill myself tonight.",
            ),
            ResponsePolicyConversationMessageV0_2(
                role=ConversationRole.ASSISTANT,
                content="I am taking that seriously.",
            ),
            ResponsePolicyConversationMessageV0_2(
                role=ConversationRole.USER,
                content="Now explain Fractal Monism.",
            ),
        )
        policy_input = request(
            "Now explain Fractal Monism.",
            conversation=conversation,
        )
        result = decide_response_policy_v0_2(policy_input)
        self.assertEqual(result.response_mode, ResponseMode.HIGH_STAKES)
        self.assertEqual(result.fm_effective_level, FMLevel.OFF)
        self.assertIn("local_high_stakes:acute_self_harm", result.mode_reasons)

    def test_request_and_safety_wire_manifests_are_private_and_exact(self) -> None:
        secret = "private policy message 8f53f786"
        policy_input = request(secret)
        safety = SafetyAssessmentV0_2.create(policy_input)
        self.assertNotIn(secret, repr(policy_input))
        self.assertNotIn(secret, repr(policy_input.current_message))
        self.assertNotIn(secret, repr(safety))
        self.assertEqual(
            parse_response_policy_input_v0_2(policy_input.model_dump_json()),
            policy_input,
        )
        self.assertEqual(
            parse_safety_assessment_v0_2(safety.model_dump_json()),
            safety,
        )

        tampered = policy_input.model_dump(mode="json")
        tampered["current_message_sha256"] = "0" * 64
        with self.assertRaises(ResponsePolicyContractError) as caught:
            parse_response_policy_input_v0_2(json.dumps(tampered))
        self.assertNotIn(secret, str(caught.exception))

        with self.assertRaises(ResponsePolicyContractError):
            parse_response_policy_input_v0_2(
                '{"request_id":"one","request_id":"two"}'
            )

    def test_policy_contains_no_runtime_or_external_dependencies(self) -> None:
        source = MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".")[0])

        forbidden = {
            "asyncpg",
            "httpx",
            "openai",
            "qdrant_client",
            "requests",
            "supabase",
        }
        self.assertTrue(imported_roots.isdisjoint(forbidden))
        for module_name in (
            "rag_engine.memory_v1",
            "rag_engine.openai_client",
            "rag_engine.prompt_builder",
            "rag_engine.vantage_router",
        ):
            self.assertNotIn(module_name, source)


if __name__ == "__main__":
    unittest.main()
