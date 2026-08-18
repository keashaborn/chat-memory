from __future__ import annotations

import json
import os
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from seebx.adapters import openai_realtime
from seebx.capabilities.voice import realtime_preview as preview
from seebx.capabilities.voice.realtime_session import (
    RealtimePreviewSessionRegistry,
)


ACTOR = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OTHER_ACTOR = "557ea042-cb82-48f8-9429-472e96c957ef"
VOICE_SESSION = "a872d3f2-2d5c-4ae3-9f02-d9f43a38899e"
THREAD_ID = "a401fdc5-92ee-4eeb-ad64-a98603c7dc69"
OFFER_SDP = "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
ANSWER_SDP = "v=0\r\no=- 2 3 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 201,
        text: str = ANSWER_SDP,
        location: str = "/v1/realtime/calls/rtc_test_123",
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = {
            "location": location,
            "x-request-id": "openai-request-realtime-001",
        }


class FakeAsyncClient:
    response = FakeResponse()
    calls: list[dict[str, Any]] = []
    post_error: Exception | None = None

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        if self.__class__.post_error is not None:
            raise self.__class__.post_error
        self.__class__.calls.append({"url": url, **kwargs})
        return self.__class__.response


class FakeSidebandController:
    instances: list["FakeSidebandController"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.started = False
        self.commits = 0
        self.web_search_authorizations: list[bool] = []
        self.closed = False
        self.__class__.instances.append(self)

    def start(self) -> None:
        self.started = True

    async def commit(self, *, web_search_authorized: bool = False) -> None:
        self.commits += 1
        self.web_search_authorizations.append(web_search_authorized)

    async def close(self) -> None:
        self.closed = True


class VoiceRealtimePreviewRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeAsyncClient.calls = []
        FakeAsyncClient.response = FakeResponse()
        FakeAsyncClient.post_error = None
        FakeSidebandController.instances = []
        app = FastAPI()
        app.include_router(preview.router)
        self.client = TestClient(app)
        self.active_lease = patch.object(
            preview,
            "require_active_voice_session",
            AsyncMock(return_value=uuid.UUID(VOICE_SESSION)),
        )
        self.active_lease.start()
        self.addCleanup(self.active_lease.stop)
        self.registry = RealtimePreviewSessionRegistry()
        self.registry_patch = patch.object(
            preview,
            "preview_sessions",
            self.registry,
        )
        self.registry_patch.start()
        self.addCleanup(self.registry_patch.stop)
        self.controller_patch = patch.object(
            preview,
            "sideband_controller_factory",
            FakeSidebandController,
        )
        self.controller_patch.start()
        self.addCleanup(self.controller_patch.stop)

    def _headers(
        self,
        *,
        owner: str = ACTOR,
        content_type: str = "application/sdp",
    ) -> dict[str, str]:
        return {
            "x-vs-actor-user-id": ACTOR,
            "x-vs-owner-user-id": owner,
            "x-vs-voice-session-id": VOICE_SESSION,
            "x-vs-thread-id": THREAD_ID,
            "content-type": content_type,
        }

    def test_requires_owner_and_actor_equality(self) -> None:
        missing = self.client.post(
            "/voice/realtime-preview/call",
            headers={
                "x-vs-actor-user-id": ACTOR,
                "content-type": "application/sdp",
            },
            content=OFFER_SDP,
        )
        self.assertEqual(missing.status_code, 400)

        mismatch = self.client.post(
            "/voice/realtime-preview/call",
            headers=self._headers(owner=OTHER_ACTOR),
            content=OFFER_SDP,
        )
        self.assertEqual(mismatch.status_code, 403)
        self.assertEqual(FakeAsyncClient.calls, [])

    def test_rejects_non_sdp_before_openai(self) -> None:
        response = self.client.post(
            "/voice/realtime-preview/call",
            headers=self._headers(content_type="application/json"),
            content=OFFER_SDP,
        )
        self.assertEqual(response.status_code, 415)
        self.assertEqual(FakeAsyncClient.calls, [])

    def test_rejects_invalid_sdp_before_openai(self) -> None:
        response = self.client.post(
            "/voice/realtime-preview/call",
            headers=self._headers(),
            content="not-sdp",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(FakeAsyncClient.calls, [])

    def test_creates_transcription_only_session_with_server_key(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "test-only-key",
                    "VS_SERVICE_TOKEN": "test-service-token",
                },
            ),
            patch.object(
                openai_realtime.httpx,
                "AsyncClient",
                FakeAsyncClient,
            ),
        ):
            response = self.client.post(
                "/voice/realtime-preview/call",
                headers=self._headers(),
                content=OFFER_SDP,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, ANSWER_SDP)
        self.assertEqual(response.headers["content-type"], "application/sdp")
        self.assertEqual(response.headers["cache-control"], preview.NO_STORE_HEADERS["cache-control"])
        self.assertEqual(
            response.headers["x-vs-realtime-preview-mode"],
            "transcription-only",
        )
        self.assertNotIn("rtc_test_123", response.headers.values())
        self.assertNotIn("test-only-key", response.text)
        self.assertEqual(self.registry.size(), 1)
        self.assertEqual(len(FakeSidebandController.instances), 1)
        controller = FakeSidebandController.instances[0]
        self.assertTrue(controller.started)
        self.assertEqual(
            controller.kwargs["session"].thread_id,
            uuid.UUID(THREAD_ID),
        )
        self.assertEqual(
            controller.kwargs["service_token"],
            "test-service-token",
        )

        call = FakeAsyncClient.calls[0]
        self.assertEqual(
            call["url"],
            "https://api.openai.com/v1/realtime/calls",
        )
        self.assertEqual(
            call["headers"]["Authorization"],
            "Bearer test-only-key",
        )
        self.assertTrue(
            call["headers"]["OpenAI-Safety-Identifier"].startswith("vs1_")
        )
        self.assertNotIn(ACTOR, call["headers"]["OpenAI-Safety-Identifier"])
        self.assertEqual(call["files"]["sdp"][1], OFFER_SDP)

        session = json.loads(call["files"]["session"][1])
        self.assertEqual(session["type"], "transcription")
        self.assertEqual(
            session["audio"]["input"]["transcription"]["model"],
            "gpt-realtime-whisper",
        )
        self.assertEqual(
            session["audio"]["input"]["turn_detection"],
            None,
        )
        self.assertNotIn("output", session["audio"])
        self.assertNotIn("instructions", session)
        self.assertNotIn("tools", session)

    def test_close_requires_matching_owner_and_voice_session(self) -> None:
        registered = self.registry.register(
            owner_user_id=ACTOR,
            voice_session_id=uuid.UUID(VOICE_SESSION),
            thread_id=uuid.UUID(THREAD_ID),
            openai_call_id="rtc_test_close",
        )
        mismatch = self.client.delete(
            f"/voice/realtime-preview/session/{registered.preview_session_id}",
            headers=self._headers(owner=OTHER_ACTOR),
        )
        self.assertEqual(mismatch.status_code, 403)
        self.assertEqual(self.registry.size(), 1)

        closed = self.client.delete(
            f"/voice/realtime-preview/session/{registered.preview_session_id}",
            headers=self._headers(),
        )
        self.assertEqual(closed.status_code, 200)
        self.assertTrue(closed.json()["removed"])
        self.assertEqual(self.registry.size(), 0)

    def test_commit_and_events_require_owned_active_session(self) -> None:
        registered = self.registry.register(
            owner_user_id=ACTOR,
            voice_session_id=uuid.UUID(VOICE_SESSION),
            thread_id=uuid.UUID(THREAD_ID),
            openai_call_id="rtc_test_events",
        )
        controller = FakeSidebandController(
            session=registered,
            api_key="not-returned",
            service_token="not-returned",
        )
        registered.controller = controller
        registered.append_event(
            "transcript.completed",
            {"transcript": "governed transcript"},
        )

        committed = self.client.post(
            (
                "/voice/realtime-preview/session/"
                f"{registered.preview_session_id}/commit"
            ),
            headers=self._headers(),
        )
        self.assertEqual(committed.status_code, 200)
        self.assertEqual(controller.commits, 1)
        self.assertEqual(controller.web_search_authorizations, [False])

        events = self.client.get(
            (
                "/voice/realtime-preview/session/"
                f"{registered.preview_session_id}/events?after=0"
            ),
            headers=self._headers(),
        )
        self.assertEqual(events.status_code, 200)
        payload = events.json()
        self.assertEqual(payload["next_cursor"], 1)
        self.assertEqual(
            payload["events"][0]["transcript"],
            "governed transcript",
        )

        missing = self.client.get(
            f"/voice/realtime-preview/session/{uuid.uuid4()}/events",
            headers=self._headers(),
        )
        self.assertEqual(missing.status_code, 404)

    def test_openai_error_is_sanitized(self) -> None:
        FakeAsyncClient.response = FakeResponse(
            status_code=401,
            text='{"error":{"message":"secret provider detail"}}',
        )
        with (
            patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "test-only-key",
                    "VS_SERVICE_TOKEN": "test-service-token",
                },
            ),
            patch.object(
                openai_realtime.httpx,
                "AsyncClient",
                FakeAsyncClient,
            ),
        ):
            response = self.client.post(
                "/voice/realtime-preview/call",
                headers=self._headers(),
                content=OFFER_SDP,
            )

        self.assertEqual(response.status_code, 502)
        self.assertNotIn("secret provider detail", response.text)
        self.assertEqual(
            response.json()["detail"]["error"],
            "openai_realtime_error",
        )

    def test_canonical_capability_has_no_legacy_wrapper(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        self.assertTrue(
            (
                repository
                / "seebx/capabilities/voice/realtime_preview.py"
            ).is_file()
        )
        self.assertFalse(
            (
                repository
                / "rag_engine/voice_realtime_preview_router.py"
            ).exists()
        )

    def test_provider_timeout_is_sanitized(self) -> None:
        FakeAsyncClient.post_error = openai_realtime.httpx.ReadTimeout(
            "private provider timeout"
        )
        with (
            patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "test-only-key",
                    "VS_SERVICE_TOKEN": "test-service-token",
                },
            ),
            patch.object(
                openai_realtime.httpx,
                "AsyncClient",
                FakeAsyncClient,
            ),
        ):
            response = self.client.post(
                "/voice/realtime-preview/call",
                headers=self._headers(),
                content=OFFER_SDP,
            )

        self.assertEqual(response.status_code, 504)
        self.assertEqual(
            response.json()["detail"],
            {"error": "openai_realtime_timeout"},
        )
        self.assertNotIn("private provider timeout", response.text)

    def test_provider_unavailable_is_sanitized(self) -> None:
        FakeAsyncClient.post_error = RuntimeError("private network detail")
        with (
            patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "test-only-key",
                    "VS_SERVICE_TOKEN": "test-service-token",
                },
            ),
            patch.object(
                openai_realtime.httpx,
                "AsyncClient",
                FakeAsyncClient,
            ),
        ):
            response = self.client.post(
                "/voice/realtime-preview/call",
                headers=self._headers(),
                content=OFFER_SDP,
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["detail"],
            {"error": "openai_realtime_unreachable"},
        )
        self.assertNotIn("private network detail", response.text)
    def test_missing_provider_key_preserves_public_error(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "",
                "VS_SERVICE_TOKEN": "test-service-token",
            },
        ):
            response = self.client.post(
                "/voice/realtime-preview/call",
                headers=self._headers(),
                content=OFFER_SDP,
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], "missing_openai_key")
        self.assertEqual(FakeAsyncClient.calls, [])

    def test_missing_service_token_preserves_public_error(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "test-only-key",
                "VS_SERVICE_TOKEN": "",
            },
        ):
            response = self.client.post(
                "/voice/realtime-preview/call",
                headers=self._headers(),
                content=OFFER_SDP,
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "missing_service_token")
        self.assertEqual(FakeAsyncClient.calls, [])

    def test_invalid_provider_answer_is_sanitized(self) -> None:
        FakeAsyncClient.response = FakeResponse(
            text="private invalid provider answer",
        )
        with (
            patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "test-only-key",
                    "VS_SERVICE_TOKEN": "test-service-token",
                },
            ),
            patch.object(
                openai_realtime.httpx,
                "AsyncClient",
                FakeAsyncClient,
            ),
        ):
            response = self.client.post(
                "/voice/realtime-preview/call",
                headers=self._headers(),
                content=OFFER_SDP,
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["detail"],
            {"error": "invalid_openai_realtime_sdp"},
        )
        self.assertNotIn("private invalid provider answer", response.text)

    def test_invalid_provider_call_id_is_sanitized(self) -> None:
        FakeAsyncClient.response = FakeResponse(
            location="/v1/realtime/calls/private-invalid-call-id",
        )
        with (
            patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "test-only-key",
                    "VS_SERVICE_TOKEN": "test-service-token",
                },
            ),
            patch.object(
                openai_realtime.httpx,
                "AsyncClient",
                FakeAsyncClient,
            ),
        ):
            response = self.client.post(
                "/voice/realtime-preview/call",
                headers=self._headers(),
                content=OFFER_SDP,
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["detail"],
            {"error": "invalid_openai_realtime_call_id"},
        )
        self.assertNotIn("private-invalid-call-id", response.text)


if __name__ == "__main__":
    unittest.main()
