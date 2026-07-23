from __future__ import annotations

import hashlib
import json
import os
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from rag_engine.lifeswitch_auth import require_actor_matches_owner


router = APIRouter()

OPENAI_REALTIME_CALLS_URL = (
    os.getenv("OPENAI_REALTIME_CALLS_URL")
    or "https://api.openai.com/v1/realtime/calls"
).strip()
REALTIME_TRANSCRIPTION_MODEL = "gpt-realtime-whisper"
REALTIME_TRANSCRIPTION_DELAY = "low"
NO_STORE_HEADERS = {
    "cache-control": "private, no-store, max-age=0, must-revalidate",
    "pragma": "no-cache",
    "expires": "0",
}


def _transcription_session_config() -> dict[str, Any]:
    """Server-owned Realtime policy that can transcribe but cannot answer."""

    return {
        "type": "transcription",
        "audio": {
            "input": {
                "transcription": {
                    "model": REALTIME_TRANSCRIPTION_MODEL,
                    "delay": REALTIME_TRANSCRIPTION_DELAY,
                },
                "turn_detection": None,
            }
        },
    }


def _safety_identifier(actor_user_id: str) -> str:
    return hashlib.sha256(actor_user_id.encode("utf-8")).hexdigest()


def get_realtime_capabilities() -> dict[str, Any]:
    return {
        "conversation_generation_enabled": False,
        "retired": True,
        "transcription_only": True,
        "models": [],
    }


def _raise_upstream_error(response: httpx.Response) -> None:
    provider_request_id = (
        response.headers.get("x-request-id")
        or response.headers.get("openai-request-id")
    )
    public_status = (
        429
        if response.status_code == 429
        else 503
        if response.status_code >= 500
        else 502
    )
    raise HTTPException(
        status_code=public_status,
        detail={
            "error": "openai_realtime_transcription_error",
            "upstream_status": response.status_code,
            "provider_request_id": provider_request_id,
        },
    )


async def _openai_transcription_webrtc_answer(
    *,
    actor_user_id: str,
    sdp: str,
) -> Response:
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise HTTPException(status_code=500, detail="missing_openai_key")
    if not sdp.strip():
        raise HTTPException(status_code=400, detail="missing_sdp")

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0)
        ) as client:
            upstream = await client.post(
                OPENAI_REALTIME_CALLS_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "OpenAI-Safety-Identifier": _safety_identifier(
                        actor_user_id
                    ),
                },
                files={
                    "sdp": (None, sdp, "application/sdp"),
                    "session": (
                        None,
                        json.dumps(_transcription_session_config()),
                        "application/json",
                    ),
                },
            )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": "openai_realtime_transcription_unreachable"},
        ) from exc

    if upstream.status_code >= 400:
        _raise_upstream_error(upstream)

    return Response(
        content=upstream.text,
        status_code=200,
        media_type="application/sdp",
        headers={
            **NO_STORE_HEADERS,
            "x-vs-voice-provider": "openai",
            "x-vs-realtime-mode": "webrtc_transcription_only",
        },
    )


@router.post("/voice/openai/transcription-webrtc-offer")
async def create_transcription_webrtc_offer(req: Request):
    """Open a transcription-only WebRTC session for governed voice turns."""

    owner_user_id = (req.headers.get("x-vs-owner-user-id") or "").strip()
    if not owner_user_id:
        raise HTTPException(status_code=400, detail="missing_owner_user_id")
    actor_user_id = require_actor_matches_owner(req, owner_user_id)

    raw = await req.body()
    sdp = raw.decode("utf-8", "ignore")
    if req.query_params.get("dry_run") == "1":
        return JSONResponse(
            {
                "status": "ok",
                "provider": "openai",
                "mode": "webrtc_transcription_only",
                "session": _transcription_session_config(),
                "sdp_chars": len(sdp),
                "dry_run": True,
            },
            headers=NO_STORE_HEADERS,
        )

    return await _openai_transcription_webrtc_answer(
        actor_user_id=actor_user_id,
        sdp=sdp,
    )
