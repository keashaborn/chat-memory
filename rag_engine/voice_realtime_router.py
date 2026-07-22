from __future__ import annotations

import hashlib
import json
import os
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from rag_engine.lifeswitch_auth import require_actor_matches_owner

router = APIRouter()

# Direct speech-to-speech generation bypasses the governed /response/query
# contract. It is disabled unless an operator explicitly enables the legacy
# rollback path while the governed voice implementation is being deployed.
ALLOW_UNGOVERNED_REALTIME_VOICE = (
    os.getenv("ALLOW_UNGOVERNED_REALTIME_VOICE") or ""
).strip() == "1"

CURRENT_REALTIME_MODEL = "gpt-realtime-2.1"
ROLLBACK_REALTIME_MODEL = "gpt-realtime-2"

REALTIME_MODEL_CAPABILITIES: dict[str, dict[str, Any]] = {
    CURRENT_REALTIME_MODEL: {
        "label": "GPT-Realtime 2.1",
        "description": "Current managed model for live, low-latency voice conversations.",
        "rollback": False,
        "default_voice": "marin",
        "recommended_voices": ["marin", "cedar"],
        "voices": [
            "alloy",
            "ash",
            "ballad",
            "cedar",
            "coral",
            "echo",
            "marin",
            "sage",
            "shimmer",
            "verse",
        ],
    },
    ROLLBACK_REALTIME_MODEL: {
        "label": "GPT-Realtime 2",
        "description": "Rollback model retained for controlled operational recovery.",
        "rollback": True,
        "default_voice": "marin",
        "recommended_voices": ["marin", "cedar"],
        "voices": [
            "alloy",
            "ash",
            "ballad",
            "cedar",
            "coral",
            "echo",
            "marin",
            "sage",
            "shimmer",
            "verse",
        ],
    },
}

ALLOWED_REALTIME_MODELS = set(REALTIME_MODEL_CAPABILITIES)
_configured_realtime_model = (os.getenv("OPENAI_REALTIME_MODEL") or "").strip()
DEFAULT_REALTIME_MODEL = (
    _configured_realtime_model
    if _configured_realtime_model in ALLOWED_REALTIME_MODELS
    else CURRENT_REALTIME_MODEL
)
_configured_realtime_voice = (os.getenv("OPENAI_REALTIME_VOICE") or "").strip().lower()
_default_model_voice = REALTIME_MODEL_CAPABILITIES[DEFAULT_REALTIME_MODEL]["default_voice"]
DEFAULT_REALTIME_VOICE = (
    _configured_realtime_voice
    if _configured_realtime_voice in REALTIME_MODEL_CAPABILITIES[DEFAULT_REALTIME_MODEL]["voices"]
    else _default_model_voice
)

# Current browser-first WebRTC unified interface.
OPENAI_REALTIME_CALLS_URL = (
    os.getenv("OPENAI_REALTIME_CALLS_URL")
    or "https://api.openai.com/v1/realtime/calls"
).strip()

# Retained only for compatibility/probing until frontend is fully wired to WebRTC calls.
OPENAI_REALTIME_CLIENT_SECRETS_URL = (
    os.getenv("OPENAI_REALTIME_CLIENT_SECRETS_URL")
    or "https://api.openai.com/v1/realtime/client_secrets"
).strip()

REALTIME_TRANSCRIPTION_MODEL = "gpt-realtime-whisper"
REALTIME_TRANSCRIPTION_DELAY = "low"

def _require_actor(req: Request) -> str:
    actor = (req.headers.get("x-vs-actor-user-id") or "").strip()
    if not actor:
        raise HTTPException(status_code=401, detail="missing_actor_user_id")
    try:
        return str(uuid.UUID(actor))
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_actor_user_id")


def _require_ungoverned_realtime_enabled() -> None:
    if not ALLOW_UNGOVERNED_REALTIME_VOICE:
        raise HTTPException(
            status_code=410,
            detail="direct_realtime_generation_retired_use_governed_voice",
        )


def _clean_model(raw: Any) -> str:
    if raw is None or not str(raw).strip():
        return DEFAULT_REALTIME_MODEL

    model = str(raw).strip()
    if model not in ALLOWED_REALTIME_MODELS:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "unsupported_realtime_model",
                "field": "model",
                "value": model[:120],
                "allowed": sorted(ALLOWED_REALTIME_MODELS),
            },
        )
    return model


def _clean_voice(raw: Any, model: str) -> str:
    capabilities = REALTIME_MODEL_CAPABILITIES[model]
    allowed = capabilities["voices"]
    fallback = DEFAULT_REALTIME_VOICE if DEFAULT_REALTIME_VOICE in allowed else capabilities["default_voice"]

    if raw is None or not str(raw).strip():
        return fallback

    voice = str(raw).strip().lower()
    if voice not in allowed:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "unsupported_realtime_voice_for_model",
                "field": "voice",
                "value": voice[:120],
                "model": model,
                "allowed": allowed,
            },
        )
    return voice


def _clean_instructions(raw: Any) -> str:
    instructions = str(raw or "").strip()
    if not instructions:
        instructions = (
            "You are Sage, a concise, useful voice assistant inside Verbal Sage. "
            "Use normal conversational language. Keep responses brief unless the user asks for detail."
        )
    return instructions[:8000]


def _session_config(body: dict[str, Any]) -> dict[str, Any]:
    model = _clean_model(body.get("model"))
    voice = _clean_voice(body.get("voice"), model)
    instructions = _clean_instructions(body.get("instructions"))

    return {
        "type": "realtime",
        "model": model,
        "instructions": instructions,
        "audio": {
            "output": {
                "voice": voice,
            },
        },
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
                # gpt-realtime-whisper requires manual buffer commits. The
                # browser performs local VAD and commits one bounded turn.
                "turn_detection": None,
            }
        },
    }


