from __future__ import annotations

import uuid
from enum import Enum
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from seebx.adapters.openai_tts import (
    DEFAULT_TTS_VOICE,
    OpenAITTSConfigurationError,
    OpenAITTSTimeoutError,
    OpenAITTSUnavailableError,
    OpenAITTSUpstreamError,
    open_tts_audio_stream,
)
from seebx.core.voice_observability import (
    voice_turn_id_from_request,
    voice_turn_response_headers,
)
from seebx.contracts.voice_language import (
    DEFAULT_VOICE_LANGUAGE,
    VOICE_LANGUAGE_CONTRACT_VERSION,
    VOICE_LANGUAGES,
)
from seebx.core.voice_identity import require_active_voice_session

router = APIRouter()

DEFAULT_TTS_MODEL = "gpt-4o-mini-tts"
DEFAULT_TTS_SPEED = 1.0
MAX_TTS_CHARS = 4096
VOICE_CAPABILITIES_VERSION = "2026-07-30.1"


class TTSConversationStyle(str, Enum):
    DIRECT = "direct"
    NATURAL = "natural"
    WARM = "warm"


TTS_STYLE_INSTRUCTIONS: dict[TTSConversationStyle, str] = {
    TTSConversationStyle.DIRECT: (
        "Speak clearly and directly in a calm, matter-of-fact manner. "
        "Avoid theatrical emphasis."
    ),
    TTSConversationStyle.NATURAL: (
        "Speak naturally in a relaxed, conversational manner. "
        "Avoid sounding formal, clinical, or theatrical."
    ),
    TTSConversationStyle.WARM: (
        "Speak naturally with a calm, warm, friendly delivery. "
        "Do not sound flattering, overly enthusiastic, or theatrical."
    ),
}

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


def _clean_conversation_style(raw: Any) -> TTSConversationStyle:
    value = str(raw or TTSConversationStyle.NATURAL.value).strip().lower()
    try:
        return TTSConversationStyle(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "unsupported_tts_conversation_style",
                "field": "conversation_style",
                "allowed": [item.value for item in TTSConversationStyle],
            },
        ) from exc


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
    if str(body.get("instructions") or "").strip():
        raise HTTPException(
            status_code=422,
            detail={"error": "freeform_tts_instructions_not_supported"},
        )
    conversation_style = _clean_conversation_style(
        body.get("conversation_style")
    )
    instructions = TTS_STYLE_INSTRUCTIONS[conversation_style]

    if body.get("dry_run") is True:
        return JSONResponse(
            {
                "status": "ok",
                "provider": "openai",
                "model": model,
                "voice": voice,
                "speed": speed,
                "conversation_style": conversation_style.value,
                "chars": len(text),
                "audio_format": "pcm_s16le",
                "audio_sample_rate": 24000,
                "dry_run": True,
            },
            headers=NO_STORE_HEADERS,
        )

    try:
        stream = await open_tts_audio_stream(
            actor_user_id=actor_user_id,
            model=model,
            voice=voice,
            text=text,
            speed=speed,
            instructions=instructions,
        )
    except OpenAITTSConfigurationError:
        raise HTTPException(status_code=500, detail="missing_openai_key")
    except OpenAITTSTimeoutError as exc:
        raise HTTPException(
            status_code=504, detail={"error": "openai_tts_timeout"}
        ) from exc
    except OpenAITTSUnavailableError as exc:
        raise HTTPException(
            status_code=502, detail={"error": "openai_tts_unreachable"}
        ) from exc
    except OpenAITTSUpstreamError as exc:
        public_status = (
            429
            if exc.status_code == 429
            else 503
            if exc.status_code >= 500
            else 502
        )
        raise HTTPException(
            status_code=public_status,
            detail={
                "error": "openai_tts_error",
                "upstream_status": exc.status_code,
                "provider_request_id": exc.provider_request_id,
            },
        )

    return StreamingResponse(
        stream.iter_audio(),
        media_type="audio/pcm",
        headers={
            **NO_STORE_HEADERS,
            "x-vs-voice-provider": "openai",
            "x-vs-voice-model": stream.model,
            "x-vs-audio-format": "pcm_s16le",
            "x-vs-audio-sample-rate": "24000",
            **(
                {"x-vs-provider-request-id": stream.provider_request_id}
                if stream.provider_request_id
                else {}
            ),
            **voice_turn_response_headers(voice_turn_id),
        },
    )


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
            "language": {
                "contract_version": VOICE_LANGUAGE_CONTRACT_VERSION,
                "default": DEFAULT_VOICE_LANGUAGE,
                "auto_detect": True,
                "options": list(VOICE_LANGUAGES),
            },
        },
        headers=NO_STORE_HEADERS,
    )
