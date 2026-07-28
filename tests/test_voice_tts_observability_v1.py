from __future__ import annotations

import os
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from rag_engine import voice_tts_router as tts


ACTOR = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
VOICE_TURN = "0fc3d70a-a6d0-4e55-9e39-20e060b416c8"
VOICE_SESSION = "a872d3f2-2d5c-4ae3-9f02-d9f43a38899e"


class FakeResponse:
    status_code = 200
    headers = {"x-request-id": "openai-request-tts-001"}

    def __init__(self) -> None:
        self.closed = False

    async def aiter_raw(self):
        yield b"fake-"
        yield b"pcm"

    async def aread(self) -> bytes:
        return b""

    async def aclose(self) -> None:
        self.closed = True


class FakeAsyncClient:
    calls: list[dict[str, Any]] = []
    instances: list["FakeAsyncClient"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.closed = False
        self.response = FakeResponse()
        self.__class__.instances.append(self)

    def build_request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        request = {"method": method, "url": url, **kwargs}
        self.__class__.calls.append(request)
        return request

    async def send(self, request: dict[str, Any], **kwargs: Any) -> FakeResponse:
        self.__class__.calls[-1]["send"] = kwargs
        return self.response

    async def aclose(self) -> None:
        self.closed = True


class VoiceTTSObservabilityV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        FakeAsyncClient.calls = []
        FakeAsyncClient.instances = []
        app = FastAPI()
        app.include_router(tts.router)
        self.client = TestClient(app)
        self.active_lease = patch.object(
            tts,
            "require_active_voice_session",
            AsyncMock(),
        )
        self.active_lease.start()
        self.addCleanup(self.active_lease.stop)

    def test_echoes_voice_turn_and_provider_request_id(self) -> None:
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-only-key"}),
            patch.object(tts.httpx, "AsyncClient", FakeAsyncClient),
        ):
            response = self.client.post(
                "/voice/tts",
                headers={
                    "x-vs-actor-user-id": ACTOR,
                    "x-vs-voice-turn-id": VOICE_TURN,
                    "x-vs-voice-session-id": VOICE_SESSION,
                },
                json={
                    "text": "Bounded test phrase.",
                    "model": "gpt-4o-mini-tts",
                    "voice": "marin",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"fake-pcm")
        self.assertEqual(response.headers["content-type"], "audio/pcm")
        self.assertEqual(response.headers["x-vs-audio-format"], "pcm_s16le")
        self.assertEqual(response.headers["x-vs-audio-sample-rate"], "24000")
        self.assertEqual(response.headers["x-vs-voice-turn-id"], VOICE_TURN)
        self.assertEqual(
            response.headers["x-vs-provider-request-id"],
            "openai-request-tts-001",
        )
        self.assertEqual(len(FakeAsyncClient.calls), 1)
        self.assertEqual(
            FakeAsyncClient.calls[0]["json"]["model"],
            "gpt-4o-mini-tts",
        )
        self.assertEqual(FakeAsyncClient.calls[0]["json"]["speed"], 1.0)
        self.assertEqual(FakeAsyncClient.calls[0]["json"]["response_format"], "pcm")
        self.assertEqual(FakeAsyncClient.calls[0]["json"]["stream_format"], "audio")
        self.assertEqual(FakeAsyncClient.calls[0]["send"], {"stream": True})
        self.assertTrue(FakeAsyncClient.instances[0].response.closed)
        self.assertTrue(FakeAsyncClient.instances[0].closed)

    def test_rejects_invalid_voice_turn_before_openai(self) -> None:
        response = self.client.post(
            "/voice/tts",
            headers={
                "x-vs-actor-user-id": ACTOR,
                "x-vs-voice-turn-id": "not-a-uuid",
            },
            json={"text": "Bounded test phrase."},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "invalid_voice_turn_id")
        self.assertEqual(FakeAsyncClient.calls, [])

    def test_capabilities_expose_authoritative_tts_voice_catalog(self) -> None:
        response = self.client.get(
            "/voice/capabilities",
            headers={"x-vs-actor-user-id": ACTOR},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()["tts"]
        self.assertEqual(payload["default_model"], "gpt-4o-mini-tts")
        self.assertEqual(len(payload["models"]), 1)
        self.assertEqual(
            payload["models"][0]["voices"],
            [
                "alloy",
                "ash",
                "ballad",
                "coral",
                "echo",
                "fable",
                "nova",
                "onyx",
                "sage",
                "shimmer",
                "verse",
                "marin",
                "cedar",
            ],
        )
        self.assertEqual(
            payload["models"][0]["recommended_voices"],
            ["marin", "cedar"],
        )

    def test_model_and_speed_are_server_owned(self) -> None:
        response = self.client.post(
            "/voice/tts",
            headers={"x-vs-actor-user-id": ACTOR},
            json={
                "text": "Bounded test phrase.",
                "voice": "cedar",
                "model": "tts-1-hd",
                "speed": 0.5,
                "dry_run": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["model"], "gpt-4o-mini-tts")
        self.assertEqual(response.json()["voice"], "cedar")
        self.assertEqual(response.json()["speed"], 1.0)

    def test_accepts_supported_non_recommended_voice(self) -> None:
        response = self.client.post(
            "/voice/tts",
            headers={"x-vs-actor-user-id": ACTOR},
            json={
                "text": "Bounded test phrase.",
                "voice": "alloy",
                "dry_run": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["voice"], "alloy")

    def test_rejects_unknown_voice(self) -> None:
        response = self.client.post(
            "/voice/tts",
            headers={"x-vs-actor-user-id": ACTOR},
            json={
                "text": "Bounded test phrase.",
                "voice": "unknown",
                "dry_run": True,
            },
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"]["error"],
            "unsupported_tts_voice_for_model",
        )


if __name__ == "__main__":
    unittest.main()
