from __future__ import annotations

import unittest

from rag_engine.response_policy_prompt_v0_2 import (
    render_response_policy_prompt_v0_2,
)
from seebx.capabilities.conversation.policy import (
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
) -> ResponsePolicyInputV0_2:
    return ResponsePolicyInputV0_2.create(
        request_id="conversational-interaction-test",
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


class ConversationalInteractionV1Tests(unittest.TestCase):
    def test_coaching_status_update_does_not_become_advice(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "All the rest of my macros were pretty good. I was a little "
                    "low on calories Monday too.",
                )
            ),
            coaching=True,
            direct_response_requested=False,
            guided_reflection_requested=False,
            behavioral_intervention_requested=False,
        )
        prompt = render_response_policy_prompt_v0_2(result)

        self.assertEqual(result.response_mode, ResponseMode.COACHING)
        self.assertEqual(result.interaction, Interaction.CONVERSATIONAL)
        self.assertEqual(result.closure, Closure.COMPLETE)
        self.assertEqual(result.question_policy, QuestionPolicy.NOT_APPLICABLE)
        self.assertEqual(
            result.interaction_reasons,
            ("conversational_update_default",),
        )
        self.assertFalse(result.fm_ir_020_eligible)
        self.assertIn("not a request for advice", prompt.content)
        self.assertIn("Do not prescribe", prompt.content)
        self.assertIn("repeat prior advice", prompt.content)
        self.assertIn("or ask a question", prompt.content)
        self.assertNotIn("The current turn requests an answer", prompt.content)

    def test_multi_turn_prior_coaching_does_not_control_current_update(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "What should I do when I miss my macro target?",
                ),
                (
                    ConversationRole.ASSISTANT,
                    "Resume your usual targets without compensating.",
                ),
                (
                    ConversationRole.USER,
                    "So Monday was the only notable outlier and the rest of the "
                    "week was near target.",
                ),
            ),
            coaching=True,
            direct_response_requested=False,
            guided_reflection_requested=False,
            behavioral_intervention_requested=False,
        )

        self.assertEqual(result.response_mode, ResponseMode.COACHING)
        self.assertEqual(result.interaction, Interaction.CONVERSATIONAL)
        self.assertIn(
            "conversational_update_default",
            result.interaction_reasons,
        )

    def test_question_shape_stays_direct_when_provider_signal_is_false(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "What should I do when I miss my protein target?",
                )
            ),
            coaching=True,
            direct_response_requested=False,
            guided_reflection_requested=False,
            behavioral_intervention_requested=False,
        )

        self.assertEqual(result.interaction, Interaction.DIRECT)
        self.assertIn("direct_request_shape", result.interaction_reasons)

    def test_imperative_request_without_question_mark_stays_direct(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "Explain how weekly protein averages work",
                )
            ),
            direct_response_requested=False,
        )

        self.assertEqual(result.interaction, Interaction.DIRECT)
        self.assertIn("direct_request_shape", result.interaction_reasons)

    def test_guided_reflection_still_precedes_conversational_default(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "Help me think through why Monday bothered me.",
                )
            )
        )

        self.assertEqual(result.interaction, Interaction.GUIDED_REFLECTION)
        self.assertEqual(result.closure, Closure.GUIDED_REFLECTION)

    def test_intervention_still_precedes_conversational_default(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "Help me design a plan to hit my protein target.",
                )
            )
        )

        self.assertEqual(
            result.interaction,
            Interaction.BEHAVIORAL_INTERVENTION,
        )
        self.assertFalse(result.intervention_authorized)

    def test_technical_status_update_can_be_conversational(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "The backend deployment finished and the service is healthy.",
                )
            ),
            technical=True,
            direct_response_requested=False,
        )

        self.assertEqual(result.response_mode, ResponseMode.TECHNICAL)
        self.assertEqual(result.interaction, Interaction.CONVERSATIONAL)

    def test_high_stakes_still_forces_direct(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "I have crushing chest pain and cannot breathe.",
                )
            ),
            direct_response_requested=False,
        )

        self.assertEqual(result.response_mode, ResponseMode.HIGH_STAKES)
        self.assertEqual(result.interaction, Interaction.DIRECT)
        self.assertIn("high_stakes_forces_direct", result.interaction_reasons)

    def test_conversational_update_preserves_explicit_question_refusal(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "Monday was the only outlier. Do not ask me any questions.",
                )
            )
        )
        prompt = render_response_policy_prompt_v0_2(result)

        self.assertEqual(result.interaction, Interaction.CONVERSATIONAL)
        self.assertEqual(result.question_policy, QuestionPolicy.FORBIDDEN)
        self.assertIn("user_declined_questions", result.interaction_reasons)
        self.assertIn("Ask none", prompt.content)

    def test_ordinary_greeting_uses_conversational_interaction(self) -> None:
        result = decide(
            request((ConversationRole.USER, "Hello."))
        )

        self.assertEqual(result.response_mode, ResponseMode.ORDINARY)
        self.assertEqual(result.interaction, Interaction.CONVERSATIONAL)


if __name__ == "__main__":
    unittest.main()
