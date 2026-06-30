from fastapi import APIRouter, Request, HTTPException
import os
import uuid
import httpx

router = APIRouter()

DEFAULT_REALTIME_MODEL = (os.getenv("OPENAI_REALTIME_MODEL") or "gpt-4o-realtime-preview").strip()
OPENAI_REALTIME_SESSIONS_URL = (
    os.getenv("OPENAI_REALTIME_SESSIONS_URL")
    or "https://api.openai.com/v1/realtime/sessions"
).strip()

ALLOWED_VOICES = {
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
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


def _clean_model(raw: object) -> str:
    model = str(raw or DEFAULT_REALTIME_MODEL or "gpt-4o-realtime-preview").strip()
    # OpenAI-only rule: reject provider-prefixed stale values such as xai:grok-*
    if not model or ":" in model:
        return DEFAULT_REALTIME_MODEL or "gpt-4o-realtime-preview"
    return model[:120]


def _clean_voice(raw: object) -> str:
    voice = str(raw or "alloy").strip().lower()
    return voice if voice in ALLOWED_VOICES else "alloy"


@router.post("/voice/openai/session")
async def create_realtime_session(req: Request):
    """
    Mint an ephemeral OpenAI Realtime session for browser use.

    Security boundary:
    - x-vs-service-token is enforced by app.py middleware.
    - x-vs-actor-user-id is required here before any OpenAI call is made.
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

    voice = _clean_voice(body.get("voice"))
    model = _clean_model(body.get("model"))
    instructions = str(body.get("instructions") or "").strip()[:8000]

    payload = {
        "model": model,
        "voice": voice,
        "instructions": instructions,
        "modalities": ["audio", "text"],
    }

    # Safe route probe. Requires service token + actor, but avoids vendor call.
    if body.get("dry_run") is True:
        return {
            "status": "ok",
            "provider": "openai",
            "actor_user_id": actor_user_id,
            "model": model,
            "voice": voice,
            "modalities": payload["modalities"],
            "dry_run": True,
        }

    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(
            OPENAI_REALTIME_SESSIONS_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=r.text)

    return r.json()
