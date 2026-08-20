from __future__ import annotations

import unittest

from seebx.capabilities.conversation.prompt import ASSEMBLY_MANIFEST_VERSION
from seebx.capabilities.conversation.orchestration import (
    ORCHESTRATOR_VERSION,
    SHADOW_TRACE_VERSION,
    TRUSTED_PLAN_VERSION,
    TRUSTED_POLICY_SIGNALS_ENVELOPE_VERSION,
)
from seebx.capabilities.conversation.policy_instructions import (
    RESPONSE_INTERACTION_VERSION,
    RESPONSE_POLICY_PROMPT_VERSION,
    render_response_policy_prompt_v0_2,
)
from seebx.capabilities.conversation.policy import (
    Closure,
    ControllingPolicyDisposition,
    ConversationRole,
    Interaction,
    POLICY_DECISION_VERSION,
    POLICY_SIGNALS_VERSION,
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
        self.assertFalse(result.intervention_authorized)
        self.assertFalse(result.fm_ir_020_eligible)
        self.assertEqual(result.closure, Closure.COMPLETE)
        self.assertIn("not consent to carry out", prompt.content)
        self.assertIn("present only a proposed option", prompt.content)
        self.assertNotIn("The user has consented", prompt.content)

    def test_specific_experiment_consent_activates_fm_ir_020(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "I consent to the specific proposed one-week tracking "
                    "experiment and its stop rule.",
                )
            ),
            coaching=True,
            behavioral_intervention_requested=True,
            coaching_consent=True,
            specific_experiment_consent=True,
            experiment_reversible_and_proportionate=True,
            experiment_measurement_defined=True,
            experiment_adverse_indicators_defined=True,
            experiment_stop_rule_defined=True,
        )
        prompt = render_response_policy_prompt_v0_2(result)

        self.assertEqual(
            result.interaction,
            Interaction.BEHAVIORAL_INTERVENTION,
        )
        self.assertTrue(result.intervention_authorized)
        self.assertTrue(result.fm_ir_020_eligible)
        self.assertEqual(result.closure, Closure.CONSENTED_COACHING)
        self.assertIn(
            "specific_experiment_consent_confirmed",
            result.interaction_reasons,
        )
        self.assertIn("The user has consented", prompt.content)

    def test_specific_consent_without_all_typed_prerequisites_stays_closed(
        self,
    ) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "I consent to the proposed experiment, but we have not "
                    "defined measurement, adverse indicators, or a stop rule.",
                )
            ),
            behavioral_intervention_requested=True,
            coaching_consent=True,
            specific_experiment_consent=True,
            experiment_reversible_and_proportionate=True,
            experiment_measurement_defined=False,
            experiment_adverse_indicators_defined=False,
            experiment_stop_rule_defined=False,
        )
        prompt = render_response_policy_prompt_v0_2(result)

        self.assertEqual(
            result.interaction,
            Interaction.BEHAVIORAL_INTERVENTION,
        )
        self.assertFalse(result.intervention_authorized)
        self.assertFalse(result.fm_ir_020_eligible)
        self.assertEqual(result.closure, Closure.COMPLETE)
        self.assertIn(
            "fm_ir_020_prerequisites_incomplete",
            result.interaction_reasons,
        )
        self.assertNotIn("The user has consented", prompt.content)

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

    def test_technical_project_plan_stays_direct(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "Help me design a plan to migrate the backend from "
                    "PostgreSQL 15 to PostgreSQL 16.",
                )
            )
        )
        prompt = render_response_policy_prompt_v0_2(result)

        self.assertEqual(result.response_mode, ResponseMode.TECHNICAL)
        self.assertEqual(result.interaction, Interaction.DIRECT)
        self.assertFalse(result.intervention_authorized)
        self.assertFalse(result.fm_ir_020_eligible)
        self.assertEqual(
            result.controlling_policy_disposition,
            ControllingPolicyDisposition.NONE,
        )
        self.assertNotIn("behavioral intervention", prompt.content.lower())

    def test_general_intervention_authorization_is_separate_from_fm(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "For my software work, use the already agreed reversible "
                    "one-week focus experiment, measure, adverse indicators, "
                    "and stop rule.",
                )
            ),
            technical=True,
            behavioral_intervention_requested=True,
            coaching_consent=True,
            specific_experiment_consent=True,
            experiment_reversible_and_proportionate=True,
            experiment_measurement_defined=True,
            experiment_adverse_indicators_defined=True,
            experiment_stop_rule_defined=True,
        )

        self.assertEqual(result.response_mode, ResponseMode.TECHNICAL)
        self.assertEqual(
            result.interaction,
            Interaction.BEHAVIORAL_INTERVENTION,
        )
        self.assertTrue(result.intervention_authorized)
        self.assertFalse(result.fm_ir_020_eligible)
        self.assertEqual(result.closure, Closure.CONSENTED_COACHING)

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

    def test_intervention_respects_global_question_refusal(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "Design an experiment for me, but do not ask me any questions.",
                )
            )
        )
        prompt = render_response_policy_prompt_v0_2(result)

        self.assertEqual(
            result.interaction,
            Interaction.BEHAVIORAL_INTERVENTION,
        )
        self.assertEqual(result.question_policy, QuestionPolicy.FORBIDDEN)
        self.assertFalse(result.fm_ir_020_eligible)
        self.assertIn("user_declined_questions", result.interaction_reasons)
        self.assertIn("Ask none", prompt.content)
        self.assertNotIn("The user has consented", prompt.content)

    def test_design_request_without_specific_consent_keeps_gate_closed(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "Help me design a plan. I do not consent to any experiment "
                    "or tracking yet.",
                )
            ),
            behavioral_intervention_requested=True,
            coaching_consent=False,
            specific_experiment_consent=False,
        )
        prompt = render_response_policy_prompt_v0_2(result)

        self.assertEqual(
            result.interaction,
            Interaction.BEHAVIORAL_INTERVENTION,
        )
        self.assertFalse(result.fm_ir_020_eligible)
        self.assertEqual(result.closure, Closure.COMPLETE)
        self.assertIn(
            "specific_experiment_consent_missing",
            result.interaction_reasons,
        )
        self.assertNotIn("The user has consented", prompt.content)

    def test_negated_intervention_language_does_not_override_reflection(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "Help me think through why I miss my target. Do not help me "
                    "design a plan or track whether it helps.",
                )
            )
        )
        prompt = render_response_policy_prompt_v0_2(result)

        self.assertEqual(result.interaction, Interaction.GUIDED_REFLECTION)
        self.assertFalse(result.fm_ir_020_eligible)
        self.assertEqual(result.closure, Closure.GUIDED_REFLECTION)
        self.assertNotIn("The user has consented", prompt.content)

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
        self.assertFalse(result.intervention_authorized)
        self.assertFalse(result.fm_ir_020_eligible)
        self.assertEqual(
            result.controlling_policy_disposition,
            ControllingPolicyDisposition.DEFER_TO_CONTROLLING_POLICY,
        )

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
        self.assertFalse(result.intervention_authorized)
        self.assertFalse(result.fm_ir_020_eligible)
        self.assertEqual(
            result.controlling_policy_disposition,
            ControllingPolicyDisposition.FM_APPLICATION_VETO,
        )
        self.assertIn(
            "fm_application_boundary_forces_direct",
            result.interaction_reasons,
        )

    def test_fm_ir_020_is_unavailable_during_fm_reflection(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "Use Relational Monism to help me reflect on this pattern, but do "
                    "not turn it into a change plan.",
                )
            )
        )
        prompt = render_response_policy_prompt_v0_2(result)

        self.assertEqual(result.response_mode, ResponseMode.FM_EXPLICIT)
        self.assertEqual(result.interaction, Interaction.GUIDED_REFLECTION)
        self.assertFalse(result.fm_ir_020_eligible)
        self.assertIn("canonical Relational Monism", prompt.content)
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

    def test_all_six_accepted_reflection_prompts_select_reflection(self) -> None:
        accepted = (
            "Help me think through what I learned from a difficult quarter. I "
            "learned that resilience matters, that we sometimes need to pivot "
            "quickly, and that clear communication kept the team aligned when "
            "plans changed.",
            "Guide me with one question about my time management. I get the work "
            "done, but I am always stressed and racing the clock. I am considering "
            "smaller tasks or using a timer.",
            "Help me get unstuck creatively. I want to explore abstract work, "
            "try a new medium, and blend visual art with storytelling, but I keep "
            "returning to what is comfortable.",
            "Help me think through my sales problem. Emails and calls are reaching "
            "fewer decision-makers, engagement is down, and the market is more "
            "competitive. I do not yet know whether the problem is targeting, "
            "message, channel, or offer.",
            "Ask me one useful question about making this bridge design last. I "
            "am considering durable low-maintenance materials, safety redundancy, "
            "and future loads.",
            "Help me think through how to explore majors before committing. I "
            "could take electives, try internships or volunteer work, and talk "
            "with professors and professionals.",
        )

        for index, message in enumerate(accepted, start=1):
            with self.subTest(case=f"REFL-{index:03d}"):
                result = decide(request((ConversationRole.USER, message)))
                self.assertEqual(
                    result.interaction,
                    Interaction.GUIDED_REFLECTION,
                )
                self.assertFalse(result.fm_ir_020_eligible)

    def test_candidate_contract_versions_are_not_silent_extensions(self) -> None:
        result = decide(
            request(
                (
                    ConversationRole.USER,
                    "Ask me one useful question about this tradeoff.",
                )
            )
        )
        prompt = render_response_policy_prompt_v0_2(result)

        self.assertEqual(result.policy_version, "response_policy_v0_2")
        self.assertIsInstance(result.intervention_authorized, bool)
        self.assertEqual(
            result.controlling_policy_disposition,
            ControllingPolicyDisposition.NONE,
        )
        self.assertEqual(
            result.contract_version,
            "response_policy_decision_v0_4",
        )
        self.assertEqual(
            POLICY_DECISION_VERSION,
            "response_policy_decision_v0_4",
        )
        self.assertEqual(
            POLICY_SIGNALS_VERSION,
            "response_policy_signals_v0_3",
        )
        self.assertEqual(
            RESPONSE_POLICY_PROMPT_VERSION,
            "response_policy_prompt_v0_7",
        )
        self.assertEqual(RESPONSE_INTERACTION_VERSION, "response_interaction_v3")
        self.assertEqual(
            prompt.contract_version,
            "response_policy_prompt_v0_7",
        )
        self.assertEqual(prompt.interaction_version, "response_interaction_v3")
        self.assertEqual(
            ASSEMBLY_MANIFEST_VERSION,
            "prompt_assembly_manifest_v7",
        )
        self.assertEqual(
            SHADOW_TRACE_VERSION,
            "conversation_response_shadow_trace_v1",
        )
        self.assertEqual(
            TRUSTED_PLAN_VERSION,
            "trusted_response_plan_v0_7",
        )
        self.assertEqual(
            ORCHESTRATOR_VERSION,
            "trusted_response_orchestrator_v0_7",
        )
        self.assertEqual(
            TRUSTED_POLICY_SIGNALS_ENVELOPE_VERSION,
            "trusted_response_policy_signals_envelope_v0_3",
        )


if __name__ == "__main__":
    unittest.main()
