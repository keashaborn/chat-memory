from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


MAX_SESSION_EVENTS = 128


class RealtimePreviewController(Protocol):
    def start(self) -> None: ...

    async def commit(self) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class RealtimePreviewEvent:
    cursor: int
    event_type: str
    payload: dict[str, Any]

    def public_dict(self) -> dict[str, Any]:
        return {
            "cursor": self.cursor,
            "type": self.event_type,
            **self.payload,
        }


@dataclass
class RealtimePreviewSession:
    preview_session_id: uuid.UUID
    owner_user_id: str
    voice_session_id: uuid.UUID
    thread_id: uuid.UUID
    openai_call_id: str
    language: str
    created_at_monotonic: float
    expires_at_monotonic: float
    controller: RealtimePreviewController | None = field(
        default=None,
        repr=False,
    )
    _events: deque[RealtimePreviewEvent] = field(
        default_factory=lambda: deque(maxlen=MAX_SESSION_EVENTS),
        init=False,
        repr=False,
    )
    _next_cursor: int = field(default=1, init=False, repr=False)
    _event_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )

    def append_event(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> RealtimePreviewEvent:
        with self._event_lock:
            event = RealtimePreviewEvent(
                cursor=self._next_cursor,
                event_type=event_type,
                payload=dict(payload or {}),
            )
            self._next_cursor += 1
            self._events.append(event)
            return event

    def events_after(
        self,
        cursor: int,
        *,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        safe_limit = min(max(int(limit), 1), 100)
        with self._event_lock:
            events = [
                event.public_dict()
                for event in self._events
                if event.cursor > cursor
            ][:safe_limit]
            next_cursor = events[-1]["cursor"] if events else cursor
            return events, int(next_cursor)

    def cancel_controller(self) -> None:
        controller = self.controller
        if controller is None:
            return
        task = getattr(controller, "task", None)
        if not isinstance(task, asyncio.Task) or task.done():
            return
        loop = task.get_loop()
        if loop.is_running():
            loop.call_soon_threadsafe(task.cancel)


class RealtimePreviewSessionRegistry:
    """
    Process-local preview registry.

    The current Brains service has one worker. A restart deliberately drops all
    preview sessions so the experimental path fails closed. Move this state to
    a shared TTL store before enabling the mode on multiple workers.

    The TTL is an inactivity timeout. Successful owner- and voice-lease-bound
    access renews it; missing or mismatched access never extends a session.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = 15 * 60,
        maximum_sessions: int = 64,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if maximum_sessions <= 0:
            raise ValueError("maximum_sessions must be positive")
        self._ttl_seconds = ttl_seconds
        self._maximum_sessions = maximum_sessions
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[uuid.UUID, RealtimePreviewSession] = {}

    def _prune_locked(self, now: float) -> None:
        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if session.expires_at_monotonic <= now
        ]
        for session_id in expired:
            session = self._sessions.pop(session_id, None)
            if session is not None:
                session.cancel_controller()

    def register(
        self,
        *,
        owner_user_id: str,
        voice_session_id: uuid.UUID,
        thread_id: uuid.UUID,
        openai_call_id: str,
        language: str = "en",
    ) -> RealtimePreviewSession:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            if len(self._sessions) >= self._maximum_sessions:
                oldest = min(
                    self._sessions.values(),
                    key=lambda item: item.created_at_monotonic,
                )
                removed = self._sessions.pop(
                    oldest.preview_session_id,
                    None,
                )
                if removed is not None:
                    removed.cancel_controller()

            session = RealtimePreviewSession(
                preview_session_id=uuid.uuid4(),
                owner_user_id=owner_user_id,
                voice_session_id=voice_session_id,
                thread_id=thread_id,
                openai_call_id=openai_call_id,
                language=language,
                created_at_monotonic=now,
                expires_at_monotonic=now + self._ttl_seconds,
            )
            self._sessions[session.preview_session_id] = session
            return session

    def get_owned(
        self,
        *,
        preview_session_id: uuid.UUID,
        owner_user_id: str,
        voice_session_id: uuid.UUID,
    ) -> RealtimePreviewSession | None:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            session = self._sessions.get(preview_session_id)
            if (
                session is None
                or session.owner_user_id != owner_user_id
                or session.voice_session_id != voice_session_id
            ):
                return None
            session.expires_at_monotonic = now + self._ttl_seconds
            return session

    def pop_owned(
        self,
        *,
        preview_session_id: uuid.UUID,
        owner_user_id: str,
        voice_session_id: uuid.UUID,
    ) -> RealtimePreviewSession | None:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            session = self._sessions.get(preview_session_id)
            if (
                session is None
                or session.owner_user_id != owner_user_id
                or session.voice_session_id != voice_session_id
            ):
                return None
            return self._sessions.pop(preview_session_id)

    def remove_owned(
        self,
        *,
        preview_session_id: uuid.UUID,
        owner_user_id: str,
        voice_session_id: uuid.UUID,
    ) -> bool:
        session = self.pop_owned(
            preview_session_id=preview_session_id,
            owner_user_id=owner_user_id,
            voice_session_id=voice_session_id,
        )
        if session is None:
            return False
        session.cancel_controller()
        return True

    def size(self) -> int:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            return len(self._sessions)
