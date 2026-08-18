from __future__ import annotations

import unittest
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from seebx.core.voice_observability import (
    voice_turn_id_from_request,
    voice_turn_response_headers,
)


VOICE_TURN = "0fc3d70a-a6d0-4e55-9e39-20e060b416c8"


class VoiceObservabilityV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()

        @app.get("/probe")
        async def probe(req: Request):
            voice_turn_id = voice_turn_id_from_request(req)
            return JSONResponse(
                {"present": voice_turn_id is not None},
                headers=voice_turn_response_headers(voice_turn_id),
            )

        self.client = TestClient(app)

    def test_canonical_module_has_no_legacy_wrapper(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        self.assertTrue(
            (repository / "seebx/core/voice_observability.py").is_file()
        )
        self.assertFalse(
            (repository / "rag_engine/voice_observability_v1.py").exists()
        )

    def test_accepts_and_echoes_uuid(self) -> None:
        response = self.client.get(
            "/probe", headers={"x-vs-voice-turn-id": VOICE_TURN}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["present"])
        self.assertEqual(response.headers["x-vs-voice-turn-id"], VOICE_TURN)

    def test_absent_header_is_not_synthesized(self) -> None:
        response = self.client.get("/probe")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["present"])
        self.assertNotIn("x-vs-voice-turn-id", response.headers)

    def test_rejects_non_uuid(self) -> None:
        response = self.client.get(
            "/probe", headers={"x-vs-voice-turn-id": "../../unsafe"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "invalid_voice_turn_id")


if __name__ == "__main__":
    unittest.main()
