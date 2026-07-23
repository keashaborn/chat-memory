from __future__ import annotations

import unittest
from pathlib import Path

from rag_engine.resse_response_router import RESPONSE_QUERY_DEADLINE_SECONDS


ROOT = Path(__file__).resolve().parents[1]


class VoiceResilienceContractTests(unittest.TestCase):
    def test_response_query_has_bounded_deadline(self) -> None:
        self.assertGreaterEqual(RESPONSE_QUERY_DEADLINE_SECONDS, 30.0)
        self.assertLessEqual(RESPONSE_QUERY_DEADLINE_SECONDS, 120.0)
        source = (ROOT / "rag_engine/resse_response_router.py").read_text()
        self.assertIn("asyncio.wait_for(", source)
        self.assertIn("response_generation_timeout", source)

    def test_transcription_timeout_is_publicly_sanitized(self) -> None:
        source = (ROOT / "rag_engine/voice_transcription_router.py").read_text()
        self.assertIn("except httpx.TimeoutException", source)
        self.assertIn("openai_transcription_timeout", source)
        self.assertNotIn("str(exc)", source)

    def test_tts_timeout_is_publicly_sanitized(self) -> None:
        source = (ROOT / "rag_engine/voice_tts_router.py").read_text()
        self.assertIn("except httpx.TimeoutException", source)
        self.assertIn("openai_tts_timeout", source)
        self.assertNotIn("str(exc)", source)


if __name__ == "__main__":
    unittest.main()
