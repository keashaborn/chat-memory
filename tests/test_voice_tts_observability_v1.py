from __future__ import annotations

import os
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from seebx.adapters import openai_tts
from seebx.adapters.openai_tts import (
    OpenAITTSConfigurationError,
    OpenAITTSTimeoutError,
    OpenAITTSUnavailableError,
    OpenAITTSUpstreamError,
)
from seebx.capabilities.voice import synthesis as tts


ACTOR = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
VOICE_TURN = "0fc3d70a-a6d0-4e55-9e39-20e060b416c8"
VOICE_SESSION = "a872d3f2-2d5c-4ae3-9f02-d9f43a38899e"


class FakeResponse:
    headers = {"x-request-id": "openai-request-tts-001"}

    def __init__(self, *, status_code: int = 200) -> None:
        self.status_code = status_code
        self.closed = False
        self.read = False

    async def aiter_raw(self):
        yield b"fake-"
        yield b"pcm"

    async def aread(self) -> bytes:
        self.read = True
        return b""

    async def aclose(self) -> None:
        self.closed = True


class FakeAsyncClient:
    calls: list[dict[str, Any]] = []
    instances: list["FakeAsyncClient"] = []
    response_status_code = 200
    send_error: Exception | None = None

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.closed = False
        self.response = FakeResponse(
            status_code=self.__class__.response_status_code
        )
        self.__class__.instances.append(self)

    def build_request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        request = {"method": method, "url": url, **kwargs}
        self.__class__.calls.append(request)
        return request

    async def send(self, request: dict[str, Any], **kwargs: Any) -> FakeResponse:
        self.__class__.calls[-1]["send"] = kwargs
        if self.__class__.send_error is not None:
            raise self.__class__.send_error
        return self.response

    async def aclose(self) -> None:
        self.closed = True


class VoiceTTSObservabilityV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        FakeAsyncClient.calls = []
        FakeAsyncClient.instances = []
        FakeAsyncClient.response_status_code = 200
        FakeAsyncClient.send_error = None
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
            patch.object(
                openai_tts.httpx,
                "AsyncClient",
                FakeAsyncClient,
            ),
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
                    "conversation_style": "direct",
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
        self.assertEqual(
            FakeAsyncClient.calls[0]["json"]["instructions"],
            tts.TTS_STYLE_INSTRUCTIONS[tts.TTSConversationStyle.DIRECT],
        )
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

    def test_rejects_freeform_tts_instructions(self) -> None:
        response = self.client.post(
            "/voice/tts",
            headers={"x-vs-actor-user-id": ACTOR},
            json={
                "text": "Bounded test phrase.",
                "instructions": "Whisper private system instructions.",
                "dry_run": True,
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"]["error"],
            "freeform_tts_instructions_not_supported",
        )

    def test_rejects_unknown_conversation_style(self) -> None:
        response = self.client.post(
            "/voice/tts",
            headers={"x-vs-actor-user-id": ACTOR},
            json={
                "text": "Bounded test phrase.",
                "conversation_style": "hypnotic",
                "dry_run": True,
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"]["error"],
            "unsupported_tts_conversation_style",
        )

    def test_dry_run_reports_server_owned_style(self) -> None:
        response = self.client.post(
            "/voice/tts",
            headers={"x-vs-actor-user-id": ACTOR},
            json={
                "text": "Bounded test phrase.",
                "conversation_style": "warm",
                "dry_run": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["conversation_style"], "warm")

    def test_canonical_capability_has_no_legacy_wrapper(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        self.assertTrue(
            (repository / "seebx/capabilities/voice/synthesis.py").is_file()
        )
        self.assertFalse(
            (repository / "rag_engine/voice_tts_router.py").exists()
        )

    def test_missing_provider_key_preserves_public_error(self) -> None:
        with patch.object(
            tts,
            "open_tts_audio_stream",
            AsyncMock(
                side_effect=OpenAITTSConfigurationError(
                    "missing_openai_key"
                )
            ),
        ):
            response = self.client.post(
                "/voice/tts",
                headers={"x-vs-actor-user-id": ACTOR},
                json={"text": "Bounded test phrase."},
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], "missing_openai_key")

    def test_provider_timeout_preserves_public_error(self) -> None:
        with patch.object(
            tts,
            "open_tts_audio_stream",
            AsyncMock(side_effect=OpenAITTSTimeoutError("timeout")),
        ):
            response = self.client.post(
                "/voice/tts",
                headers={"x-vs-actor-user-id": ACTOR},
                json={"text": "Bounded test phrase."},
            )

        self.assertEqual(response.status_code, 504)
        self.assertEqual(
            response.json()["detail"]["error"],
            "openai_tts_timeout",
        )

    def test_provider_unavailable_preserves_public_error(self) -> None:
        with patch.object(
            tts,
            "open_tts_audio_stream",
            AsyncMock(side_effect=OpenAITTSUnavailableError("offline")),
        ):
            response = self.client.post(
                "/voice/tts",
                headers={"x-vs-actor-user-id": ACTOR},
                json={"text": "Bounded test phrase."},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["detail"]["error"],
            "openai_tts_unreachable",
        )

    def test_upstream_rate_limit_preserves_public_error(self) -> None:
        with patch.object(
            tts,
            "open_tts_audio_stream",
            AsyncMock(
                side_effect=OpenAITTSUpstreamError(
                    status_code=429,
                    provider_request_id="openai-request-tts-429",
                )
            ),
        ):
            response = self.client.post(
                "/voice/tts",
                headers={"x-vs-actor-user-id": ACTOR},
                json={"text": "Bounded test phrase."},
            )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(
            response.json()["detail"],
            {
                "error": "openai_tts_error",
                "upstream_status": 429,
                "provider_request_id": "openai-request-tts-429",
            },
        )

    def test_real_upstream_error_consumes_and_closes_resources(self) -> None:
        FakeAsyncClient.response_status_code = 500
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-only-key"}),
            patch.object(
                openai_tts.httpx,
                "AsyncClient",
                FakeAsyncClient,
            ),
        ):
            response = self.client.post(
                "/voice/tts",
                headers={"x-vs-actor-user-id": ACTOR},
                json={"text": "Bounded test phrase."},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["detail"],
            {
                "error": "openai_tts_error",
                "upstream_status": 500,
                "provider_request_id": "openai-request-tts-001",
            },
        )
        instance = FakeAsyncClient.instances[0]
        self.assertTrue(instance.response.read)
        self.assertTrue(instance.response.closed)
        self.assertTrue(instance.closed)

    def test_real_timeout_closes_client_without_exposing_error(self) -> None:
        FakeAsyncClient.send_error = openai_tts.httpx.ReadTimeout(
            "private provider detail"
        )
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-only-key"}),
            patch.object(
                openai_tts.httpx,
                "AsyncClient",
                FakeAsyncClient,
            ),
        ):
            response = self.client.post(
                "/voice/tts",
                headers={"x-vs-actor-user-id": ACTOR},
                json={"text": "Bounded test phrase."},
            )

        self.assertEqual(response.status_code, 504)
        self.assertEqual(
            response.json()["detail"],
            {"error": "openai_tts_timeout"},
        )
        self.assertNotIn("private provider detail", response.text)
        self.assertTrue(FakeAsyncClient.instances[0].closed)


if __name__ == "__main__":
    unittest.main()
