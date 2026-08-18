from __future__ import annotations

import unittest
from pathlib import Path

from seebx.capabilities.conversation.router import RESPONSE_QUERY_DEADLINE_SECONDS


ROOT = Path(__file__).resolve().parents[1]


class VoiceResilienceContractTests(unittest.TestCase):
    def test_response_query_has_bounded_deadline(self) -> None:
        self.assertGreaterEqual(RESPONSE_QUERY_DEADLINE_SECONDS, 30.0)
        self.assertLessEqual(RESPONSE_QUERY_DEADLINE_SECONDS, 120.0)
        source = (ROOT / "seebx/capabilities/conversation/router.py").read_text()
        self.assertIn("asyncio.wait_for(", source)
        self.assertIn("response_generation_timeout", source)

    def test_transcription_timeout_is_publicly_sanitized(self) -> None:
        capability_source = (
            ROOT / "seebx/capabilities/voice/transcription.py"
        ).read_text()
        adapter_source = (
            ROOT / "seebx/adapters/openai_transcription.py"
        ).read_text()
        self.assertIn(
            "except OpenAITranscriptionTimeoutError",
            capability_source,
        )
        self.assertIn("openai_transcription_timeout", capability_source)
        self.assertIn("except httpx.TimeoutException", adapter_source)
        self.assertNotIn("str(exc)", capability_source)
        self.assertNotIn("str(exc)", adapter_source)

    def test_tts_timeout_is_publicly_sanitized(self) -> None:
        source = (ROOT / "rag_engine/voice_tts_router.py").read_text()
        self.assertIn("except httpx.TimeoutException", source)
        self.assertIn("openai_tts_timeout", source)
        self.assertNotIn("str(exc)", source)


if __name__ == "__main__":
    unittest.main()
