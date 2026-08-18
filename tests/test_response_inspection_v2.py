from __future__ import annotations

import unittest

from seebx.capabilities.conversation.composition import (
    ConversationResponseComposer,
)
from seebx.capabilities.conversation.inspection import build_response_inspection_v2
from tests.test_conversation_composition import (
    ANSWER,
    CORRELATION,
    CombinedOpenAIClient,
    SnapshotConn,
    classifier_output,
    command,
)


class ResponseInspectionV2Tests(unittest.IsolatedAsyncioTestCase):
    async def test_conversational_decision_is_visible_without_content(self) -> None:
        private_text = (
            "So Monday was the only notable outlier and the rest of my macros "
            "were near target."
        )
        client = CombinedOpenAIClient(
            classifier_output(
                coaching=True,
                direct_response_requested=False,
                guided_reflection_requested=False,
                behavioral_intervention_requested=False,
            )
        )
        root = ConversationResponseComposer(
            openai_client=client,
            classifier_model="gpt-5.1",
            answer_id_factory=lambda: ANSWER,
            correlation_id_factory=lambda: CORRELATION,
        )
        execution = await root.execute_detailed(
            SnapshotConn(),
            command(private_text),
        )

        inspection = build_response_inspection_v2(
            trusted_plan=execution.trusted_plan,
            provider_response=execution.provider_response,
            finalized=execution.finalized,
            transcript_persistence="persisted",
        )

        self.assertEqual(inspection.contract_version, "response_inspection_v2")
        self.assertEqual(inspection.before_openai.response_mode, "COACHING")
        self.assertEqual(
            inspection.before_openai.interaction_version,
            "response_interaction_v3",
        )
        self.assertEqual(
            inspection.before_openai.interaction,
            "CONVERSATIONAL",
        )
        self.assertEqual(
            inspection.before_openai.question_policy,
            "NOT_APPLICABLE",
        )
        self.assertEqual(
            inspection.before_openai.interaction_reason_codes,
            ("conversational_update_default",),
        )
        wire = inspection.model_dump_json()
        self.assertNotIn(private_text, wire)
        self.assertNotIn("system_prompt", wire)
        self.assertNotIn("request_sha256", wire)

    async def test_direct_request_is_distinguishable_in_safe_trace(self) -> None:
        root = ConversationResponseComposer(
            openai_client=CombinedOpenAIClient(
                classifier_output(
                    coaching=True,
                    direct_response_requested=False,
                )
            ),
            classifier_model="gpt-5.1",
            answer_id_factory=lambda: ANSWER,
            correlation_id_factory=lambda: CORRELATION,
        )
        execution = await root.execute_detailed(
            SnapshotConn(),
            command("What should I do when I miss my protein target?"),
        )

        inspection = build_response_inspection_v2(
            trusted_plan=execution.trusted_plan,
            provider_response=execution.provider_response,
            finalized=execution.finalized,
            transcript_persistence="persisted",
        )

        self.assertEqual(inspection.before_openai.interaction, "DIRECT")
        self.assertEqual(
            inspection.before_openai.interaction_reason_codes,
            ("direct_request_shape",),
        )


if __name__ == "__main__":
    unittest.main()
