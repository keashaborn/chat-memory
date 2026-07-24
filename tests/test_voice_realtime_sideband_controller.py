from __future__ import annotations

import asyncio
import json
import unittest
import uuid
from typing import Any

from rag_engine.voice_realtime_session_manager import (
    RealtimePreviewSessionRegistry,
)
from rag_engine.voice_realtime_sideband_controller import (
    RealtimePreviewSidebandController,
    RealtimePreviewSidebandError,
)


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
VOICE_SESSION = uuid.UUID("a872d3f2-2d5c-4ae3-9f02-d9f43a38899e")
THREAD_ID = uuid.UUID("a401fdc5-92ee-4eeb-ad64-a98603c7dc69")
ANSWER_ID = "f13705e4-3974-4834-b4a1-00c8e9cbef3b"


class FakeHTTPResponse:
    def __init__(
        self,
        *,
        status_code: int,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeHTTPClient:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> "FakeHTTPClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> FakeHTTPResponse:
        self.calls.append({"url": url, **kwargs})
        headers = kwargs["headers"]
        if url.endswith("/log"):
            return FakeHTTPResponse(
                status_code=200,
                payload={
                    "status": "ok",
                    "id": str(uuid.uuid4()),
                    "request_id": headers["x-request-id"],
                },
            )
        return FakeHTTPResponse(
            status_code=200,
            payload={
                "answer": f"Answer to {kwargs['json']['message']}",
                "answer_id": ANSWER_ID,
                "timings": {"backend_total_ms": 42},
            },
            headers={
                "x-vs-voice-turn-id": headers["x-vs-voice-turn-id"],
            },
        )


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)


class RealtimePreviewSidebandControllerTests(
    unittest.IsolatedAsyncioTestCase
):
    def setUp(self) -> None:
        self.registry = RealtimePreviewSessionRegistry()
        self.session = self.registry.register(
            owner_user_id=OWNER,
            voice_session_id=VOICE_SESSION,
            thread_id=THREAD_ID,
            openai_call_id="rtc_sideband_test",
        )
        self.http = FakeHTTPClient()
        self.controller = RealtimePreviewSidebandController(
            session=self.session,
            api_key="server-api-key",
            service_token="server-service-token",
            http_client_factory=lambda **kwargs: self.http,
        )

    async def test_out_of_order_completions_are_governed_in_commit_order(
        self,
    ) -> None:
        worker = asyncio.create_task(self.controller._turn_worker())
        try:
            await self.controller._handle_message(
                json.dumps(
                    {
                        "type": (
                            "conversation.item."
                            "input_audio_transcription.completed"
                        ),
                        "item_id": "item_second",
                        "transcript": "second turn",
                    }
                )
            )
            await self.controller._handle_message(
                json.dumps(
                    {
                        "type": (
                            "conversation.item."
                            "input_audio_transcription.completed"
                        ),
                        "item_id": "item_first",
                        "transcript": "first turn",
                    }
                )
            )
            await self.controller._handle_message(
                json.dumps(
                    {
                        "type": "input_audio_buffer.committed",
                        "item_id": "item_first",
                    }
                )
            )
            await self.controller._handle_message(
                json.dumps(
                    {
                        "type": "input_audio_buffer.committed",
                        "item_id": "item_second",
                    }
                )
            )
            await asyncio.wait_for(
                self.controller._pending_turns.join(),
                timeout=1.0,
            )
        finally:
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker

        query_calls = [
            call
            for call in self.http.calls
            if call["url"].endswith("/response/query")
        ]
        self.assertEqual(
            [call["json"]["message"] for call in query_calls],
            ["first turn", "second turn"],
        )
        self.assertTrue(
            all(
                call["json"]["thread_id"] == str(THREAD_ID)
                for call in query_calls
            )
        )
        self.assertTrue(
            all(
                call["headers"]["x-vs-service-token"]
                == "server-service-token"
                for call in self.http.calls
            )
        )
        response_events = [
            event
            for event in self.session.events_after(0)[0]
            if event["type"] == "response.completed"
        ]
        self.assertEqual(
            [event["sequence"] for event in response_events],
            [1, 2],
        )
        self.assertEqual(
            [event["transcript"] for event in response_events],
            ["first turn", "second turn"],
        )
        self.assertTrue(
            all(event["answer_id"] == ANSWER_ID for event in response_events)
        )

    async def test_commit_uses_server_sideband_only(self) -> None:
        websocket = FakeWebSocket()
        self.controller._websocket = websocket
        self.controller._connected.set()

        await self.controller.commit()

        self.assertEqual(
            json.loads(websocket.sent[0]),
            {"type": "input_audio_buffer.commit"},
        )
        events = self.session.events_after(0)[0]
        self.assertEqual(events[0]["type"], "commit.accepted")

    async def test_provider_error_does_not_expose_provider_message(
        self,
    ) -> None:
        await self.controller._handle_message(
            json.dumps(
                {
                    "type": "error",
                    "error": {
                        "code": "audio_buffer_too_small",
                        "message": "sensitive upstream detail",
                    },
                }
            )
        )
        events = self.session.events_after(0)[0]
        self.assertEqual(events[0]["type"], "provider.error")
        self.assertEqual(events[0]["error"], "audio_buffer_too_small")
        self.assertNotIn("sensitive", json.dumps(events))

    async def test_invalid_item_id_fails_closed(self) -> None:
        with self.assertRaises(RealtimePreviewSidebandError):
            await self.controller._handle_message(
                json.dumps(
                    {
                        "type": "input_audio_buffer.committed",
                        "item_id": "../../not-an-item",
                    }
                )
            )

    async def test_any_direct_realtime_response_fails_closed(self) -> None:
        with self.assertRaises(RealtimePreviewSidebandError):
            await self.controller._handle_message(
                json.dumps(
                    {
                        "type": "response.created",
                        "response": {"id": "resp_must_not_exist"},
                    }
                )
            )


if __name__ == "__main__":
    unittest.main()
