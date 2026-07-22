from __future__ import annotations

"""Content-free correlation metadata for governed voice turns."""

from uuid import UUID

from fastapi import HTTPException, Request


VOICE_TURN_HEADER = "x-vs-voice-turn-id"


def voice_turn_id_from_request(req: Request) -> UUID | None:
    raw = (req.headers.get(VOICE_TURN_HEADER) or "").strip()
    if not raw:
        return None
    try:
        return UUID(raw)
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="invalid_voice_turn_id") from None


def voice_turn_response_headers(voice_turn_id: UUID | None) -> dict[str, str]:
    return {VOICE_TURN_HEADER: str(voice_turn_id)} if voice_turn_id else {}


__all__ = [
    "VOICE_TURN_HEADER",
    "voice_turn_id_from_request",
    "voice_turn_response_headers",
]
