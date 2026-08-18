from __future__ import annotations

import math
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from seebx.core.ownership import require_actor_matches_owner
from seebx.adapters.openai_transcription import (
    OpenAITranscriptionConfigurationError,
    OpenAITranscriptionInvalidResponseError,
    OpenAITranscriptionTimeoutError,
    OpenAITranscriptionUnavailableError,
    transcribe_audio_with_openai,
)
from seebx.core.voice_observability import (
    voice_turn_id_from_request,
    voice_turn_response_headers,
)
from seebx.core.voice_identity import require_active_voice_session
from seebx.contracts.voice_language import (
    voice_language_from_request,
)


router = APIRouter()

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
    language = voice_language_from_request(req)
    voice_turn_id = voice_turn_id_from_request(req)
    await require_active_voice_session(req, owner_user_id)

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

    try:
        upstream = await transcribe_audio_with_openai(
            owner_user_id=owner_user_id,
            filename=filename,
            raw_audio=raw,
            content_type=content_type,
            language=language,
        )
    except OpenAITranscriptionConfigurationError:
        raise HTTPException(status_code=500, detail="missing_openai_key")
    except OpenAITranscriptionTimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail={"error": "openai_transcription_timeout"},
        ) from exc
    except OpenAITranscriptionUnavailableError as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": "openai_transcription_unreachable"},
        ) from exc
    except OpenAITranscriptionInvalidResponseError as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": "invalid_openai_transcription_response"},
        ) from exc

    if upstream.status_code >= 400:
        raise HTTPException(
            status_code=_public_upstream_status(upstream.status_code),
            detail={
                "error": "openai_transcription_error",
                "upstream_status": upstream.status_code,
                "provider_request_id": upstream.provider_request_id,
            },
        )

    payload = upstream.payload
    if payload is None:
        raise HTTPException(
            status_code=502,
            detail={"error": "invalid_openai_transcription_response"},
        )

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
            "model": upstream.model,
            "language": language,
            "confidence": confidence,
            "provider_request_id": upstream.provider_request_id,
        },
        headers={
            "cache-control": "no-store",
            **voice_turn_response_headers(voice_turn_id),
        },
    )
