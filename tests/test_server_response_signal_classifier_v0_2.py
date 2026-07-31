from __future__ import annotations

import unittest
from typing import Any

import httpx
from openai import APITimeoutError

from rag_engine.response_policy_v0_2 import (
    Closure,
    ConversationRole,
    FMLevel,
    GateState,
    Interaction,
    QuestionPolicy,
    ResponseMode,
    ResponsePolicyConversationMessageV0_2,
    ResponsePolicyInputV0_2,
    SafetyAssessmentV0_2,
    decide_response_policy_v0_2,
)
from rag_engine.server_response_signal_classifier_v0_2 import (
    ClassificationOutcome,
    DomainRiskCategory,
    OpenAIServerResponseSignalClassifierV0_2,
    _CLASSIFIER_INSTRUCTIONS,
)


MODEL = "gpt-5.1"


def request(message: str) -> ResponsePolicyInputV0_2:
    return ResponsePolicyInputV0_2.create(
        request_id="signal-classifier-test",
        conversation=(
            ResponsePolicyConversationMessageV0_2(
                role=ConversationRole.USER,
                content=message,
            ),
        ),
    )


def output(**updates: Any) -> dict[str, Any]:
    value = {
        "domain_risk_gate": "pass",
        "categories": [],
        "safety_action_required": False,
        "fm_application_gate": "pass",
        "technical": False,
        "fm_explicit": False,
        "coaching": False,
        "ordinary_fm_relevant": False,
        "user_fm_opt_out": False,
        "technical_procedure_requested": False,
        "coaching_consent": False,
        "specific_experiment_consent": False,
        "experiment_reversible_and_proportionate": False,
        "experiment_measurement_defined": False,
        "experiment_adverse_indicators_defined": False,
        "experiment_stop_rule_defined": False,
        "direct_response_requested": False,
        "guided_reflection_requested": False,
        "behavioral_intervention_requested": False,
        "user_declines_questions": False,
        "material_clarification_required": False,
        "explicit_next_step_requested": False,
    }
    value.update(updates)
    return value


class FakeResponses:
    def __init__(self, parsed: Any = None, error: Exception | None = None) -> None:
        self.parsed = parsed
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {
            "status": "completed",
            "error": None,
            "incomplete_details": None,
            "id": "resp_classifier_001",
            "model": MODEL,
            "output_parsed": self.parsed,
        }


class FakeClient:
    def __init__(self, parsed: Any = None, error: Exception | None = None) -> None:
        self.responses = FakeResponses(parsed, error)
        self.option_calls: list[dict[str, Any]] = []

    def with_options(self, **kwargs: Any) -> "FakeClient":
        self.option_calls.append(kwargs)
        return self


def classifier(client: FakeClient) -> OpenAIServerResponseSignalClassifierV0_2:
    return OpenAIServerResponseSignalClassifierV0_2(
        client,
        model=MODEL,
        safety_identifier="vs1_" + "a" * 60,
    )


