from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections import deque
from contextlib import suppress
from typing import Any, Callable

import httpx
from websockets.asyncio.client import connect

from rag_engine.openai_chat_provider_v1 import safety_identifier_v1
from rag_engine.voice_language_v1 import VOICE_LANGUAGE_HEADER
from rag_engine.voice_realtime_session_manager import RealtimePreviewSession


OPENAI_REALTIME_SIDEBAND_URL = (
    "wss://api.openai.com/v1/realtime?call_id={call_id}"
)
BRAINS_INTERNAL_BASE_URL = "http://127.0.0.1:8088"
MAX_SIDEBAND_MESSAGE_BYTES = 256 * 1024
MAX_TRANSCRIPT_CHARS = 32_768
MAX_PENDING_TURNS = 8
_ITEM_ID_RE = re.compile(r"^item_[A-Za-z0-9_-]{1,160}$")


class RealtimePreviewSidebandError(RuntimeError):
    pass


class RealtimePreviewSidebandNotReady(RealtimePreviewSidebandError):
    pass


class RealtimePreviewSidebandController:
    """
    Server-owned Realtime transcription sideband.

    OpenAI is permitted to produce transcript events only. Every completed
    transcript is sent through the existing local /log and /response/query
    contracts before an answer can be returned to the browser.
    """

    def __init__(
        self,
        *,
        session: RealtimePreviewSession,
        api_key: str,
        service_token: str,
        internal_base_url: str = BRAINS_INTERNAL_BASE_URL,
        connect_factory: Callable[..., Any] = connect,
        http_client_factory: Callable[..., Any] = httpx.AsyncClient,
    ) -> None:
        if not api_key.strip():
            raise ValueError("api_key is required")
        if not service_token.strip():
            raise ValueError("service_token is required")
        self.session = session
        self._api_key = api_key.strip()
        self._service_token = service_token.strip()
        self._internal_base_url = internal_base_url.rstrip("/")
        self._connect_factory = connect_factory
        self._http_client_factory = http_client_factory
        self._connected = asyncio.Event()
        self._closing = False
        self._websocket: Any | None = None
        self._send_lock = asyncio.Lock()
        self._pending_turns: asyncio.Queue[tuple[int, str, str, bool]] = (
            asyncio.Queue(maxsize=MAX_PENDING_TURNS)
        )
        self._pending_commit_authorizations: deque[bool] = deque()
        self._web_search_authorized_by_item: dict[str, bool] = {}
        self._committed_items: list[str] = []
        self._sequence_by_item: dict[str, int] = {}
        self._completed_by_item: dict[str, str] = {}
        self._next_completed_index = 0
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self.task is not None:
            raise RuntimeError("sideband controller already started")
        self.task = asyncio.create_task(
            self._run(),
            name=f"realtime-preview:{self.session.preview_session_id}",
        )

    async def commit(self, *, web_search_authorized: bool = False) -> None:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=5.0)
        except asyncio.TimeoutError as exc:
            raise RealtimePreviewSidebandNotReady(
                "sideband connection is not ready"
            ) from exc
        websocket = self._websocket
        if websocket is None or self._closing:
            raise RealtimePreviewSidebandNotReady(
                "sideband connection is unavailable"
            )
        async with self._send_lock:
            self._pending_commit_authorizations.append(
                bool(web_search_authorized)
            )
            try:
                await websocket.send(
                    json.dumps(
                        {"type": "input_audio_buffer.commit"},
                        separators=(",", ":"),
                    )
                )
            except Exception:
                self._pending_commit_authorizations.pop()
                raise
        self.session.append_event("commit.accepted")

    async def close(self) -> None:
        self._closing = True
        websocket = self._websocket
        if websocket is not None:
            with suppress(Exception):
                await websocket.close(code=1000)
        task = self.task
        if (
            task is not None
            and task is not asyncio.current_task()
            and not task.done()
        ):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        worker: asyncio.Task[None] | None = None
        sideband_url = OPENAI_REALTIME_SIDEBAND_URL.format(
            call_id=self.session.openai_call_id
        )
        try:
            async with self._connect_factory(
                sideband_url,
                additional_headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "OpenAI-Safety-Identifier": safety_identifier_v1(
                        self.session.owner_user_id
                    ),
                },
                compression=None,
                open_timeout=10,
                close_timeout=5,
                ping_interval=20,
                ping_timeout=20,
                max_size=MAX_SIDEBAND_MESSAGE_BYTES,
                max_queue=32,
            ) as websocket:
                self._websocket = websocket
                self._connected.set()
                self.session.append_event("session.connected")
                worker = asyncio.create_task(
                    self._turn_worker(),
                    name=(
                        "realtime-preview-turns:"
                        f"{self.session.preview_session_id}"
                    ),
                )
                async for raw_message in websocket:
                    await self._handle_message(raw_message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closing:
                self.session.append_event(
                    "session.failed",
                    {"error": self._public_error(exc)},
                )
        finally:
            self._websocket = None
            self._connected.clear()
            if worker is not None:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
            if not self._closing:
                self.session.append_event("session.closed")

    async def _handle_message(self, raw_message: Any) -> None:
        if isinstance(raw_message, bytes):
            if len(raw_message) > MAX_SIDEBAND_MESSAGE_BYTES:
                raise RealtimePreviewSidebandError(
                    "sideband message exceeded limit"
                )
            try:
                raw_message = raw_message.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RealtimePreviewSidebandError(
                    "sideband message was not UTF-8"
                ) from exc
        if not isinstance(raw_message, str):
            raise RealtimePreviewSidebandError(
                "sideband message type was invalid"
            )
        if len(raw_message.encode("utf-8")) > MAX_SIDEBAND_MESSAGE_BYTES:
            raise RealtimePreviewSidebandError(
                "sideband message exceeded limit"
            )
        try:
            event = json.loads(raw_message)
        except json.JSONDecodeError as exc:
            raise RealtimePreviewSidebandError(
                "sideband message was not JSON"
            ) from exc
        if not isinstance(event, dict):
            raise RealtimePreviewSidebandError(
                "sideband event was not an object"
            )

        event_type = event.get("type")
        if isinstance(event_type, str) and event_type.startswith("response."):
            raise RealtimePreviewSidebandError(
                "transcription session emitted a forbidden response event"
            )
        if event_type == "input_audio_buffer.committed":
            self._handle_committed(event)
            await self._queue_ordered_completions()
        elif (
            event_type
            == "conversation.item.input_audio_transcription.completed"
        ):
            self._handle_transcript_completed(event)
            await self._queue_ordered_completions()
        elif event_type == "error":
            error = event.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            self.session.append_event(
                "provider.error",
                {"error": self._safe_provider_code(code)},
            )

    def _handle_committed(self, event: dict[str, Any]) -> None:
        item_id = self._item_id(event.get("item_id"))
        if item_id in self._sequence_by_item:
            return
        sequence = len(self._committed_items) + 1
        self._committed_items.append(item_id)
        self._sequence_by_item[item_id] = sequence
        self._web_search_authorized_by_item[item_id] = (
            self._pending_commit_authorizations.popleft()
            if self._pending_commit_authorizations
            else False
        )

    def _handle_transcript_completed(self, event: dict[str, Any]) -> None:
        item_id = self._item_id(event.get("item_id"))
        transcript = event.get("transcript")
        if not isinstance(transcript, str):
            raise RealtimePreviewSidebandError(
                "completed transcript was invalid"
            )
        transcript = transcript.strip()
        if len(transcript) > MAX_TRANSCRIPT_CHARS:
            raise RealtimePreviewSidebandError(
                "completed transcript exceeded limit"
            )
        self._completed_by_item.setdefault(item_id, transcript)

    async def _queue_ordered_completions(self) -> None:
        while self._next_completed_index < len(self._committed_items):
            item_id = self._committed_items[self._next_completed_index]
            if item_id not in self._completed_by_item:
                return
            transcript = self._completed_by_item.pop(item_id)
            sequence = self._sequence_by_item[item_id]
            web_search_authorized = self._web_search_authorized_by_item.pop(
                item_id,
                False,
            )
            try:
                self._pending_turns.put_nowait(
                    (
                        sequence,
                        item_id,
                        transcript,
                        web_search_authorized,
                    )
                )
            except asyncio.QueueFull as exc:
                raise RealtimePreviewSidebandError(
                    "too many pending governed turns"
                ) from exc
            self._next_completed_index += 1

    async def _turn_worker(self) -> None:
        async with self._http_client_factory(
            timeout=httpx.Timeout(100.0, connect=5.0),
        ) as client:
            while True:
                (
                    sequence,
                    item_id,
                    transcript,
                    web_search_authorized,
                ) = await self._pending_turns.get()
                try:
                    if not transcript:
                        self.session.append_event(
                            "transcript.empty",
                            {
                                "item_id": item_id,
                                "sequence": sequence,
                            },
                        )
                        continue
                    await self._process_governed_turn(
                        client=client,
                        sequence=sequence,
                        item_id=item_id,
                        transcript=transcript,
                        web_search_authorized=web_search_authorized,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.session.append_event(
                        "turn.failed",
                        {
                            "item_id": item_id,
                            "sequence": sequence,
                            "error": self._public_error(exc),
                        },
                    )
                finally:
                    self._pending_turns.task_done()

    async def _process_governed_turn(
        self,
        *,
        client: Any,
        sequence: int,
        item_id: str,
        transcript: str,
        web_search_authorized: bool,
    ) -> None:
        request_id = str(uuid.uuid4())
        voice_turn_id = str(uuid.uuid4())
        headers = {
            "content-type": "application/json",
            "x-request-id": request_id,
            "x-vs-service-token": self._service_token,
            "x-vs-actor-user-id": self.session.owner_user_id,
            "x-vs-owner-user-id": self.session.owner_user_id,
            "x-vs-voice-session-id": str(self.session.voice_session_id),
            "x-vs-voice-turn-id": voice_turn_id,
            VOICE_LANGUAGE_HEADER: self.session.language,
        }

        self.session.append_event(
            "transcript.completed",
            {
                "item_id": item_id,
                "sequence": sequence,
                "transcript": transcript,
                "voice_turn_id": voice_turn_id,
                "language": self.session.language,
            },
        )

        if web_search_authorized:
            search_headers = {
                **headers,
                "x-vs-web-search-authorization": (
                    "supabase_fresh_voice_lease_v1"
                ),
            }
            search_response = await client.post(
                f"{self._internal_base_url}/search/execute",
                headers=search_headers,
                json={
                    "user_id": self.session.owner_user_id,
                    "thread_id": str(self.session.thread_id),
                    "query": transcript,
                    "channel": "voice",
                    "response_language": self.session.language,
                },
            )
            if search_response.status_code != 200:
                raise RealtimePreviewSidebandError(
                    "governed search execution failed"
                )
            search_payload = search_response.json()
            if not isinstance(search_payload, dict):
                raise RealtimePreviewSidebandError(
                    "governed search payload was invalid"
                )
            if search_payload.get("executed") is True:
                answer = search_payload.get("answer")
                answer_id = search_payload.get("answer_id")
                if not isinstance(answer, str) or not answer.strip():
                    raise RealtimePreviewSidebandError(
                        "governed search answer was empty"
                    )
                try:
                    parsed_answer_id = str(uuid.UUID(str(answer_id)))
                except (TypeError, ValueError) as exc:
                    raise RealtimePreviewSidebandError(
                        "governed search answer binding was invalid"
                    ) from exc
                self.session.append_event(
                    "response.completed",
                    {
                        "item_id": item_id,
                        "sequence": sequence,
                        "transcript": transcript,
                        "answer": answer,
                        "answer_id": parsed_answer_id,
                        "voice_turn_id": voice_turn_id,
                        "request_id": request_id,
                        "timings": {},
                        "web_search": True,
                        "language": self.session.language,
                        "search_route": (
                            search_payload.get("plan") or {}
                        ).get("selected_route"),
                    },
                )
                return

        log_response = await client.post(
            f"{self._internal_base_url}/log",
            headers=headers,
            json={
                "user_id": self.session.owner_user_id,
                "text": transcript,
                "source": "voice/realtime-preview:user",
                "tags": ["user", "chat", "voice", "realtime_preview"],
                "thread_id": str(self.session.thread_id),
            },
        )
        if log_response.status_code != 200:
            raise RealtimePreviewSidebandError(
                "governed transcript persistence failed"
            )
        log_payload = log_response.json()
        if (
            not isinstance(log_payload, dict)
            or log_payload.get("status") != "ok"
            or log_payload.get("request_id") != request_id
        ):
            raise RealtimePreviewSidebandError(
                "governed transcript persistence was not confirmed"
            )

        response = await client.post(
            f"{self._internal_base_url}/response/query",
            headers={
                **headers,
                **(
                    {
                        "x-vs-web-search-authorization": (
                            "supabase_fresh_voice_lease_v1"
                        )
                    }
                    if web_search_authorized
                    else {}
                ),
            },
            json={
                "user_id": self.session.owner_user_id,
                "message": transcript,
                "thread_id": str(self.session.thread_id),
                "no_store": False,
                "include_inspection": False,
            },
        )
        if response.status_code != 200:
            raise RealtimePreviewSidebandError(
                "governed response generation failed"
            )
        if response.headers.get("x-vs-voice-turn-id") != voice_turn_id:
            raise RealtimePreviewSidebandError(
                "governed voice turn binding failed"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise RealtimePreviewSidebandError(
                "governed response payload was invalid"
            )
        answer = payload.get("answer")
        answer_id = payload.get("answer_id")
        if not isinstance(answer, str) or not answer.strip():
            raise RealtimePreviewSidebandError(
                "governed response answer was empty"
            )
        try:
            parsed_answer_id = str(uuid.UUID(str(answer_id)))
        except (TypeError, ValueError) as exc:
            raise RealtimePreviewSidebandError(
                "governed response answer binding was invalid"
            ) from exc

        self.session.append_event(
            "response.completed",
            {
                "item_id": item_id,
                "sequence": sequence,
                "transcript": transcript,
                "answer": answer,
                "answer_id": parsed_answer_id,
                "voice_turn_id": voice_turn_id,
                "request_id": request_id,
                "timings": self._safe_timings(payload.get("timings")),
                "language": self.session.language,
            },
        )

    @staticmethod
    def _item_id(value: Any) -> str:
        item_id = str(value or "")
        if not _ITEM_ID_RE.fullmatch(item_id):
            raise RealtimePreviewSidebandError(
                "sideband item ID was invalid"
            )
        return item_id

    @staticmethod
    def _safe_provider_code(value: Any) -> str:
        code = str(value or "provider_error")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code):
            return "provider_error"
        return code

    @staticmethod
    def _safe_timings(value: Any) -> dict[str, int | float]:
        if not isinstance(value, dict):
            return {}
        return {
            str(key): timing
            for key, timing in value.items()
            if isinstance(key, str)
            and len(key) <= 80
            and isinstance(timing, (int, float))
            and not isinstance(timing, bool)
            and timing >= 0
        }

    @staticmethod
    def _public_error(exc: Exception) -> str:
        if isinstance(exc, RealtimePreviewSidebandNotReady):
            return "sideband_not_ready"
        if isinstance(exc, RealtimePreviewSidebandError):
            return "realtime_preview_protocol_error"
        if isinstance(exc, httpx.TimeoutException):
            return "governed_response_timeout"
        if isinstance(exc, httpx.HTTPError):
            return "governed_response_unavailable"
        return "realtime_preview_unavailable"