def _safety_identifier(actor_user_id: str) -> str:
    return hashlib.sha256(actor_user_id.encode("utf-8")).hexdigest()


def get_realtime_capabilities() -> dict[str, Any]:
    models = []
    for model_id in (CURRENT_REALTIME_MODEL, ROLLBACK_REALTIME_MODEL):
        models.append({"id": model_id, **REALTIME_MODEL_CAPABILITIES[model_id]})

    return {
        "default_model": DEFAULT_REALTIME_MODEL,
        "default_voice": DEFAULT_REALTIME_VOICE,
        "managed_model": True,
        "conversation_generation_enabled": ALLOW_UNGOVERNED_REALTIME_VOICE,
        "deprecated": True,
        "models": models,
    }


def _raise_upstream_error(response: httpx.Response, error: str) -> None:
    provider_request_id = response.headers.get("x-request-id") or response.headers.get("openai-request-id")
    public_status = 429 if response.status_code == 429 else 503 if response.status_code >= 500 else 502
    raise HTTPException(
        status_code=public_status,
        detail={
            "error": error,
            "upstream_status": response.status_code,
            "provider_request_id": provider_request_id,
        },
    )


async def _openai_webrtc_answer(
    *,
    actor_user_id: str,
    sdp: str,
    session: dict[str, Any],
    mode: str,
) -> Response:
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise HTTPException(status_code=500, detail="missing_openai_key")

    if not sdp.strip():
        raise HTTPException(status_code=400, detail="missing_sdp")

    multipart_fields = {
        "sdp": (None, sdp, "application/sdp"),
        "session": (None, json.dumps(session), "application/json"),
    }

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
                files=multipart_fields,
            )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": "openai_realtime_unreachable"},
        ) from exc

    if upstream.status_code >= 400:
        _raise_upstream_error(upstream, "openai_realtime_call_error")

    return Response(
        content=upstream.text,
        status_code=200,
        media_type="application/sdp",
        headers={
            "cache-control": "no-store",
            "x-vs-voice-provider": "openai",
            "x-vs-realtime-mode": mode,
        },
    )


@router.post("/voice/openai/session")
async def create_realtime_client_secret(req: Request):
    """
    Compatibility endpoint: create a short-lived OpenAI Realtime client secret.

    Preferred browser path is /voice/openai/webrtc-offer, which uses the
    modern WebRTC unified interface and does not expose a reusable secret to
    the browser.
    """

    actor_user_id = _require_actor(req)
    _require_ungoverned_realtime_enabled()

    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise HTTPException(status_code=500, detail="missing_openai_key")

    try:
        body = await req.json()
    except Exception:
        body = {}

    if not isinstance(body, dict):
        body = {}

    session = _session_config(body)

    if body.get("dry_run") is True:
        return {
            "status": "ok",
            "provider": "openai",
            "mode": "client_secret",
            "session": session,
            "dry_run": True,
        }

    payload = {
        "expires_after": {
            "anchor": "created_at",
            "seconds": 600,
        },
        "session": session,
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                OPENAI_REALTIME_CLIENT_SECRETS_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "OpenAI-Safety-Identifier": _safety_identifier(actor_user_id),
                },
                json=payload,
            )
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"error": "openai_realtime_unreachable"}) from exc

    if r.status_code >= 400:
        _raise_upstream_error(r, "openai_realtime_session_error")

    return r.json()


@router.post("/voice/openai/webrtc-offer")
async def create_realtime_webrtc_offer(req: Request):
    """
    Modern browser WebRTC unified interface.

    Input:
    - request body may be raw SDP text, or JSON { "sdp": "...", ...sessionOptions }

    Output:
    - OpenAI answer SDP as application/sdp.
    """

    actor_user_id = _require_actor(req)
    _require_ungoverned_realtime_enabled()

    content_type = (req.headers.get("content-type") or "").lower()
    raw = await req.body()

    body: dict[str, Any] = {}
    sdp = ""

    if "application/json" in content_type:
        try:
            parsed = json.loads(raw.decode("utf-8", "ignore") or "{}")
        except Exception:
            parsed = {}
        if isinstance(parsed, dict):
            body = parsed
            sdp = str(parsed.get("sdp") or "")
    else:
        sdp = raw.decode("utf-8", "ignore")
        # Optional session controls can still be sent as query params.
        body = {
            "model": req.query_params.get("model"),
            "voice": req.query_params.get("voice"),
            "instructions": req.query_params.get("instructions"),
        }

    session = _session_config(body)

    if body.get("dry_run") is True:
        return {
            "status": "ok",
            "provider": "openai",
            "mode": "webrtc_unified",
            "session": session,
            "sdp_chars": len(sdp),
            "dry_run": True,
        }

    return await _openai_webrtc_answer(
        actor_user_id=actor_user_id,
        sdp=sdp,
        session=session,
        mode="webrtc_unified_legacy_generation",
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
    session = _transcription_session_config()

    if req.query_params.get("dry_run") == "1":
        return {
            "status": "ok",
            "provider": "openai",
            "mode": "webrtc_transcription_only",
            "session": session,
            "sdp_chars": len(sdp),
            "dry_run": True,
        }

    return await _openai_webrtc_answer(
        actor_user_id=actor_user_id,
        sdp=sdp,
        session=session,
        mode="webrtc_transcription_only",
    )
