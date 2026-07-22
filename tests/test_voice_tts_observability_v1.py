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
    content = b"fake-mp3"


class FakeAsyncClient:
    calls: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.__class__.calls.append({"url": url, **kwargs})
        return FakeResponse()


class VoiceTTSObservabilityV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        FakeAsyncClient.calls = []
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
        self.assertEqual(response.content, b"fake-mp3")
        self.assertEqual(response.headers["x-vs-voice-turn-id"], VOICE_TURN)
        self.assertEqual(
            response.headers["x-vs-provider-request-id"],
            "openai-request-tts-001",
        )
        self.assertEqual(len(FakeAsyncClient.calls), 1)

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
