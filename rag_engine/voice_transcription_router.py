from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from rag_engine.lifeswitch_auth import require_actor_matches_owner
from rag_engine.openai_chat_provider_v1 import safety_identifier_v1


router = APIRouter()

OPENAI_TRANSCRIPTION_URL = (
    os.getenv("OPENAI_TRANSCRIPTION_URL")
    or "https://api.openai.com/v1/audio/transcriptions"
).strip()

ALLOWED_TRANSCRIPTION_MODELS = {
    "gpt-4o-transcribe",
    "gpt-4o-mini-transcribe",
}
_configured_model = (os.getenv("OPENAI_TRANSCRIPTION_MODEL") or "").strip()
DEFAULT_TRANSCRIPTION_MODEL = (
    _configured_model
    if _configured_model in ALLOWED_TRANSCRIPTION_MODELS
    else "gpt-4o-transcribe"
)

MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_TRANSCRIPT_CHARACTERS = 32_000
SUPPORTED_AUDIO_TYPES = {
    "audio/mp4": "voice.m4a",
    "audio/mpeg": "voice.mp3",
    "audio/ogg": "voice.ogg",
    "audio/wav": "voice.wav",
    "audio/wave": "voice.wav",
    "audio/webm": "voice.webm",
    "audio/x-m4a": "voice.m4a",
    "audio/x-wav": "voice.wav",
    "video/mp4": "voice.mp4",
}


def _normalized_audio_type(raw: Any) -> str:
    return str(raw or "").split(";", 1)[0].strip().lower()


def _owner_from_request(req: Request) -> str:
    owner_user_id = (req.headers.get("x-vs-owner-user-id") or "").strip()
    if not owner_user_id:
        raise HTTPException(status_code=400, detail="missing_owner_user_id")
    return require_actor_matches_owner(req, owner_user_id)


def _public_upstream_status(status_code: int) -> int:
    if status_code == 429:
        return 429
    if status_code >= 500:
        return 503
    return 502


@router.post("/voice/openai/transcribe")
async def transcribe_voice_audio(req: Request):
    owner_user_id = _owner_from_request(req)

    content_type = _normalized_audio_type(req.headers.get("content-type"))
    filename = SUPPORTED_AUDIO_TYPES.get(content_type)
    if not filename:
        raise HTTPException(
            status_code=415,
            detail={
                "error": "unsupported_audio_type",
                "allowed": sorted(SUPPORTED_AUDIO_TYPES),
            },
        )

    raw = await req.body()
    if not raw:
        raise HTTPException(status_code=400, detail="missing_audio")
    if len(raw) > MAX_AUDIO_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "error": "audio_too_large",
                "maximum_bytes": MAX_AUDIO_BYTES,
                "actual_bytes": len(raw),
            },
        )

    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise HTTPException(status_code=500, detail="missing_openai_key")

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0)
        ) as client:
            upstream = await client.post(
                OPENAI_TRANSCRIPTION_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "OpenAI-Safety-Identifier": safety_identifier_v1(owner_user_id),
                },
                files={"file": (filename, raw, content_type)},
                data={
                    "model": DEFAULT_TRANSCRIPTION_MODEL,
                    "response_format": "json",
                },
            )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": "openai_transcription_unreachable"},
        ) from exc

    provider_request_id = (
        upstream.headers.get("x-request-id")
        or upstream.headers.get("openai-request-id")
    )
    if upstream.status_code >= 400:
        raise HTTPException(
            status_code=_public_upstream_status(upstream.status_code),
            detail={
                "error": "openai_transcription_error",
                "upstream_status": upstream.status_code,
                "provider_request_id": provider_request_id,
            },
        )

    try:
        payload = upstream.json()
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": "invalid_openai_transcription_response"},
        ) from exc

    transcript = str(payload.get("text") or "").strip()
    if not transcript:
        raise HTTPException(status_code=422, detail="empty_transcript")
    if len(transcript) > MAX_TRANSCRIPT_CHARACTERS:
        raise HTTPException(status_code=502, detail="transcript_too_large")

    return JSONResponse(
        {
            "transcript": transcript,
            "provider": "openai",
            "model": DEFAULT_TRANSCRIPTION_MODEL,
            "provider_request_id": provider_request_id,
        },
        headers={"cache-control": "no-store"},
    )
