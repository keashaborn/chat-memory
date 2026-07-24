from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class RealtimePreviewSession:
    preview_session_id: uuid.UUID
    owner_user_id: str
    voice_session_id: uuid.UUID
    openai_call_id: str
    created_at_monotonic: float
    expires_at_monotonic: float


class RealtimePreviewSessionRegistry:
    """
    Process-local preview registry.

    The current Brains service has one worker. A restart deliberately drops all
    preview sessions so the experimental path fails closed. Move this state to
    a shared TTL store before enabling the mode on multiple workers.
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
            self._sessions.pop(session_id, None)

    def register(
        self,
        *,
        owner_user_id: str,
        voice_session_id: uuid.UUID,
        openai_call_id: str,
    ) -> RealtimePreviewSession:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            if len(self._sessions) >= self._maximum_sessions:
                oldest = min(
                    self._sessions.values(),
                    key=lambda item: item.created_at_monotonic,
                )
                self._sessions.pop(oldest.preview_session_id, None)

            session = RealtimePreviewSession(
                preview_session_id=uuid.uuid4(),
                owner_user_id=owner_user_id,
                voice_session_id=voice_session_id,
                openai_call_id=openai_call_id,
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
            return session

    def remove_owned(
        self,
        *,
        preview_session_id: uuid.UUID,
        owner_user_id: str,
        voice_session_id: uuid.UUID,
    ) -> bool:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            session = self._sessions.get(preview_session_id)
            if (
                session is None
                or session.owner_user_id != owner_user_id
                or session.voice_session_id != voice_session_id
            ):
                return False
            self._sessions.pop(preview_session_id, None)
            return True

    def size(self) -> int:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            return len(self._sessions)
