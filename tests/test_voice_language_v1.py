from __future__ import annotations

import unittest

from rag_engine.voice_language_v1 import (
    SUPPORTED_VOICE_LANGUAGE_IDS,
    VOICE_LANGUAGES,
    normalize_voice_language,
    response_language_instruction,
    transcription_prompt,
)


class VoiceLanguageV1Tests(unittest.TestCase):
    def test_catalog_is_unique_and_contains_documented_core_languages(self) -> None:
        ids = [item["id"] for item in VOICE_LANGUAGES]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), set(SUPPORTED_VOICE_LANGUAGE_IDS))
        self.assertEqual(ids[0], "auto")
        self.assertTrue(
            {"en", "es", "fr", "de", "pt", "zh", "ja", "ko", "ar"}.issubset(
                SUPPORTED_VOICE_LANGUAGE_IDS
            )
        )

    def test_invalid_client_value_falls_back_only_at_local_preference_boundary(self) -> None:
        self.assertEqual(normalize_voice_language("ES"), "es")
        self.assertEqual(normalize_voice_language("not-supported"), "en")

    def test_response_language_instructions_preserve_governance(self) -> None:
        spanish = response_language_instruction("es")
        automatic = response_language_instruction("auto")
        self.assertIn("Reply in Spanish", spanish)
        self.assertIn("never weakens safety", spanish)
        self.assertIn("current message", automatic)

    def test_transcription_prompt_keeps_product_names(self) -> None:
        prompt = transcription_prompt("fr")
        self.assertIn("French", prompt)
        self.assertIn("Relational Monism v0.4", prompt)
        self.assertIn("Supabase", prompt)
        self.assertNotIn("RESSE", prompt)


if __name__ == "__main__":
    unittest.main()
