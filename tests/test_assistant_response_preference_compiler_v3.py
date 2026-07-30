from __future__ import annotations

from datetime import datetime, timezone
import unittest
from uuid import UUID

from pydantic import ValidationError

from rag_engine.assistant_response_preference_compiler_v1 import (
    ASSISTANT_PREFERENCE_COMPILER_VERSION,
    AssistantPreferenceCompilationCandidateV1,
    _CompilerModelOutput,
    build_compilation_candidate_v1,
)
from rag_engine.assistant_response_preferences_v1 import (
    MAX_COMPILED_PREFERENCE_RULES,
    AssistantResponsePreferencesV1,
    CompiledPreferenceRuleId,
    ConversationStyle,
    PreferenceSource,
    ResponseFormat,
    ResponseLength,
    TechnicalDepth,
    compiled_preference_marker_v1,
    render_assistant_response_preferences_v1,
)
from rag_engine.response_policy_v0_2 import ResponseMode


OWNER = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
NOW = datetime(2026, 7, 30, 20, 0, tzinfo=timezone.utc)


class AssistantResponsePreferenceCompilerV3Tests(unittest.TestCase):
    def test_version_and_rule_ceiling_are_explicit(self) -> None:
        self.assertEqual(
            ASSISTANT_PREFERENCE_COMPILER_VERSION,
            "assistant_preference_compiler_v3",
        )
        self.assertEqual(MAX_COMPILED_PREFERENCE_RULES, 12)
        values = list(CompiledPreferenceRuleId)[:MAX_COMPILED_PREFERENCE_RULES]
        output = _CompilerModelOutput(
            response_length=None,
            technical_depth=None,
            response_format=None,
            conversation_style=None,
            rule_ids=values,
            rejected_reason_codes=[],
        )
        self.assertEqual(len(output.rule_ids), 12)
        with self.assertRaises(ValidationError):
            _CompilerModelOutput(
                response_length=None,
                technical_depth=None,
                response_format=None,
                conversation_style=None,
                rule_ids=[
                    *values,
                    CompiledPreferenceRuleId.CALM_PATIENT_TONE,
                ],
                rejected_reason_codes=[],
            )

    def test_nuanced_tone_rules_render_fixed_server_text(self) -> None:
        candidate = build_compilation_candidate_v1(
            owner_user_id=OWNER,
            source_revision=3,
            source_narrative=(
                "Use short conversational prose. Stay calm and patient, and "
                "occasionally be poetic when the topic is reflective."
            ),
            output=_CompilerModelOutput(
                response_length=ResponseLength.CONCISE,
                technical_depth=None,
                response_format=ResponseFormat.PROSE,
                conversation_style=ConversationStyle.NATURAL,
                rule_ids=[
                    CompiledPreferenceRuleId.CALM_PATIENT_TONE,
                    CompiledPreferenceRuleId.CONTEXTUAL_POETIC_LANGUAGE,
                ],
                rejected_reason_codes=[],
            ),
            provider_model="gpt-5.6-test",
            provider_response_id="resp_test",
            now=NOW,
        )
        self.assertIsInstance(
            candidate,
            AssistantPreferenceCompilationCandidateV1,
        )
        self.assertIn(
            "Uses a calm, patient tone without becoming placating.",
            candidate.summary,
        )
        self.assertIn(
            "Uses restrained poetic phrasing when it naturally fits.",
            candidate.summary,
        )
        self.assertEqual(
            candidate.compiler_version,
            "assistant_preference_compiler_v3",
        )

    def test_summary_capacity_covers_four_settings_and_twelve_rules(self) -> None:
        candidate = build_compilation_candidate_v1(
            owner_user_id=OWNER,
            source_revision=1,
            source_narrative="Synthetic maximum supported preference plan.",
            output=_CompilerModelOutput(
                response_length=ResponseLength.CONCISE,
                technical_depth=TechnicalDepth.EXPERT,
                response_format=ResponseFormat.PROSE,
                conversation_style=ConversationStyle.NATURAL,
                rule_ids=list(CompiledPreferenceRuleId)[:12],
                rejected_reason_codes=[],
            ),
            provider_model="gpt-5.6-test",
            provider_response_id="resp_test",
            now=NOW,
        )
        self.assertEqual(len(candidate.summary), 16)

    def test_new_rules_render_as_fixed_text_and_high_stakes_suppresses_them(
        self,
    ) -> None:
        preferences = AssistantResponsePreferencesV1(
            owner_user_id=OWNER,
            revision=1,
            source=PreferenceSource.POSTGRES,
            updated_at=NOW,
            custom_instructions=compiled_preference_marker_v1(
                [
                    CompiledPreferenceRuleId.CALM_PATIENT_TONE,
                    CompiledPreferenceRuleId.CONTEXTUAL_POETIC_LANGUAGE,
                ]
            ),
        )
        ordinary, ordinary_inspection = render_assistant_response_preferences_v1(
            preferences,
            ResponseMode.ORDINARY,
        )
        self.assertIn("Maintain a calm, patient tone", ordinary)
        self.assertIn("occasional restrained poetic phrasing", ordinary)
        self.assertTrue(ordinary_inspection.custom_instructions_included)

        high_stakes, high_stakes_inspection = (
            render_assistant_response_preferences_v1(
                preferences,
                ResponseMode.HIGH_STAKES,
            )
        )
        self.assertNotIn("calm, patient tone", high_stakes)
        self.assertNotIn("poetic phrasing", high_stakes)
        self.assertTrue(high_stakes_inspection.high_stakes_override)


if __name__ == "__main__":
    unittest.main()
