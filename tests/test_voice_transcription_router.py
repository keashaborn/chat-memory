from __future__ import annotations

import os
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient

from rag_engine import voice_transcription_router as transcription


ACTOR = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OTHER_ACTOR = "557ea042-cb82-48f8-9429-472e96c957ef"
VOICE_TURN = "0fc3d70a-a6d0-4e55-9e39-20e060b416c8"
VOICE_SESSION = "a872d3f2-2d5c-4ae3-9f02-d9f43a38899e"


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {"text": "What should I prioritize today?"}
        self.headers = {"x-request-id": "openai-request-voice-001"}

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeAsyncClient:
    response = FakeResponse()
    calls: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.__class__.calls.append({"url": url, **kwargs})
        return self.__class__.response


class VoiceTranscriptionRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeAsyncClient.calls = []
        FakeAsyncClient.response = FakeResponse()
        app = FastAPI()
        app.include_router(transcription.router)
        self.client = TestClient(app)
        self.active_lease = patch.object(
            transcription,
            "require_active_voice_session",
            AsyncMock(),
        )
        self.active_lease.start()
        self.addCleanup(self.active_lease.stop)

    def _headers(self, *, owner: str = ACTOR, content_type: str = "audio/webm") -> dict[str, str]:
        return {
            "x-vs-actor-user-id": ACTOR,
            "x-vs-owner-user-id": owner,
            "content-type": content_type,
            "x-vs-voice-turn-id": VOICE_TURN,
            "x-vs-voice-session-id": VOICE_SESSION,
        }

    def test_requires_owner_and_actor_equality(self) -> None:
        missing = self.client.post(
            "/voice/openai/transcribe",
            headers={"x-vs-actor-user-id": ACTOR, "content-type": "audio/webm"},
            content=b"audio",
        )
        self.assertEqual(missing.status_code, 400)

        mismatch = self.client.post(
            "/voice/openai/transcribe",
            headers=self._headers(owner=OTHER_ACTOR),
            content=b"audio",
        )
        self.assertEqual(mismatch.status_code, 403)

    def test_rejects_unsupported_audio_before_openai(self) -> None:
        response = self.client.post(
            "/voice/openai/transcribe",
            headers=self._headers(content_type="application/octet-stream"),
            content=b"audio",
        )
        self.assertEqual(response.status_code, 415)
        self.assertEqual(FakeAsyncClient.calls, [])

    def test_transcribes_with_server_owned_model_and_safety_identifier(self) -> None:
        FakeAsyncClient.response = FakeResponse(
            payload={
                "text": "What should I prioritize today?",
                "logprobs": [
                    {"token": "What", "logprob": -0.05},
                    {"token": " should", "logprob": -1.25},
                    {"token": " I", "logprob": -0.1},
                ],
            }
        )
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-only-key"}),
            patch.object(transcription.httpx, "AsyncClient", FakeAsyncClient),
        ):
            response = self.client.post(
                "/voice/openai/transcribe",
                headers=self._headers(content_type="audio/webm;codecs=opus"),
                content=b"not-real-audio",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["transcript"], "What should I prioritize today?")
        self.assertEqual(response.json()["language"], "en")
        self.assertEqual(
            response.json()["confidence"],
            {
                "token_count": 3,
                "mean_logprob": -0.466667,
                "minimum_logprob": -1.25,
                "low_confidence_token_count": 1,
            },
        )
        self.assertNotIn("logprobs", response.json())
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-vs-voice-turn-id"], VOICE_TURN)

        call = FakeAsyncClient.calls[0]
        self.assertEqual(call["data"]["model"], "gpt-4o-transcribe")
        self.assertEqual(call["data"]["response_format"], "json")
        self.assertEqual(call["data"]["language"], "en")
        self.assertEqual(call["data"]["temperature"], "0")
        self.assertEqual(call["data"]["include[]"], "logprobs")
        self.assertIn("Fractal Monism v0.2", call["data"]["prompt"])
        self.assertIn("LifeSwitch", call["data"]["prompt"])
        self.assertIn("Preserve short questions", call["data"]["prompt"])
        self.assertEqual(call["files"]["file"][0], "voice.webm")
        self.assertTrue(
            call["headers"]["OpenAI-Safety-Identifier"].startswith("vs1_")
        )
        self.assertNotIn(ACTOR, call["headers"]["OpenAI-Safety-Identifier"])

    def test_explicit_language_is_validated_and_sent_to_openai(self) -> None:
        headers = self._headers()
        headers["x-vs-voice-language"] = "es"
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-only-key"}),
            patch.object(transcription.httpx, "AsyncClient", FakeAsyncClient),
        ):
            response = self.client.post(
                "/voice/openai/transcribe",
                headers=headers,
                content=b"not-real-audio",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["language"], "es")
        self.assertEqual(FakeAsyncClient.calls[0]["data"]["language"], "es")
        self.assertIn("Spanish", FakeAsyncClient.calls[0]["data"]["prompt"])

    def test_auto_detect_omits_language_hint(self) -> None:
        headers = self._headers()
        headers["x-vs-voice-language"] = "auto"
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-only-key"}),
            patch.object(transcription.httpx, "AsyncClient", FakeAsyncClient),
        ):
            response = self.client.post(
                "/voice/openai/transcribe",
                headers=headers,
                content=b"not-real-audio",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["language"], "auto")
        self.assertNotIn("language", FakeAsyncClient.calls[0]["data"])

    def test_rejects_unsupported_language_before_openai(self) -> None:
        headers = self._headers()
        headers["x-vs-voice-language"] = "xx"
        response = self.client.post(
            "/voice/openai/transcribe",
            headers=headers,
            content=b"audio",
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"]["error"],
            "unsupported_voice_language",
        )
        self.assertEqual(FakeAsyncClient.calls, [])

    def test_rejects_invalid_voice_turn_id_before_openai(self) -> None:
        headers = self._headers()
        headers["x-vs-voice-turn-id"] = "not-a-uuid"
        response = self.client.post(
            "/voice/openai/transcribe",
            headers=headers,
            content=b"audio",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "invalid_voice_turn_id")
        self.assertEqual(FakeAsyncClient.calls, [])

    def test_only_governed_batch_transcription_is_exposed(self) -> None:
        app = FastAPI()
        app.include_router(transcription.router)
        paths = {route.path for route in app.routes}

        self.assertIn("/voice/openai/transcribe", paths)
        self.assertNotIn("/voice/openai/session", paths)
        self.assertNotIn("/voice/openai/webrtc-offer", paths)
        self.assertNotIn(
            "/voice/openai/transcription-webrtc-offer",
            paths,
        )


if __name__ == "__main__":
    unittest.main()
