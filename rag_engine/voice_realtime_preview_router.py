from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from rag_engine.lifeswitch_auth import require_actor_matches_owner
from rag_engine.openai_chat_provider_v1 import safety_identifier_v1
from rag_engine.voice_realtime_session_manager import (
    RealtimePreviewSessionRegistry,
)
from rag_engine.voice_session_router import require_active_voice_session


router = APIRouter()
preview_sessions = RealtimePreviewSessionRegistry()

OPENAI_REALTIME_CALLS_URL = (
    os.getenv("OPENAI_REALTIME_CALLS_URL")
    or "https://api.openai.com/v1/realtime/calls"
).strip()
REALTIME_TRANSCRIPTION_MODEL = "gpt-realtime-whisper"
REALTIME_TRANSCRIPTION_LANGUAGE = "en"
REALTIME_TRANSCRIPTION_DELAY = "low"
MAX_SDP_BYTES = 128 * 1024
MAX_SDP_RESPONSE_BYTES = 256 * 1024
_CALL_ID_RE = re.compile(r"^rtc_[A-Za-z0-9_-]{1,120}$")
NO_STORE_HEADERS = {
    "cache-control": "private, no-store, max-age=0, must-revalidate",
    "pragma": "no-cache",
    "expires": "0",
    "x-content-type-options": "nosniff",
}


def _normalized_content_type(raw: Any) -> str:
    return str(raw or "").split(";", 1)[0].strip().lower()


def _owner_from_request(req: Request) -> str:
    owner_user_id = (req.headers.get("x-vs-owner-user-id") or "").strip()
    if not owner_user_id:
        raise HTTPException(status_code=400, detail="missing_owner_user_id")
    return require_actor_matches_owner(req, owner_user_id)


def _voice_session_id_from_request(req: Request) -> uuid.UUID:
    raw = (req.headers.get("x-vs-voice-session-id") or "").strip()
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_voice_session_id"},
        ) from None


def _public_upstream_status(status_code: int) -> int:
    if status_code == 429:
        return 429
    if status_code >= 500:
        return 503
    return 502


def _call_id_from_location(raw: str | None) -> str:
    location = str(raw or "").strip()
    call_id = location.rstrip("/").rsplit("/", 1)[-1]
    if not _CALL_ID_RE.fullmatch(call_id):
        raise HTTPException(
            status_code=502,
            detail={"error": "invalid_openai_realtime_call_id"},
        )
    return call_id


def _transcription_session_config() -> dict[str, Any]:
    return {
        "type": "transcription",
        "audio": {
            "input": {
                "transcription": {
                    "model": REALTIME_TRANSCRIPTION_MODEL,
                    "language": REALTIME_TRANSCRIPTION_LANGUAGE,
                    "delay": REALTIME_TRANSCRIPTION_DELAY,
                },
                "turn_detection": None,
            },
        },
    }


@router.post("/voice/realtime-preview/call")
async def create_realtime_preview_call(req: Request):
    owner_user_id = _owner_from_request(req)
    voice_session_id = await require_active_voice_session(req, owner_user_id)

    if _normalized_content_type(req.headers.get("content-type")) != "application/sdp":
        raise HTTPException(
            status_code=415,
            detail={"error": "unsupported_content_type"},
        )

    raw_sdp = await req.body()
    if not raw_sdp:
        raise HTTPException(status_code=400, detail={"error": "missing_sdp"})
    if len(raw_sdp) > MAX_SDP_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "error": "sdp_too_large",
                "maximum_bytes": MAX_SDP_BYTES,
            },
        )
    try:
        offer_sdp = raw_sdp.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_sdp_encoding"},
        ) from exc
    if not offer_sdp.strip().startswith("v=0"):
        raise HTTPException(status_code=400, detail={"error": "invalid_sdp"})

    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise HTTPException(status_code=500, detail="missing_openai_key")

    session_config = _transcription_session_config()
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=10.0),
        ) as client:
            upstream = await client.post(
                OPENAI_REALTIME_CALLS_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "OpenAI-Safety-Identifier": safety_identifier_v1(
                        owner_user_id
                    ),
                },
                files={
                    "sdp": (None, offer_sdp, "application/sdp"),
                    "session": (
                        None,
                        json.dumps(session_config, separators=(",", ":")),
                        "application/json",
                    ),
                },
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=504,
            detail={"error": "openai_realtime_timeout"},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": "openai_realtime_unreachable"},
        ) from exc

    provider_request_id = (
        upstream.headers.get("x-request-id")
        or upstream.headers.get("openai-request-id")
    )
    if upstream.status_code >= 400:
        raise HTTPException(
            status_code=_public_upstream_status(upstream.status_code),
            detail={
                "error": "openai_realtime_error",
                "upstream_status": upstream.status_code,
                "provider_request_id": provider_request_id,
            },
        )

    answer_sdp = upstream.text
    if not answer_sdp.strip().startswith("v=0"):
        raise HTTPException(
            status_code=502,
            detail={"error": "invalid_openai_realtime_sdp"},
        )
    if len(answer_sdp.encode("utf-8")) > MAX_SDP_RESPONSE_BYTES:
        raise HTTPException(
            status_code=502,
            detail={"error": "openai_realtime_sdp_too_large"},
        )

    call_id = _call_id_from_location(upstream.headers.get("location"))
    session = preview_sessions.register(
        owner_user_id=owner_user_id,
        voice_session_id=voice_session_id,
        openai_call_id=call_id,
    )

    return Response(
        content=answer_sdp,
        media_type="application/sdp",
        headers={
            **NO_STORE_HEADERS,
            "x-vs-realtime-preview-session-id": str(
                session.preview_session_id
            ),
            "x-vs-realtime-preview-mode": "transcription-only",
        },
    )


@router.delete("/voice/realtime-preview/session/{preview_session_id}")
async def close_realtime_preview_session(
    preview_session_id: uuid.UUID,
    req: Request,
):
    owner_user_id = _owner_from_request(req)
    voice_session_id = _voice_session_id_from_request(req)
    removed = preview_sessions.remove_owned(
        preview_session_id=preview_session_id,
        owner_user_id=owner_user_id,
        voice_session_id=voice_session_id,
    )
    return JSONResponse(
        {
            "ok": True,
            "preview_session_id": str(preview_session_id),
            "removed": removed,
        },
        headers=NO_STORE_HEADERS,
    )
