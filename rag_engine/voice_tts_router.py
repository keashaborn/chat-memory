from __future__ import annotations

import hashlib
import os
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from rag_engine.voice_observability_v1 import (
    voice_turn_id_from_request,
    voice_turn_response_headers,
)
from rag_engine.voice_session_router import require_active_voice_session

router = APIRouter()

OPENAI_TTS_URL = os.getenv("OPENAI_TTS_URL") or "https://api.openai.com/v1/audio/speech"

DEFAULT_TTS_MODEL = "gpt-4o-mini-tts"
DEFAULT_TTS_VOICE = os.getenv("OPENAI_TTS_VOICE") or "marin"
DEFAULT_TTS_SPEED = 1.0
MAX_TTS_CHARS = 4096
VOICE_CAPABILITIES_VERSION = "2026-07-28.1"

TTS_MODEL_CAPABILITIES: dict[str, dict[str, Any]] = {
    "gpt-4o-mini-tts": {
        "label": "GPT-4o mini TTS",
        "description": "Recommended: expressive, reliable speech with style instructions.",
        "legacy": False,
        "supports_instructions": True,
        "default_voice": "marin",
        "recommended_voices": ["marin", "cedar"],
        "voices": [
            "alloy",
            "ash",
            "ballad",
            "coral",
            "echo",
            "fable",
            "nova",
            "onyx",
            "sage",
            "shimmer",
            "verse",
            "marin",
            "cedar",
        ],
    },
}

NO_STORE_HEADERS = {
    "cache-control": "private, no-store, max-age=0, must-revalidate",
    "pragma": "no-cache",
    "expires": "0",
}


def _require_actor(req: Request) -> str:
    raw = (req.headers.get("x-vs-actor-user-id") or "").strip()
    if not raw:
        raise HTTPException(status_code=401, detail="missing_actor_user_id")

    try:
        return str(uuid.UUID(raw))
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_actor_user_id")


def _clean_voice(raw: Any, model: str) -> str:
    model_capabilities = TTS_MODEL_CAPABILITIES[model]
    allowed = model_capabilities["voices"]
    configured_default = DEFAULT_TTS_VOICE.strip().lower()
    fallback = configured_default if configured_default in allowed else model_capabilities["default_voice"]

    if raw is None or not str(raw).strip():
        return fallback

    value = str(raw).strip().lower()
    if value not in allowed:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "unsupported_tts_voice_for_model",
                "field": "voice",
                "value": value[:120],
                "model": model,
                "allowed": allowed,
            },
        )

    return value


@router.post("/voice/tts")
async def create_tts(req: Request):
    actor_user_id = _require_actor(req)
    voice_turn_id = voice_turn_id_from_request(req)
    if voice_turn_id is not None:
        await require_active_voice_session(req, actor_user_id)

    try:
        body = await req.json()
    except Exception:
        body = {}

    if not isinstance(body, dict):
        body = {}

    text = str(body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="missing_text")

    if len(text) > MAX_TTS_CHARS:
        raise HTTPException(status_code=413, detail=f"text_too_large_max_{MAX_TTS_CHARS}")

    model = DEFAULT_TTS_MODEL
    voice = _clean_voice(body.get("voice"), model)
    speed = DEFAULT_TTS_SPEED
    instructions = str(body.get("instructions") or "").strip()

    if body.get("dry_run") is True:
        return JSONResponse(
            {
                "status": "ok",
                "provider": "openai",
                "model": model,
                "voice": voice,
                "speed": speed,
                "chars": len(text),
                "audio_format": "pcm_s16le",
                "audio_sample_rate": 24000,
                "dry_run": True,
            },
            headers=NO_STORE_HEADERS,
        )

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="missing_openai_key")

    payload: dict[str, Any] = {
        "model": model,
        "voice": voice,
        "input": text,
        "response_format": "pcm",
        "stream_format": "audio",
        "speed": speed,
    }

    if instructions and model == "gpt-4o-mini-tts":
        payload["instructions"] = instructions[:2000]

    client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
    try:
        request = client.build_request(
            "POST",
            OPENAI_TTS_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "OpenAI-Safety-Identifier": _safety_identifier(actor_user_id),
            },
            json=payload,
        )
        upstream = await client.send(request, stream=True)
    except httpx.TimeoutException as exc:
        await client.aclose()
        raise HTTPException(
            status_code=504, detail={"error": "openai_tts_timeout"}
        ) from exc
    except Exception as exc:
        await client.aclose()
        raise HTTPException(
            status_code=502, detail={"error": "openai_tts_unreachable"}
        ) from exc

    if upstream.status_code >= 400:
        provider_request_id = upstream.headers.get("x-request-id") or upstream.headers.get("openai-request-id")
        public_status = 429 if upstream.status_code == 429 else 503 if upstream.status_code >= 500 else 502
        await upstream.aread()
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(
            status_code=public_status,
            detail={
                "error": "openai_tts_error",
                "upstream_status": upstream.status_code,
                "provider_request_id": provider_request_id,
            },
        )

    provider_request_id = (
        upstream.headers.get("x-request-id")
        or upstream.headers.get("openai-request-id")
    )

    async def stream_audio():
        try:
            async for chunk in upstream.aiter_raw():
                if chunk:
                    yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        stream_audio(),
        media_type="audio/pcm",
        headers={
            **NO_STORE_HEADERS,
            "x-vs-voice-provider": "openai",
            "x-vs-voice-model": model,
            "x-vs-audio-format": "pcm_s16le",
            "x-vs-audio-sample-rate": "24000",
            **(
                {"x-vs-provider-request-id": provider_request_id}
                if provider_request_id
                else {}
            ),
            **voice_turn_response_headers(voice_turn_id),
        },
    )


def _safety_identifier(actor_user_id: str) -> str:
    return hashlib.sha256(actor_user_id.encode("utf-8")).hexdigest()


@router.get("/voice/capabilities")
async def get_voice_capabilities(req: Request):
    _require_actor(req)

    models = []
    for model_id in (DEFAULT_TTS_MODEL,):
        capabilities = TTS_MODEL_CAPABILITIES[model_id]
        models.append({"id": model_id, **capabilities})

    default_model = DEFAULT_TTS_MODEL
    default_voice = _clean_voice(None, default_model)

    return JSONResponse(
        {
            "version": VOICE_CAPABILITIES_VERSION,
            "tts": {
                "default_model": default_model,
                "default_voice": default_voice,
                "maximum_input_characters": MAX_TTS_CHARS,
                "models": models,
            },
        },
        headers=NO_STORE_HEADERS,
    )
