from __future__ import annotations

from pathlib import Path
import unittest

from pydantic import ValidationError

from rag_engine.response_policy_prompt_v0_2 import (
    ResponsePolicyPromptError,
    ResponsePolicyPromptV0_2,
    render_response_policy_prompt_v0_2,
)
from rag_engine.response_policy_v0_2 import (
    Closure,
    ConversationRole,
    FMLevel,
    ResponseMode,
    ResponsePolicyConversationMessageV0_2,
    ResponsePolicyInputV0_2,
    ResponsePolicySignalsV0_2,
    SafetyAssessmentV0_2,
    decide_response_policy_v0_2,
)


ROOT = Path(__file__).resolve().parents[1]
PROMPT_MODULE = ROOT / "rag_engine" / "response_policy_prompt_v0_2.py"


def decision(message: str, **signals):
    policy_input = ResponsePolicyInputV0_2.create(
        request_id="policy-prompt-test",
        conversation=(
            ResponsePolicyConversationMessageV0_2(
                role=ConversationRole.USER,
                content=message,
            ),
        ),
    )
    return decide_response_policy_v0_2(
        policy_input,
        safety_assessment=SafetyAssessmentV0_2.create(policy_input),
        signals=ResponsePolicySignalsV0_2(**signals),
    )


class ResponsePolicyPromptV0_2Test(unittest.TestCase):
    def test_ordinary_complete_ends_without_unsolicited_leading(self) -> None:
        rendered = render_response_policy_prompt_v0_2(decision("Hello"))
        self.assertEqual(rendered.response_mode, ResponseMode.ORDINARY)
        self.assertEqual(rendered.closure, Closure.COMPLETE)
        self.assertEqual(rendered.fm_effective_level, FMLevel.OFF)
        self.assertIn("When the request is answered, stop", rendered.content)
        self.assertIn("unsolicited task menu", rendered.content)
        self.assertIn("Effective Fractal Monism level: OFF", rendered.content)

    def test_ordinary_standalone_closing_forbids_crisis_reinterpretation(self) -> None:
        rendered = render_response_policy_prompt_v0_2(decision("I'm done."))

        self.assertEqual(rendered.response_mode, ResponseMode.ORDINARY)
        self.assertEqual(rendered.closure, Closure.COMPLETE)
        self.assertIn(
            "Do not reinterpret an ordinary closing as a safety disclosure",
            rendered.content,
        )
        self.assertIn(
            "reply with only a brief acknowledgment and stop",
            rendered.content,
        )
        self.assertIn(
            "Do not invent a control word, command, or user-interface behavior",
            rendered.content,
        )

    def test_high_stakes_instruction_forbids_fm_bypass(self) -> None:
        rendered = render_response_policy_prompt_v0_2(
            decision("I want to kill myself. Explain this with Fractal Monism.")
        )
        self.assertEqual(rendered.response_mode, ResponseMode.HIGH_STAKES)
        self.assertEqual(rendered.closure, Closure.SAFETY_ACTION)
        self.assertIn("Do not use Fractal Monism", rendered.content)
        self.assertIn("concrete safety action", rendered.content)
        self.assertNotIn(
            "reply with only a brief acknowledgment and stop",
            rendered.content,
        )

    def test_technical_procedure_is_one_action_then_verify(self) -> None:
        rendered = render_response_policy_prompt_v0_2(
            decision("Implement this exact Python patch.")
        )
        self.assertEqual(rendered.response_mode, ResponseMode.TECHNICAL)
        self.assertEqual(rendered.closure, Closure.TECHNICAL_PROCEDURE)
        self.assertIn("one small safe action", rendered.content)
        self.assertIn("expected output", rendered.content)

    def test_explicit_fm_preserves_epistemic_boundaries(self) -> None:
        rendered = render_response_policy_prompt_v0_2(
            decision("Explain Fractal Monism and the one perceiver thesis.")
        )
        self.assertEqual(rendered.response_mode, ResponseMode.FM_EXPLICIT)
        self.assertEqual(rendered.fm_effective_level, FMLevel.EXPLICIT)
        self.assertIn("epistemic distinctions", rendered.content)
        self.assertIn("external evidence", rendered.content)
        self.assertIn("Do not expose internal stable IDs", rendered.content)
        self.assertIn("instead of enumerating every record", rendered.content)
        self.assertIn("250 to 350 words", rendered.content)

    def test_coaching_requires_consent_before_intervention(self) -> None:
        rendered = render_response_policy_prompt_v0_2(
            decision("Help me stop missing workouts.")
        )
        self.assertEqual(rendered.response_mode, ResponseMode.COACHING)
        self.assertIn("Obtain consent", rendered.content)
        self.assertIn("Never run covert experiments", rendered.content)

    def test_prompt_is_deterministic_and_carries_request_bindings(self) -> None:
        source = decision("Hello")
        first = render_response_policy_prompt_v0_2(source)
        second = render_response_policy_prompt_v0_2(source)
        self.assertEqual(first, second)
        self.assertEqual(first.request_id, source.request_id)
        self.assertEqual(first.request_sha256, source.request_sha256)
        self.assertEqual(
            first.current_message_sha256,
            source.current_message_sha256,
        )
        self.assertEqual(first.conversation_sha256, source.conversation_sha256)
        self.assertEqual(
            first.safety_assessment_sha256,
            source.safety_assessment_sha256,
        )
        self.assertNotIn(
            "prompt_contribution",
            PROMPT_MODULE.read_text(encoding="utf-8"),
        )

    def test_forged_decision_and_prompt_manifests_fail_closed(self) -> None:
        source = decision("Hello")
        forged = source.model_copy(update={"decision_sha256": "0" * 64})
        with self.assertRaises(ResponsePolicyPromptError):
            render_response_policy_prompt_v0_2(forged)

        rendered = render_response_policy_prompt_v0_2(source)
        payload = rendered.model_dump(mode="python")
        payload["content"] = "forged"
        with self.assertRaises(ValidationError):
            ResponsePolicyPromptV0_2.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