class ServerResponseSignalClassifierV0_2Tests(unittest.TestCase):
    def test_local_immediate_risk_bypasses_provider_and_suppresses_fm(self) -> None:
        client = FakeClient(output())
        policy_input = request(
            "Explain Fractal Monism, but I have crushing chest pain and cannot breathe."
        )

        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertEqual(result.assessment.outcome, ClassificationOutcome.LOCAL_TRIGGERED)
        self.assertEqual(result.assessment.gate, GateState.TRIGGERED)
        self.assertEqual(
            result.assessment.categories,
            (DomainRiskCategory.ACUTE_MEDICAL,),
        )
        self.assertTrue(result.assessment.safety_action_required)
        self.assertEqual(result.assessment.provider_call_count, 0)
        self.assertEqual(client.responses.calls, [])
        self.assertEqual(decision.response_mode, ResponseMode.HIGH_STAKES)
        self.assertEqual(decision.fm_effective_level, FMLevel.OFF)

    def test_local_input_limit_is_honest_zero_call_uncertain(self) -> None:
        client = FakeClient(output())
        oversized = ResponsePolicyInputV0_2.create(
            request_id="signal-classifier-large-test",
            conversation=tuple(
                ResponsePolicyConversationMessageV0_2(
                    role=ConversationRole.USER,
                    content=character * 27_000,
                )
                for character in ("a", "b", "c")
            ),
        )
        result = classifier(client).classify(oversized)

        self.assertEqual(result.assessment.outcome, ClassificationOutcome.LOCAL_UNCERTAIN)
        self.assertEqual(result.assessment.gate, GateState.UNCERTAIN)
        self.assertEqual(result.assessment.provider_call_count, 0)
        self.assertIsNone(result.assessment.provider_model)
        self.assertEqual(client.responses.calls, [])

    def test_provider_pass_can_select_technical_without_domain_risk(self) -> None:
        client = FakeClient(
            output(technical=True, technical_procedure_requested=True)
        )
        result = classifier(client).classify(request("Repair the websocket code."))

        self.assertEqual(result.assessment.outcome, ClassificationOutcome.PROVIDER_CLASSIFIED)
        self.assertEqual(result.assessment.gate, GateState.PASS)
        self.assertTrue(result.signals.technical)
        self.assertTrue(result.signals.technical_procedure_requested)
        self.assertEqual(result.assessment.provider_call_count, 1)
        call = client.responses.calls[0]
        self.assertFalse(call["store"])
        self.assertEqual(call["max_output_tokens"], 500)
        self.assertEqual(call["safety_identifier"], "vs1_" + "a" * 60)

    def test_provider_contract_requires_empty_categories_for_pass(self) -> None:
        self.assertIn(
            "When domain_risk_gate=pass, categories must be an empty list",
            _CLASSIFIER_INSTRUCTIONS,
        )
        self.assertIn(
            "A direct informational request about Fractal Monism is not domain risk",
            _CLASSIFIER_INSTRUCTIONS,
        )
        self.assertIn(
            'A conventional conversation closing or task-completion statement such as "I\'m done"',
            _CLASSIFIER_INSTRUCTIONS,
        )
        self.assertIn(
            "Apply the same risk thresholds in every language",
            _CLASSIFIER_INSTRUCTIONS,
        )

    def test_provider_contract_distinguishes_ordinary_fitness_nutrition(self) -> None:
        self.assertIn(
            "Ordinary fitness and nutrition language is not evidence",
            _CLASSIFIER_INSTRUCTIONS,
        )
        self.assertIn(
            "cancels independently supported medical",
            _CLASSIFIER_INSTRUCTIONS,
        )

    def test_provider_macro_false_positive_is_calibrated_to_reflection(self) -> None:
        client = FakeClient(
            output(
                domain_risk_gate="triggered",
                categories=["eating_disorder_or_dangerous_restriction"],
                fm_application_gate="triggered",
                coaching=True,
                guided_reflection_requested=False,
            )
        )
        policy_input = request(
            "Help me think through why missing my macro target bothers me. "
            "I want reflection, not a plan or experiment."
        )

        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertEqual(
            result.assessment.outcome,
            ClassificationOutcome.PROVIDER_CLASSIFIED,
        )
        self.assertEqual(result.assessment.gate, GateState.PASS)
        self.assertEqual(result.assessment.categories, ())
        self.assertEqual(result.assessment.provider_call_count, 1)
        self.assertEqual(decision.response_mode, ResponseMode.COACHING)
        self.assertEqual(decision.interaction, Interaction.GUIDED_REFLECTION)
        self.assertEqual(decision.closure, Closure.GUIDED_REFLECTION)

    def test_fitness_calibration_covers_product_vocabulary(self) -> None:
        messages = (
            "I keep missing my macros while bulking.",
            "Help me review my protein target for strength training.",
            "My calorie target is hard to hit during this cut.",
            "My nutrition adherence was inconsistent this week.",
            "Can we discuss my meal-plan targets for bodybuilding?",
            "My weightlifting macro targets changed this month.",
        )
        for message in messages:
            with self.subTest(message=message):
                client = FakeClient(
                    output(
                        domain_risk_gate="triggered",
                        categories=["eating_disorder_or_dangerous_restriction"],
                        fm_application_gate="triggered",
                    )
                )
                result = classifier(client).classify(request(message))
                self.assertEqual(result.assessment.gate, GateState.PASS)
                self.assertEqual(result.assessment.categories, ())
                self.assertEqual(result.assessment.provider_call_count, 1)

    def test_fitness_calibration_does_not_erase_other_risk_categories(self) -> None:
        client = FakeClient(
            output(
                domain_risk_gate="triggered",
                categories=[
                    "eating_disorder_or_dangerous_restriction",
                    "medical_or_health_decision",
                ],
                fm_application_gate="triggered",
            )
        )
        result = classifier(client).classify(
            request("My macro target changed after a new medical diagnosis.")
        )

        self.assertEqual(result.assessment.gate, GateState.TRIGGERED)
        self.assertEqual(
            result.assessment.categories,
            (
                DomainRiskCategory.EATING_DISORDER_OR_DANGEROUS_RESTRICTION,
                DomainRiskCategory.MEDICAL_OR_HEALTH_DECISION,
            ),
        )

    def test_fitness_calibration_does_not_erase_additional_risk_evidence(self) -> None:
        client = FakeClient(
            output(
                domain_risk_gate="triggered",
                categories=["eating_disorder_or_dangerous_restriction"],
                fm_application_gate="triggered",
            )
        )
        result = classifier(client).classify(
            request(
                "I am cutting, feel dizzy, and only eat 1,000 calories. "
                "Help me lower my macro target again."
            )
        )

        self.assertEqual(result.assessment.gate, GateState.TRIGGERED)
        self.assertEqual(
            result.assessment.categories,
            (DomainRiskCategory.EATING_DISORDER_OR_DANGEROUS_RESTRICTION,),
        )

    def test_fitness_calibration_preserves_broader_disorder_evidence(self) -> None:
        messages = (
            "I am obsessed with macros and terrified of gaining weight.",
            "I fast for three days when I miss my macro target.",
            "I hide food and avoid eating while cutting.",
            "I overexercise to compensate for missing my macro target.",
        )
        for message in messages:
            with self.subTest(message=message):
                client = FakeClient(
                    output(
                        domain_risk_gate="triggered",
                        categories=["eating_disorder_or_dangerous_restriction"],
                        fm_application_gate="triggered",
                    )
                )
                result = classifier(client).classify(request(message))
                self.assertEqual(result.assessment.gate, GateState.TRIGGERED)
                self.assertEqual(
                    result.assessment.categories,
                    (
                        DomainRiskCategory.EATING_DISORDER_OR_DANGEROUS_RESTRICTION,
                    ),
                )

    def test_fitness_calibration_does_not_override_uncertain_provider(self) -> None:
        client = FakeClient(
            output(
                domain_risk_gate="uncertain",
                categories=["eating_disorder_or_dangerous_restriction"],
                fm_application_gate="uncertain",
            )
        )
        result = classifier(client).classify(
            request("My macro target is difficult to hit while cutting.")
        )

        self.assertEqual(result.assessment.gate, GateState.UNCERTAIN)

    def test_fitness_calibration_does_not_override_safety_action(self) -> None:
        client = FakeClient(
            output(
                domain_risk_gate="triggered",
                categories=["eating_disorder_or_dangerous_restriction"],
                safety_action_required=True,
                fm_application_gate="triggered",
            )
        )
        result = classifier(client).classify(
            request("My macro target is difficult to hit while cutting.")
        )

        self.assertEqual(result.assessment.gate, GateState.TRIGGERED)
        self.assertTrue(result.assessment.safety_action_required)

    def test_local_dangerous_restriction_precedes_fitness_calibration(self) -> None:
        client = FakeClient(error=AssertionError("provider must not be called"))
        result = classifier(client).classify(
            request("I am weightlifting while starving myself to hit my macros.")
        )

        self.assertEqual(
            result.assessment.outcome,
            ClassificationOutcome.LOCAL_TRIGGERED,
        )
        self.assertEqual(result.assessment.gate, GateState.TRIGGERED)
        self.assertEqual(result.assessment.provider_call_count, 0)
        self.assertEqual(client.responses.calls, [])

    def test_non_english_risk_is_preserved_for_provider_classification(self) -> None:
        client = FakeClient(
            output(
                domain_risk_gate="triggered",
                categories=["mental_health_crisis"],
                safety_action_required=True,
                fm_application_gate="triggered",
            )
        )
        policy_input = request("No quiero vivir y estoy en peligro ahora.")

        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertEqual(result.assessment.gate, GateState.TRIGGERED)
        self.assertEqual(result.assessment.provider_call_count, 1)
        self.assertEqual(decision.response_mode, ResponseMode.HIGH_STAKES)
        self.assertEqual(decision.fm_effective_level, FMLevel.OFF)
        provider_payload = client.responses.calls[0]["input"][1]["content"]
        self.assertIn("No quiero vivir", provider_payload)

    def test_standalone_benign_closing_passes_locally_without_provider(self) -> None:
        client = FakeClient(error=AssertionError("provider must not be called"))
        policy_input = request("I'm done.")

        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertEqual(
            result.assessment.outcome,
            ClassificationOutcome.LOCAL_PASS,
        )
        self.assertEqual(result.assessment.gate, GateState.PASS)
        self.assertEqual(result.assessment.provider_call_count, 0)
        self.assertEqual(client.responses.calls, [])
        self.assertEqual(decision.response_mode, ResponseMode.ORDINARY)
        self.assertEqual(decision.high_stakes_gate, GateState.PASS)

    def test_benign_closing_rule_does_not_mask_explicit_self_harm(self) -> None:
        client = FakeClient(output())
        policy_input = request("I'm done. I want to kill myself.")

        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertEqual(
            result.assessment.outcome,
            ClassificationOutcome.PROVIDER_CLASSIFIED,
        )
        self.assertEqual(result.assessment.provider_call_count, 1)
        self.assertEqual(len(client.responses.calls), 1)
        self.assertEqual(decision.response_mode, ResponseMode.HIGH_STAKES)
        self.assertEqual(decision.high_stakes_gate, GateState.TRIGGERED)

    def test_benign_closing_rule_does_not_bypass_prior_risk_context(self) -> None:
        client = FakeClient(
            output(
                domain_risk_gate="uncertain",
                categories=["mental_health_crisis"],
                fm_application_gate="uncertain",
            )
        )
        policy_input = ResponsePolicyInputV0_2.create(
            request_id="signal-classifier-context-test",
            conversation=(
                ResponsePolicyConversationMessageV0_2(
                    role=ConversationRole.USER,
                    content="I don't want to live.",
                ),
                ResponsePolicyConversationMessageV0_2(
                    role=ConversationRole.ASSISTANT,
                    content="Are you in immediate danger?",
                ),
                ResponsePolicyConversationMessageV0_2(
                    role=ConversationRole.USER,
                    content="I'm done.",
                ),
            ),
        )

        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertEqual(
            result.assessment.outcome,
            ClassificationOutcome.PROVIDER_UNCERTAIN,
        )
        self.assertEqual(result.assessment.provider_call_count, 1)
        self.assertEqual(len(client.responses.calls), 1)
        self.assertEqual(decision.response_mode, ResponseMode.HIGH_STAKES)
        self.assertEqual(decision.high_stakes_gate, GateState.TRIGGERED)

    def test_inconsistent_pass_category_still_fails_closed(self) -> None:
        client = FakeClient(output(categories=["other_material_risk"]))
        result = classifier(client).classify(
            request("Could you tell me about Fractal Monism?")
        )

        self.assertEqual(result.assessment.gate, GateState.UNCERTAIN)
        self.assertEqual(result.signals.fm_application_gate, GateState.UNCERTAIN)
        self.assertEqual(
            result.assessment.reason_codes,
            ("domain_classifier_provider_error",),
        )

    def test_provider_domain_risk_routes_high_stakes_and_fm_off(self) -> None:
        client = FakeClient(
            output(
                domain_risk_gate="triggered",
                categories=["legal_decision"],
                fm_application_gate="triggered",
                fm_explicit=True,
            )
        )
        policy_input = request(
            "Under Fractal Monism, how should I answer this custody filing?"
        )
        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertEqual(result.assessment.gate, GateState.TRIGGERED)
        self.assertEqual(decision.response_mode, ResponseMode.HIGH_STAKES)
        self.assertEqual(decision.fm_effective_level, FMLevel.OFF)

    def test_provider_error_fails_closed_without_leaking_details(self) -> None:
        client = FakeClient(error=RuntimeError("private credential text"))
        policy_input = request("This is ambiguous.")
        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertEqual(result.assessment.outcome, ClassificationOutcome.PROVIDER_UNCERTAIN)
        self.assertEqual(result.assessment.gate, GateState.UNCERTAIN)
        self.assertEqual(
            result.assessment.reason_codes,
            ("domain_classifier_provider_error",),
        )
        self.assertEqual(decision.response_mode, ResponseMode.HIGH_STAKES)
        self.assertNotIn("credential", result.model_dump_json())

    def test_provider_timeout_uses_bounded_degraded_policy(self) -> None:
        timeout = APITimeoutError(
            request=httpx.Request("POST", "https://api.openai.com/v1/responses")
        )
        client = FakeClient(error=timeout)
        policy_input = request(
            "For this response-trace test only: what is 2 + 2? "
            "Answer with the number."
        )

        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertEqual(
            result.assessment.outcome,
            ClassificationOutcome.PROVIDER_UNCERTAIN,
        )
        self.assertEqual(result.assessment.gate, GateState.UNCERTAIN)
        self.assertEqual(
            result.assessment.reason_codes,
            ("domain_classifier_unavailable",),
        )
        self.assertEqual(decision.response_mode, ResponseMode.TECHNICAL)
        self.assertNotEqual(decision.response_mode, ResponseMode.HIGH_STAKES)
        self.assertEqual(decision.high_stakes_gate, GateState.PASS)
        self.assertEqual(decision.fm_application_gate, GateState.UNCERTAIN)
        self.assertEqual(decision.fm_effective_level, FMLevel.OFF)
        self.assertEqual(decision.interaction, Interaction.DIRECT)
        self.assertIn("domain_classifier_unavailable", decision.mode_reasons)
        self.assertEqual(result.assessment.provider_call_count, 1)
        self.assertEqual(len(client.responses.calls), 1)

    def test_provider_timeout_preserves_explicit_guided_reflection(self) -> None:
        timeout = APITimeoutError(
            request=httpx.Request("POST", "https://api.openai.com/v1/responses")
        )
        client = FakeClient(error=timeout)
        policy_input = request("Help me think through why Monday bothered me.")

        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertEqual(result.assessment.gate, GateState.UNCERTAIN)
        self.assertEqual(decision.response_mode, ResponseMode.ORDINARY)
        self.assertEqual(decision.interaction, Interaction.GUIDED_REFLECTION)
        self.assertEqual(decision.closure, Closure.GUIDED_REFLECTION)
        self.assertEqual(
            decision.question_policy,
            QuestionPolicy.OPTIONAL_ONE_NON_LEADING,
        )
        self.assertEqual(decision.fm_effective_level, FMLevel.OFF)
        self.assertFalse(decision.intervention_authorized)

    def test_educational_risk_terms_require_provider_context(self) -> None:
        cases = (
            (
                "What does the term self-harm mean in a clinical research paper?",
                "self_harm",
            ),
            (
                "I am writing an article about suicide prevention programs.",
                "mental_health_crisis",
            ),
            (
                "Summarize national overdose statistics for a public-health report.",
                "substance_intoxication_or_withdrawal",
            ),
            (
                "Explain how alcohol withdrawal works physiologically in a "
                "medical textbook.",
                "substance_intoxication_or_withdrawal",
            ),
            (
                "How is shortness of breath defined in medical writing?",
                "acute_medical",
            ),
            ("What is a custody hearing in general?", "legal_decision"),
            (
                "What is an eating disorder in general medical education?",
                "eating_disorder_or_dangerous_restriction",
            ),
            (
                "Define medication interaction for a pharmacology class.",
                "medication",
            ),
            (
                "Explain coercion as a concept in legal ethics.",
                "coercion_or_consent",
            ),
            (
                "What does tax advice mean as a regulated professional service?",
                "financial_decision",
            ),
            ("What is a bankruptcy advice clinic?", "financial_decision"),
            (
                "What does detox at home mean in a public-health article?",
                "substance_intoxication_or_withdrawal",
            ),
        )
        for message, category in cases:
            with self.subTest(message=message):
                client = FakeClient(
                    output(
                        domain_risk_gate="triggered",
                        categories=[category],
                        fm_application_gate="triggered",
                    )
                )
                policy_input = request(message)

                result = classifier(client).classify(policy_input)
                decision = decide_response_policy_v0_2(
                    policy_input,
                    safety_assessment=SafetyAssessmentV0_2.create(policy_input),
                    signals=result.signals,
                )

                self.assertEqual(
                    result.assessment.outcome,
                    ClassificationOutcome.PROVIDER_CLASSIFIED,
                )
                self.assertEqual(result.assessment.gate, GateState.PASS)
                self.assertEqual(result.assessment.provider_call_count, 1)
                self.assertEqual(len(client.responses.calls), 1)
                self.assertNotEqual(decision.response_mode, ResponseMode.HIGH_STAKES)

    def test_educational_context_never_masks_personal_risk(self) -> None:
        client = FakeClient(
            output(
                domain_risk_gate="triggered",
                categories=["self_harm"],
                safety_action_required=True,
                fm_application_gate="triggered",
            )
        )
        policy_input = request(
            "I am writing an article about suicide prevention, and I want to "
            "kill myself now."
        )

        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertEqual(result.assessment.gate, GateState.TRIGGERED)
        self.assertTrue(result.assessment.safety_action_required)
        self.assertEqual(decision.response_mode, ResponseMode.HIGH_STAKES)
        self.assertEqual(decision.closure, Closure.SAFETY_ACTION)

    def test_simple_arithmetic_clears_provider_technical_drift(self) -> None:
        client = FakeClient(
            output(
                technical=True,
                ordinary_fm_relevant=True,
                technical_procedure_requested=True,
                direct_response_requested=True,
            )
        )
        policy_input = request("What is 2 + 2?")

        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertIs(result.signals.technical, None)
        self.assertFalse(result.signals.ordinary_fm_relevant)
        self.assertIs(result.signals.technical_procedure_requested, None)
        self.assertEqual(decision.response_mode, ResponseMode.ORDINARY)
        self.assertEqual(decision.fm_effective_level, FMLevel.OFF)

    def test_simple_arithmetic_clears_provider_risk_uncertainty(self) -> None:
        client = FakeClient(
            output(
                domain_risk_gate="uncertain",
                categories=["other_material_risk"],
                fm_application_gate="uncertain",
                technical=True,
                ordinary_fm_relevant=True,
                direct_response_requested=True,
            )
        )
        policy_input = request("What is 2 + 2?")

        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertEqual(result.assessment.gate, GateState.PASS)
        self.assertEqual(result.assessment.categories, ())
        self.assertEqual(decision.response_mode, ResponseMode.ORDINARY)
        self.assertEqual(decision.fm_effective_level, FMLevel.OFF)

    def test_clear_non_fm_education_clears_provider_fm_drift(self) -> None:
        client = FakeClient(
            output(
                ordinary_fm_relevant=True,
                direct_response_requested=True,
            )
        )
        policy_input = request("Explain coercion as a concept in legal ethics.")

        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertFalse(result.signals.ordinary_fm_relevant)
        self.assertEqual(decision.response_mode, ResponseMode.ORDINARY)
        self.assertEqual(decision.fm_effective_level, FMLevel.OFF)

    def test_explicit_direct_request_clears_provider_clarification_drift(self) -> None:
        client = FakeClient(
            output(
                coaching=True,
                direct_response_requested=True,
                guided_reflection_requested=True,
                material_clarification_required=True,
            )
        )
        policy_input = request(
            "Give me your recommendation directly, then help me think through it."
        )

        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertIs(result.signals.direct_response_requested, True)
        self.assertIsNot(result.signals.material_clarification_required, True)
        self.assertEqual(decision.interaction, Interaction.DIRECT)
        self.assertEqual(decision.closure, Closure.COMPLETE)

    def test_technical_explanation_clears_provider_procedure_drift(self) -> None:
        client = FakeClient(
            output(
                technical=True,
                technical_procedure_requested=True,
                direct_response_requested=True,
            )
        )
        policy_input = request("Explain why this Python unit test is failing.")

        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertIs(result.signals.technical_procedure_requested, None)
        self.assertEqual(decision.response_mode, ResponseMode.TECHNICAL)
        self.assertEqual(decision.interaction, Interaction.DIRECT)
        self.assertEqual(decision.closure, Closure.COMPLETE)

    def test_specific_experiment_consent_negation_closes_every_gate(self) -> None:
        client = FakeClient(
            output(
                coaching=True,
                coaching_consent=True,
                specific_experiment_consent=True,
                experiment_reversible_and_proportionate=True,
                experiment_measurement_defined=True,
                experiment_adverse_indicators_defined=True,
                experiment_stop_rule_defined=True,
                direct_response_requested=True,
                behavioral_intervention_requested=True,
            )
        )
        policy_input = request(
            "Help me design a plan to hit my protein target, but I have not "
            "chosen or consented to a specific experiment."
        )

        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertIs(result.signals.specific_experiment_consent, False)
        self.assertIs(
            result.signals.experiment_reversible_and_proportionate,
            False,
        )
        self.assertIs(result.signals.experiment_measurement_defined, False)
        self.assertIs(
            result.signals.experiment_adverse_indicators_defined,
            False,
        )
        self.assertIs(result.signals.experiment_stop_rule_defined, False)
        self.assertEqual(
            decision.interaction,
            Interaction.BEHAVIORAL_INTERVENTION,
        )
        self.assertFalse(decision.intervention_authorized)
        self.assertFalse(decision.fm_ir_020_eligible)
        self.assertEqual(decision.closure, Closure.COMPLETE)

    def test_provider_cannot_trigger_domain_risk_while_leaving_fm_enabled(self) -> None:
        client = FakeClient(
            output(
                domain_risk_gate="triggered",
                categories=["medical_or_health_decision"],
                fm_application_gate="pass",
            )
        )
        result = classifier(client).classify(request("A subtle health decision."))

        self.assertEqual(result.assessment.gate, GateState.UNCERTAIN)
        self.assertEqual(result.signals.fm_application_gate, GateState.UNCERTAIN)
        self.assertEqual(
            result.assessment.reason_codes,
            ("domain_classifier_provider_error",),
        )

    def test_model_false_does_not_disable_deterministic_mode_detection(self) -> None:
        client = FakeClient(output())
        policy_input = request("Explain Fractal Monism.")
        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertIsNone(result.signals.fm_explicit)
        self.assertEqual(decision.response_mode, ResponseMode.FM_EXPLICIT)

    def test_provider_false_interaction_signals_remain_authoritative(self) -> None:
        client = FakeClient(
            output(
                direct_response_requested=False,
                guided_reflection_requested=True,
                behavioral_intervention_requested=False,
                user_declines_questions=False,
                coaching_consent=False,
            )
        )
        policy_input = request(
            "Help me think through why I miss my target. Do not help me design "
            "a plan or track whether it helps."
        )

        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertIs(result.signals.direct_response_requested, False)
        self.assertIs(result.signals.guided_reflection_requested, True)
        self.assertIs(result.signals.behavioral_intervention_requested, False)
        self.assertIs(result.signals.user_declines_questions, False)
        self.assertIs(result.signals.coaching_consent, False)
        self.assertIs(result.signals.specific_experiment_consent, False)
        self.assertEqual(decision.interaction, Interaction.GUIDED_REFLECTION)
        self.assertEqual(decision.closure, Closure.GUIDED_REFLECTION)
        self.assertFalse(decision.fm_ir_020_eligible)

    def test_frozen_reflection_phrase_overrides_provider_misclassification(self) -> None:
        client = FakeClient(
            output(
                direct_response_requested=True,
                guided_reflection_requested=False,
                behavioral_intervention_requested=True,
            )
        )
        policy_input = request(
            "Help me get unstuck creatively. I want to explore abstract work, "
            "try a new medium, and blend visual art with storytelling."
        )

        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertIs(result.signals.direct_response_requested, False)
        self.assertIs(result.signals.guided_reflection_requested, True)
        self.assertIs(result.signals.behavioral_intervention_requested, False)
        self.assertEqual(decision.interaction, Interaction.GUIDED_REFLECTION)
        self.assertFalse(decision.intervention_authorized)

    def test_frozen_technical_reflection_phrase_overrides_provider_false(self) -> None:
        client = FakeClient(
            output(
                technical=True,
                direct_response_requested=False,
                guided_reflection_requested=False,
                behavioral_intervention_requested=False,
            )
        )
        policy_input = request(
            "Ask me one useful question about making this bridge design last. "
            "I am considering durable low-maintenance materials."
        )

        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertIs(result.signals.guided_reflection_requested, True)
        self.assertEqual(decision.response_mode, ResponseMode.TECHNICAL)
        self.assertEqual(decision.interaction, Interaction.GUIDED_REFLECTION)

    def test_ambiguous_provider_false_remains_authoritative(self) -> None:
        client = FakeClient(output(guided_reflection_requested=False))
        policy_input = request("I am considering what this tradeoff means.")

        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertIs(result.signals.guided_reflection_requested, False)
        self.assertEqual(decision.interaction, Interaction.CONVERSATIONAL)
        self.assertEqual(
            decision.interaction_reasons,
            ("conversational_update_default",),
        )

    def test_direct_request_precedes_frozen_reflection_phrase(self) -> None:
        client = FakeClient(
            output(
                direct_response_requested=True,
                guided_reflection_requested=False,
            )
        )
        policy_input = request(
            "Help me think through the tradeoff, but give me your recommendation."
        )

        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertIs(result.signals.direct_response_requested, True)
        self.assertIs(result.signals.guided_reflection_requested, False)
        self.assertEqual(decision.interaction, Interaction.DIRECT)

    def test_negated_intervention_does_not_block_frozen_reflection(self) -> None:
        client = FakeClient(
            output(
                direct_response_requested=True,
                guided_reflection_requested=False,
                behavioral_intervention_requested=True,
            )
        )
        policy_input = request(
            "Help me think through why I miss my target. Do not help me design "
            "a plan or track whether it helps."
        )

        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertIs(result.signals.guided_reflection_requested, True)
        self.assertIs(result.signals.behavioral_intervention_requested, False)
        self.assertEqual(decision.interaction, Interaction.GUIDED_REFLECTION)

    def test_complete_specific_activation_derives_intervention_intent(self) -> None:
        client = FakeClient(
            output(
                coaching=False,
                coaching_consent=False,
                specific_experiment_consent=True,
                behavioral_intervention_requested=False,
                experiment_reversible_and_proportionate=True,
                experiment_measurement_defined=True,
                experiment_adverse_indicators_defined=True,
                experiment_stop_rule_defined=True,
            )
        )
        policy_input = request(
            "I accept the proposed one-week reversible behavior-change "
            "experiment, its measure, adverse indicators, and stop rule."
        )

        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertIs(result.signals.coaching, True)
        self.assertIs(result.signals.coaching_consent, True)
        self.assertIs(result.signals.behavioral_intervention_requested, True)
        self.assertEqual(decision.response_mode, ResponseMode.COACHING)
        self.assertEqual(
            decision.interaction,
            Interaction.BEHAVIORAL_INTERVENTION,
        )
        self.assertTrue(decision.intervention_authorized)
        self.assertTrue(decision.fm_ir_020_eligible)

    def test_frozen_activation_derives_explicit_prerequisites(self) -> None:
        client = FakeClient(output())
        policy_input = request(
            "I accept the proposed one-week reversible experiment for starting "
            "one 25-minute writing block at 9 AM. Record whether I start it. "
            "If it interferes with a required meeting, stop the experiment."
        )

        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertIs(result.signals.specific_experiment_consent, True)
        self.assertIs(
            result.signals.experiment_reversible_and_proportionate,
            True,
        )
        self.assertIs(result.signals.experiment_measurement_defined, True)
        self.assertIs(
            result.signals.experiment_adverse_indicators_defined,
            True,
        )
        self.assertIs(result.signals.experiment_stop_rule_defined, True)
        self.assertEqual(
            decision.interaction,
            Interaction.BEHAVIORAL_INTERVENTION,
        )
        self.assertTrue(decision.intervention_authorized)

    def test_frozen_activation_requires_every_explicit_element(self) -> None:
        client = FakeClient(output())
        policy_input = request(
            "I accept the proposed one-week reversible experiment. "
            "Record whether I start it."
        )

        result = classifier(client).classify(policy_input)

        self.assertIs(result.signals.specific_experiment_consent, False)
        self.assertIs(result.signals.behavioral_intervention_requested, False)

    def test_negated_frozen_activation_does_not_authorize(self) -> None:
        client = FakeClient(output())
        policy_input = request(
            "Do not use the agreed reversible experiment, measure, adverse "
            "indicators, or stop rule."
        )

        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertIs(result.signals.behavioral_intervention_requested, False)
        self.assertFalse(decision.intervention_authorized)

    def test_incomplete_specific_activation_does_not_derive_intervention(self) -> None:
        client = FakeClient(
            output(
                specific_experiment_consent=True,
                behavioral_intervention_requested=False,
                experiment_reversible_and_proportionate=True,
                experiment_measurement_defined=True,
                experiment_adverse_indicators_defined=True,
                experiment_stop_rule_defined=False,
            )
        )
        policy_input = request("I accept the proposed experiment.")

        result = classifier(client).classify(policy_input)

        self.assertIs(result.signals.behavioral_intervention_requested, False)

    def test_classifier_requires_every_fm_ir_020_prerequisite(self) -> None:
        client = FakeClient(
            output(
                coaching=True,
                coaching_consent=True,
                specific_experiment_consent=True,
                behavioral_intervention_requested=True,
                experiment_reversible_and_proportionate=True,
                experiment_measurement_defined=True,
                experiment_adverse_indicators_defined=True,
                experiment_stop_rule_defined=True,
            )
        )
        policy_input = request(
            "I accept the proposed one-week reversible experiment, its measure, "
            "adverse indicators, and stop rule."
        )

        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertEqual(
            decision.interaction,
            Interaction.BEHAVIORAL_INTERVENTION,
        )
        self.assertTrue(decision.fm_ir_020_eligible)
        self.assertEqual(decision.closure, Closure.CONSENTED_COACHING)

    def test_classifier_path_supports_accepted_technical_reflection(self) -> None:
        client = FakeClient(
            output(
                technical=True,
                guided_reflection_requested=True,
                behavioral_intervention_requested=False,
                coaching_consent=False,
            )
        )
        policy_input = request(
            "Ask me one useful question about making this bridge design last. "
            "I am considering durable low-maintenance materials, safety "
            "redundancy, and future loads."
        )

        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertEqual(decision.response_mode, ResponseMode.TECHNICAL)
        self.assertEqual(decision.interaction, Interaction.GUIDED_REFLECTION)
        self.assertEqual(
            decision.question_policy,
            QuestionPolicy.OPTIONAL_ONE_NON_LEADING,
        )
        self.assertFalse(decision.fm_ir_020_eligible)

    def test_classifier_path_supports_multi_turn_reflection_continuation(self) -> None:
        client = FakeClient(
            output(
                guided_reflection_requested=True,
                behavioral_intervention_requested=False,
                coaching_consent=False,
            )
        )
        policy_input = ResponsePolicyInputV0_2.create(
            request_id="signal-classifier-reflection-continuation",
            conversation=(
                ResponsePolicyConversationMessageV0_2(
                    role=ConversationRole.USER,
                    content="Help me think through whether this goal is still mine.",
                ),
                ResponsePolicyConversationMessageV0_2(
                    role=ConversationRole.ASSISTANT,
                    content="You are separating the goal from recognition.",
                ),
                ResponsePolicyConversationMessageV0_2(
                    role=ConversationRole.USER,
                    content=(
                        "What matters is whether I would choose it if nobody knew."
                    ),
                ),
            ),
        )

        result = classifier(client).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=result.signals,
        )

        self.assertEqual(decision.interaction, Interaction.GUIDED_REFLECTION)
        self.assertEqual(decision.closure, Closure.GUIDED_REFLECTION)
        self.assertFalse(decision.fm_ir_020_eligible)


if __name__ == "__main__":
    unittest.main()
