from __future__ import annotations

"""Request authority for an active owner-scoped voice session."""

from uuid import UUID

from fastapi import HTTPException, Request

from seebx.adapters import voice_session as store
from seebx.core.ownership import require_actor_matches_owner


VOICE_SESSION_HEADER = "x-vs-voice-session-id"


def voice_session_id_from_request(request: Request) -> UUID:
    raw = (request.headers.get(VOICE_SESSION_HEADER) or "").strip()
    if not raw:
        raise HTTPException(
            status_code=409,
            detail={"error": "voice_session_required"},
        )
    try:
        return UUID(raw)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_voice_session_id"},
        ) from None


def voice_session_store_http_exception(
    exc: store.VoiceSessionStoreUnconfigured
    | store.VoiceSessionStoreUnavailable,
) -> HTTPException:
    if isinstance(exc, store.VoiceSessionStoreUnconfigured):
        return HTTPException(status_code=503, detail="voice_session_unconfigured")
    if isinstance(exc, store.VoiceSessionStoreUnavailable):
        return HTTPException(
            status_code=503,
            detail={"error": "voice_session_store_unavailable"},
        )
    raise AssertionError("unsupported voice session store error")


async def require_active_voice_session(
    request: Request,
    owner_user_id: str,
) -> UUID:
    owner = require_actor_matches_owner(request, owner_user_id)
    session_id = voice_session_id_from_request(request)
    try:
        active = await store.is_active(owner, session_id)
    except (
        store.VoiceSessionStoreUnconfigured,
        store.VoiceSessionStoreUnavailable,
    ) as exc:
        raise voice_session_store_http_exception(exc) from exc
    if not active:
        raise HTTPException(
            status_code=409,
            detail={"error": "voice_session_not_active"},
        )
    return session_id


__all__ = [
    "VOICE_SESSION_HEADER",
    "require_active_voice_session",
    "voice_session_id_from_request",
    "voice_session_store_http_exception",
]
