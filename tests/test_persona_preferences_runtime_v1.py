from __future__ import annotations

import json
import unittest

from rag_engine.persona_loader import (
    BASE_PERSONA,
    USER_PREFERENCES_MARKER,
    _format_user_instructions_text,
)


class PersonaPreferencesRuntimeV1Tests(unittest.TestCase):
    def test_base_persona_requires_independent_judgment(self) -> None:
        self.assertIn("Do not agree merely to validate", BASE_PERSONA)
        self.assertIn("Avoid flattery", BASE_PERSONA)
        self.assertIn("must not change factual conclusions", BASE_PERSONA)

    def test_preferences_render_style_and_fixed_neutral_policy(self) -> None:
        rendered = _format_user_instructions_text(
            USER_PREFERENCES_MARKER
            + json.dumps(
                {
                    "conversation_style": "warm",
                    "response_length": "concise",
                    "technical_depth": "expert",
                    "format": "prose",
                    "encouragement": "minimal",
                }
            )
        )

        self.assertIn("Conversation style: warm", rendered)
        self.assertIn("without becoming flattering", rendered)
        self.assertIn("Encouragement is fixed to neutral", rendered)
        self.assertNotIn("encouragement: minimal", rendered.lower())


if __name__ == "__main__":
    unittest.main()
