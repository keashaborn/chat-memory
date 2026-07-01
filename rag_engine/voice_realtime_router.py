from __future__ import annotations

import hashlib
import json
import os
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

router = APIRouter()

DEFAULT_REALTIME_MODEL = (os.getenv("OPENAI_REALTIME_MODEL") or "gpt-realtime-2").strip()
DEFAULT_REALTIME_VOICE = (os.getenv("OPENAI_REALTIME_VOICE") or "marin").strip().lower()

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

ALLOWED_VOICES = {
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
}


def _require_actor(req: Request) -> str:
    actor = (req.headers.get("x-vs-actor-user-id") or "").strip()
    if not actor:
        raise HTTPException(status_code=401, detail="missing_actor_user_id")
    try:
        return str(uuid.UUID(actor))
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_actor_user_id")


def _clean_model(raw: Any) -> str:
    model = str(raw or DEFAULT_REALTIME_MODEL or "gpt-realtime-2").strip()
    # OpenAI-only rule: reject provider-prefixed stale values.
    if not model or ":" in model:
        return DEFAULT_REALTIME_MODEL or "gpt-realtime-2"
    return model[:120]


def _clean_voice(raw: Any) -> str:
    voice = str(raw or DEFAULT_REALTIME_VOICE or "marin").strip().lower()
    return voice if voice in ALLOWED_VOICES else (DEFAULT_REALTIME_VOICE if DEFAULT_REALTIME_VOICE in ALLOWED_VOICES else "marin")


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
    voice = _clean_voice(body.get("voice"))
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


def _safety_identifier(actor_user_id: str) -> str:
    return hashlib.sha256(actor_user_id.encode("utf-8")).hexdigest()


@router.post("/voice/openai/session")
async def create_realtime_client_secret(req: Request):
    """
    Compatibility endpoint: create a short-lived OpenAI Realtime client secret.

    Preferred browser path is /voice/openai/webrtc-offer, which uses the
    modern WebRTC unified interface and does not expose a reusable secret to
    the browser.
    """

    actor_user_id = _require_actor(req)

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
            "actor_user_id": actor_user_id,
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

    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=r.text)

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

    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise HTTPException(status_code=500, detail="missing_openai_key")

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
            "actor_user_id": actor_user_id,
            "session": session,
            "sdp_chars": len(sdp),
            "dry_run": True,
        }

    if not sdp.strip():
        raise HTTPException(status_code=400, detail="missing_sdp")

    print(
        f"[voice-webrtc] offer actor={actor_user_id} raw_len={len(raw)} sdp_len={len(sdp)}",
        flush=True,
    )

    # OpenAI Realtime unified WebRTC expects multipart FormData string fields.
    # In httpx, filename=None creates normal multipart fields, equivalent to
    # FormData.set("sdp", raw_sdp) and FormData.set("session", session_json).
    multipart_fields = {
        "sdp": (None, sdp),
        "session": (None, json.dumps(session)),
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        r = await client.post(
            OPENAI_REALTIME_CALLS_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "OpenAI-Safety-Identifier": _safety_identifier(actor_user_id),
            },
            files=multipart_fields,
        )

    if r.status_code >= 400:
        print(f"[voice-webrtc] openai_error status={r.status_code}", flush=True)
        raise HTTPException(status_code=502, detail=r.text[:4000])

    answer_text = r.text
    print(
        f"[voice-webrtc] openai_ok status={r.status_code} answer_len={len(answer_text)}",
        flush=True,
    )

    return Response(
        content=answer_text,
        status_code=200,
        media_type="application/sdp",
        headers={
            "x-vs-voice-provider": "openai",
            "x-vs-realtime-mode": "webrtc_unified",
        },
    )
