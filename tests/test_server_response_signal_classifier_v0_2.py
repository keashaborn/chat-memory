from __future__ import annotations

import unittest
from typing import Any

from rag_engine.response_policy_v0_2 import (
    ConversationRole,
    FMLevel,
    GateState,
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
        result = classifier(client).classify(request("This is ambiguous."))

        self.assertEqual(result.assessment.outcome, ClassificationOutcome.PROVIDER_UNCERTAIN)
        self.assertEqual(result.assessment.gate, GateState.UNCERTAIN)
        self.assertEqual(
            result.assessment.reason_codes,
            ("domain_classifier_provider_error",),
        )
        self.assertNotIn("credential", result.model_dump_json())

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


if __name__ == "__main__":
    unittest.main()
