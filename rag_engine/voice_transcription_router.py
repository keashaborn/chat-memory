from __future__ import annotations

import math
import os
import re
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from rag_engine.lifeswitch_auth import require_actor_matches_owner
from rag_engine.openai_chat_provider_v1 import safety_identifier_v1
from rag_engine.voice_observability_v1 import (
    voice_turn_id_from_request,
    voice_turn_response_headers,
)


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
_LANGUAGE_RE = re.compile(r"^[a-z]{2}$")
_configured_language = (
    os.getenv("OPENAI_TRANSCRIPTION_LANGUAGE") or "en"
).strip().lower()
DEFAULT_TRANSCRIPTION_LANGUAGE = (
    _configured_language if _LANGUAGE_RE.fullmatch(_configured_language) else "en"
)

MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_TRANSCRIPT_CHARACTERS = 32_000
TRANSCRIPTION_CONTEXT_PROMPT = (
    "Natural conversational English in LifeSwitch with the Verbal Sage assistant. "
    "Preserve short questions and incomplete phrases exactly; do not complete or "
    "reinterpret them. Proper names may include Fractal Monism v0.2, "
    "FM v0.2, Sage, RESSE, Governed Memory V1, Qdrant, OpenAI, and Supabase."
)
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


def _confidence_summary(payload: dict[str, Any]) -> dict[str, Any] | None:
    raw_logprobs = payload.get("logprobs")
    if not isinstance(raw_logprobs, list):
        return None

    logprobs: list[float] = []
    for item in raw_logprobs:
        if not isinstance(item, dict):
            continue
        try:
            value = float(item.get("logprob"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            logprobs.append(max(-100.0, min(0.0, value)))

    if not logprobs:
        return None

    return {
        "token_count": len(logprobs),
        "mean_logprob": round(sum(logprobs) / len(logprobs), 6),
        "minimum_logprob": round(min(logprobs), 6),
        "low_confidence_token_count": sum(value < -1.0 for value in logprobs),
    }


@router.post("/voice/openai/transcribe")
async def transcribe_voice_audio(req: Request):
    owner_user_id = _owner_from_request(req)
    voice_turn_id = voice_turn_id_from_request(req)

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
                    "language": DEFAULT_TRANSCRIPTION_LANGUAGE,
                    "temperature": "0",
                    "include[]": "logprobs",
                    "prompt": TRANSCRIPTION_CONTEXT_PROMPT,
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
    confidence = _confidence_summary(payload)

    return JSONResponse(
        {
            "transcript": transcript,
            "provider": "openai",
            "model": DEFAULT_TRANSCRIPTION_MODEL,
            "language": DEFAULT_TRANSCRIPTION_LANGUAGE,
            "confidence": confidence,
            "provider_request_id": provider_request_id,
        },
        headers={
            "cache-control": "no-store",
            **voice_turn_response_headers(voice_turn_id),
        },
    )
