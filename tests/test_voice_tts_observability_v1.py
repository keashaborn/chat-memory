from __future__ import annotations

import os
import unittest
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from rag_engine import voice_tts_router as tts


ACTOR = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
VOICE_TURN = "0fc3d70a-a6d0-4e55-9e39-20e060b416c8"


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


if __name__ == "__main__":
    unittest.main()
