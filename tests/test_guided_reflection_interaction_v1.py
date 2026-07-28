from __future__ import annotations

import unittest

from rag_engine.response_policy_prompt_v0_2 import (
    render_response_policy_prompt_v0_2,
)
from rag_engine.response_policy_v0_2 import (
    Closure,
    ConversationRole,
    Interaction,
    QuestionPolicy,
    ResponseMode,
    ResponsePolicyConversationMessageV0_2,
    ResponsePolicyInputV0_2,
    ResponsePolicySignalsV0_2,
    SafetyAssessmentV0_2,
    decide_response_policy_v0_2,
)


def request(
    *turns: tuple[ConversationRole, str],
    request_id: str = "guided-reflection-test",
) -> ResponsePolicyInputV0_2:
    return ResponsePolicyInputV0_2.create(
        request_id=request_id,
        conversation=tuple(
            ResponsePolicyConversationMessageV0_2(role=role, content=content)
            for role, content in turns
        ),
    )


def decide(
    policy_input: ResponsePolicyInputV0_2,
    **signals: bool,
):
    return decide_response_policy_v0_2(
        policy_input,
        safety_assessment=SafetyAssessmentV0_2.create(policy_input),
        signals=ResponsePolicySignalsV0_2(**signals),
    )


class GuidedReflectionInteractionV1Tests(unittest.TestCase):
    def test_reflection_does_not_trigger_intervention(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "I want to think through why missing my macro target bothers "
                    "me. I am not asking for a plan.",
                )
            )
        )
        prompt = render_response_policy_prompt_v0_2(result)

        self.assertEqual(result.interaction, Interaction.GUIDED_REFLECTION)
        self.assertFalse(result.fm_ir_020_eligible)
        self.assertNotIn("observation window", prompt.content)
        self.assertNotIn("review criterion", prompt.content)
        self.assertIn("not an intervention plan", prompt.content)

    def test_synthesis_can_end_without_a_question(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "Help me think this through, but do not ask me any questions.",
                )
            )
        )
        prompt = render_response_policy_prompt_v0_2(result)

        self.assertEqual(result.interaction, Interaction.GUIDED_REFLECTION)
        self.assertEqual(result.question_policy, QuestionPolicy.FORBIDDEN)
        self.assertIn("Ask none", prompt.content)
        self.assertIn("concise synthesis alone", prompt.content)

    def test_one_non_leading_question_is_optional(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "Help me think through the tradeoff between training harder "
                    "and keeping evenings free.",
                )
            )
        )
        prompt = render_response_policy_prompt_v0_2(result)

        self.assertEqual(
            result.question_policy,
            QuestionPolicy.OPTIONAL_ONE_NON_LEADING,
        )
        self.assertIn("ask zero or one non-leading question", prompt.content)
        self.assertIn("A question is not required", prompt.content)
        self.assertEqual(result.closure, Closure.GUIDED_REFLECTION)

    def test_direct_advice_precedes_reflection(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "Help me think this through, then give me your recommendation.",
                )
            )
        )

        self.assertEqual(result.interaction, Interaction.DIRECT)
        self.assertEqual(result.question_policy, QuestionPolicy.NOT_APPLICABLE)
        self.assertFalse(result.fm_ir_020_eligible)

    def test_explicit_change_design_selects_intervention(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "Help me design one reversible change and track whether it helps.",
                )
            )
        )
        prompt = render_response_policy_prompt_v0_2(result)

        self.assertEqual(
            result.interaction,
            Interaction.BEHAVIORAL_INTERVENTION,
        )
        self.assertTrue(result.fm_ir_020_eligible)
        self.assertEqual(result.closure, Closure.CONSENTED_COACHING)
        self.assertIn("observation window", prompt.content)
        self.assertIn("stop rule", prompt.content)

    def test_technical_reflection_is_not_reclassified_as_coaching(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "Help me think through the tradeoff between a synchronous API "
                    "bridge and an event queue. Do not design it for me yet.",
                )
            )
        )
        prompt = render_response_policy_prompt_v0_2(result)

        self.assertEqual(result.response_mode, ResponseMode.TECHNICAL)
        self.assertEqual(result.interaction, Interaction.GUIDED_REFLECTION)
        self.assertIn("Use factual technical reasoning", prompt.content)
        self.assertIn("not an intervention plan", prompt.content)
        self.assertNotIn("observation window", prompt.content)

    def test_multi_turn_reflection_can_continue(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "Help me think through whether this goal is still mine.",
                ),
                (
                    ConversationRole.ASSISTANT,
                    "You are separating the goal from other people's recognition.",
                ),
                (
                    ConversationRole.USER,
                    "What matters is whether I would choose it if nobody knew.",
                ),
            ),
            guided_reflection_requested=True,
        )

        self.assertEqual(result.interaction, Interaction.GUIDED_REFLECTION)
        self.assertFalse(result.fm_ir_020_eligible)

    def test_multi_turn_reflection_exits_to_direct_advice(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "Help me think through whether mornings fit me better.",
                ),
                (
                    ConversationRole.ASSISTANT,
                    "You are weighing consistency against how mornings feel.",
                ),
                (
                    ConversationRole.USER,
                    "I considered it. Now give me your recommendation.",
                ),
            )
        )

        self.assertEqual(result.interaction, Interaction.DIRECT)

    def test_multi_turn_reflection_exits_to_requested_intervention(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "Help me understand why I miss evening workouts.",
                ),
                (
                    ConversationRole.ASSISTANT,
                    "You are noticing that the timing may conflict with your energy.",
                ),
                (
                    ConversationRole.USER,
                    "Now help me design one reversible change and track whether it helps.",
                ),
            )
        )

        self.assertEqual(
            result.interaction,
            Interaction.BEHAVIORAL_INTERVENTION,
        )

    def test_explicit_refusal_of_questions_is_respected(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "Think this through with me, but don't ask further questions.",
                )
            )
        )

        self.assertEqual(result.interaction, Interaction.GUIDED_REFLECTION)
        self.assertEqual(result.question_policy, QuestionPolicy.FORBIDDEN)
        self.assertIn("user_declined_questions", result.interaction_reasons)

    def test_high_stakes_forces_direct_interaction(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "I have crushing chest pain. Help me reflect on whether I should wait.",
                )
            ),
            guided_reflection_requested=True,
        )

        self.assertEqual(result.response_mode, ResponseMode.HIGH_STAKES)
        self.assertEqual(result.interaction, Interaction.DIRECT)
        self.assertFalse(result.fm_ir_020_eligible)

    def test_application_boundary_vetoes_intervention(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "Help me design an experiment without consent to prove another "
                    "person's consent is not separate from mine.",
                )
            ),
            behavioral_intervention_requested=True,
        )

        self.assertEqual(result.interaction, Interaction.DIRECT)
        self.assertFalse(result.fm_ir_020_eligible)
        self.assertIn(
            "fm_application_boundary_forces_direct",
            result.interaction_reasons,
        )

    def test_fm_ir_020_is_unavailable_during_fm_reflection(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "Use Fractal Monism to help me reflect on this pattern, but do "
                    "not turn it into a change plan.",
                )
            )
        )
        prompt = render_response_policy_prompt_v0_2(result)

        self.assertEqual(result.response_mode, ResponseMode.FM_EXPLICIT)
        self.assertEqual(result.interaction, Interaction.GUIDED_REFLECTION)
        self.assertFalse(result.fm_ir_020_eligible)
        self.assertIn("canonical Fractal Monism", prompt.content)
        self.assertNotIn("observation window", prompt.content)

    def test_prompt_carries_content_free_interaction_bindings(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "Help me think through the tradeoff.",
                )
            )
        )
        prompt = render_response_policy_prompt_v0_2(result)

        self.assertEqual(prompt.interaction, Interaction.GUIDED_REFLECTION)
        self.assertRegex(prompt.interaction_instruction_sha256, r"^[0-9a-f]{64}$")
        self.assertRegex(prompt.closure_instruction_sha256, r"^[0-9a-f]{64}$")
        self.assertNotIn(
            "Help me think through the tradeoff.",
            prompt.model_dump_json(exclude={"content"}),
        )


if __name__ == "__main__":
    unittest.main()
