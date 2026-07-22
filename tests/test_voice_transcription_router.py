from __future__ import annotations

import os
import unittest
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient

from rag_engine import voice_realtime_router as realtime
from rag_engine import voice_transcription_router as transcription


ACTOR = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OTHER_ACTOR = "557ea042-cb82-48f8-9429-472e96c957ef"


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

    def _headers(self, *, owner: str = ACTOR, content_type: str = "audio/webm") -> dict[str, str]:
        return {
            "x-vs-actor-user-id": ACTOR,
            "x-vs-owner-user-id": owner,
            "content-type": content_type,
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
        self.assertEqual(response.headers["cache-control"], "no-store")

        call = FakeAsyncClient.calls[0]
        self.assertEqual(call["data"]["model"], "gpt-4o-transcribe")
        self.assertEqual(call["data"]["response_format"], "json")
        self.assertIn("Fractal Monism v0.2", call["data"]["prompt"])
        self.assertIn("LifeSwitch", call["data"]["prompt"])
        self.assertEqual(call["files"]["file"][0], "voice.webm")
        self.assertTrue(
            call["headers"]["OpenAI-Safety-Identifier"].startswith("vs1_")
        )
        self.assertNotIn(ACTOR, call["headers"]["OpenAI-Safety-Identifier"])

    def test_direct_realtime_generation_is_retired_by_default(self) -> None:
        with patch.object(realtime, "ALLOW_UNGOVERNED_REALTIME_VOICE", False):
            with self.assertRaises(HTTPException) as raised:
                realtime._require_ungoverned_realtime_enabled()

        self.assertEqual(raised.exception.status_code, 410)
        self.assertEqual(
            raised.exception.detail,
            "direct_realtime_generation_retired_use_governed_voice",
        )
        self.assertFalse(
            realtime.get_realtime_capabilities()[
                "conversation_generation_enabled"
            ]
        )

    def test_realtime_transcription_policy_cannot_generate_answers(self) -> None:
        session = realtime._transcription_session_config()

        self.assertEqual(session["type"], "transcription")
        self.assertEqual(
            session["audio"]["input"]["transcription"]["model"],
            "gpt-realtime-whisper",
        )
        self.assertIsNone(session["audio"]["input"]["turn_detection"])
        self.assertNotIn("instructions", session)
        self.assertNotIn("model", session)
        self.assertNotIn("output", session["audio"])

    def test_realtime_transcription_requires_owner_actor_equality(self) -> None:
        app = FastAPI()
        app.include_router(realtime.router)
        client = TestClient(app)

        response = client.post(
            "/voice/openai/transcription-webrtc-offer?dry_run=1",
            headers={
                "x-vs-actor-user-id": ACTOR,
                "x-vs-owner-user-id": OTHER_ACTOR,
                "content-type": "application/sdp",
            },
            content=b"v=0\r\n",
        )

        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
