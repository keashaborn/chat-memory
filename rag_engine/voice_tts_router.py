from __future__ import annotations

import math
import os
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

router = APIRouter()

OPENAI_TTS_URL = os.getenv("OPENAI_TTS_URL") or "https://api.openai.com/v1/audio/speech"

DEFAULT_TTS_MODEL = os.getenv("OPENAI_TTS_MODEL") or "gpt-4o-mini-tts"
DEFAULT_TTS_VOICE = os.getenv("OPENAI_TTS_VOICE") or "sage"
MAX_TTS_CHARS = int(os.getenv("OPENAI_TTS_MAX_CHARS") or "8000")

ALLOWED_TTS_MODELS = {
    "gpt-4o-mini-tts",
    "tts-1",
    "tts-1-hd",
}

ALLOWED_TTS_VOICES = {
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
}


def _require_actor(req: Request) -> str:
    raw = (req.headers.get("x-vs-actor-user-id") or "").strip()
    if not raw:
        raise HTTPException(status_code=401, detail="missing_actor_user_id")

    try:
        return str(uuid.UUID(raw))
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_actor_user_id")


def _clean_model(raw: Any) -> str:
    fallback = DEFAULT_TTS_MODEL if DEFAULT_TTS_MODEL in ALLOWED_TTS_MODELS else "gpt-4o-mini-tts"
    value = str(raw or fallback).strip()

    # Do not allow stale provider-prefixed values like xai:...
    if not value or ":" in value:
        return fallback

    return value if value in ALLOWED_TTS_MODELS else fallback


def _clean_voice(raw: Any) -> str:
    fallback = DEFAULT_TTS_VOICE.lower() if DEFAULT_TTS_VOICE.lower() in ALLOWED_TTS_VOICES else "sage"
    value = str(raw or fallback).strip().lower()

    return value if value in ALLOWED_TTS_VOICES else fallback


def _clean_speed(raw: Any) -> float:
    try:
        value = float(raw)
    except Exception:
        value = 1.0

    if not math.isfinite(value):
        value = 1.0

    return max(0.25, min(4.0, value))


@router.post("/voice/tts")
async def create_tts(req: Request):
    actor_user_id = _require_actor(req)

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

    model = _clean_model(body.get("model"))
    voice = _clean_voice(body.get("voice"))
    speed = _clean_speed(body.get("speed", 1.0))
    instructions = str(body.get("instructions") or "").strip()

    if body.get("dry_run") is True:
        return JSONResponse(
            {
                "status": "ok",
                "provider": "openai",
                "actor_user_id": actor_user_id,
                "model": model,
                "voice": voice,
                "speed": speed,
                "chars": len(text),
                "dry_run": True,
            }
        )

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="missing_openai_key")

    payload: dict[str, Any] = {
        "model": model,
        "voice": voice,
        "input": text,
        "response_format": "mp3",
        "speed": speed,
    }

    if instructions and model == "gpt-4o-mini-tts":
        payload["instructions"] = instructions[:2000]

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
            upstream = await client.post(
                OPENAI_TTS_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"openai_tts_unreachable:{exc}") from exc

    if upstream.status_code >= 400:
        body_text = upstream.text[:2000]
        raise HTTPException(
            status_code=502,
            detail={
                "error": "openai_tts_error",
                "status": upstream.status_code,
                "body": body_text,
            },
        )

    return Response(
        content=upstream.content,
        media_type="audio/mpeg",
        headers={
            "x-vs-voice-provider": "openai",
            "x-vs-voice-model": model,
            "x-vs-voice-actor": actor_user_id,
        },
    )
