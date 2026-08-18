from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from seebx.core.ownership import require_actor_matches_owner
from seebx.adapters.openai_realtime import (
    OpenAIRealtimeProtocolError,
    OpenAIRealtimeTimeoutError,
    OpenAIRealtimeUnavailableError,
    OpenAIRealtimeUpstreamError,
    create_transcription_call,
)
from seebx.adapters.voice_realtime_config import (
    VoiceRealtimeConfigurationError,
    load_voice_realtime_secrets,
)
from seebx.capabilities.voice.realtime_session import (
    RealtimePreviewSessionRegistry,
)
from rag_engine.voice_realtime_sideband_controller import (
    RealtimePreviewSidebandController,
    RealtimePreviewSidebandNotReady,
)
from seebx.core.voice_identity import require_active_voice_session
from seebx.contracts.voice_language import (
    voice_language_from_request,
)


router = APIRouter()
preview_sessions = RealtimePreviewSessionRegistry()
sideband_controller_factory = RealtimePreviewSidebandController

MAX_SDP_BYTES = 128 * 1024
NO_STORE_HEADERS = {
    "cache-control": "private, no-store, max-age=0, must-revalidate",
    "pragma": "no-cache",
    "expires": "0",
    "x-content-type-options": "nosniff",
}


def _normalized_content_type(raw: Any) -> str:
    return str(raw or "").split(";", 1)[0].strip().lower()


def _owner_from_request(req: Request) -> str:
    owner_user_id = (req.headers.get("x-vs-owner-user-id") or "").strip()
    if not owner_user_id:
        raise HTTPException(status_code=400, detail="missing_owner_user_id")
    return require_actor_matches_owner(req, owner_user_id)


def _voice_session_id_from_request(req: Request) -> uuid.UUID:
    raw = (req.headers.get("x-vs-voice-session-id") or "").strip()
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_voice_session_id"},
        ) from None


def _thread_id_from_request(req: Request) -> uuid.UUID:
    raw = (req.headers.get("x-vs-thread-id") or "").strip()
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_thread_id"},
        ) from None


def _public_upstream_status(status_code: int) -> int:
    if status_code == 429:
        return 429
    if status_code >= 500:
        return 503
    return 502


@router.post("/voice/realtime-preview/call")
async def create_realtime_preview_call(req: Request):
    owner_user_id = _owner_from_request(req)
    language = voice_language_from_request(req)
    voice_session_id = await require_active_voice_session(req, owner_user_id)
    thread_id = _thread_id_from_request(req)

    if _normalized_content_type(req.headers.get("content-type")) != "application/sdp":
        raise HTTPException(
            status_code=415,
            detail={"error": "unsupported_content_type"},
        )

    raw_sdp = await req.body()
    if not raw_sdp:
        raise HTTPException(status_code=400, detail={"error": "missing_sdp"})
    if len(raw_sdp) > MAX_SDP_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "error": "sdp_too_large",
                "maximum_bytes": MAX_SDP_BYTES,
            },
        )
    try:
        offer_sdp = raw_sdp.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_sdp_encoding"},
        ) from exc
    if not offer_sdp.strip().startswith("v=0"):
        raise HTTPException(status_code=400, detail={"error": "invalid_sdp"})

    try:
        secrets = load_voice_realtime_secrets()
    except VoiceRealtimeConfigurationError as exc:
        raise HTTPException(
            status_code=(
                500 if exc.code == "missing_openai_key" else 503
            ),
            detail=exc.code,
        ) from exc

    try:
        provider_call = await create_transcription_call(
            owner_user_id=owner_user_id,
            offer_sdp=offer_sdp,
            language=language,
            api_key=secrets.openai_api_key,
        )
    except OpenAIRealtimeTimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail={"error": "openai_realtime_timeout"},
        ) from exc
    except OpenAIRealtimeUnavailableError as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": "openai_realtime_unreachable"},
        ) from exc
    except OpenAIRealtimeUpstreamError as exc:
        raise HTTPException(
            status_code=_public_upstream_status(exc.status_code),
            detail={
                "error": "openai_realtime_error",
                "upstream_status": exc.status_code,
                "provider_request_id": exc.provider_request_id,
            },
        ) from exc
    except OpenAIRealtimeProtocolError as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": exc.code},
        ) from exc

    session = preview_sessions.register(
        owner_user_id=owner_user_id,
        voice_session_id=voice_session_id,
        thread_id=thread_id,
        openai_call_id=provider_call.call_id,
        language=language,
    )
    try:
        controller = sideband_controller_factory(
            session=session,
            api_key=secrets.openai_api_key,
            service_token=secrets.service_token,
        )
        session.controller = controller
        controller.start()
    except Exception as exc:
        preview_sessions.remove_owned(
            preview_session_id=session.preview_session_id,
            owner_user_id=owner_user_id,
            voice_session_id=voice_session_id,
        )
        raise HTTPException(
            status_code=503,
            detail={"error": "realtime_sideband_unavailable"},
        ) from exc

    return Response(
        content=provider_call.answer_sdp,
        media_type="application/sdp",
        headers={
            **NO_STORE_HEADERS,
            "x-vs-realtime-preview-session-id": str(
                session.preview_session_id
            ),
            "x-vs-realtime-preview-mode": "transcription-only",
        },
    )


def _owned_session_or_404(
    *,
    preview_session_id: uuid.UUID,
    owner_user_id: str,
    voice_session_id: uuid.UUID,
):
    session = preview_sessions.get_owned(
        preview_session_id=preview_session_id,
        owner_user_id=owner_user_id,
        voice_session_id=voice_session_id,
    )
    if session is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "realtime_preview_session_not_found"},
        )
    return session


@router.post("/voice/realtime-preview/session/{preview_session_id}/commit")
async def commit_realtime_preview_audio(
    preview_session_id: uuid.UUID,
    req: Request,
):
    owner_user_id = _owner_from_request(req)
    voice_session_id = await require_active_voice_session(
        req,
        owner_user_id,
    )
    session = _owned_session_or_404(
        preview_session_id=preview_session_id,
        owner_user_id=owner_user_id,
        voice_session_id=voice_session_id,
    )
    controller = session.controller
    if controller is None:
        raise HTTPException(
            status_code=409,
            detail={"error": "realtime_sideband_not_ready"},
        )
    try:
        await controller.commit(
            web_search_authorized=(
                (
                    req.headers.get("x-vs-web-search-authorization")
                    or ""
                ).strip()
                == "supabase_fresh_voice_lease_v1"
            )
        )
    except RealtimePreviewSidebandNotReady as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "realtime_sideband_not_ready"},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "realtime_sideband_unavailable"},
        ) from exc
    return JSONResponse(
        {
            "ok": True,
            "preview_session_id": str(preview_session_id),
            "committed": True,
        },
        headers=NO_STORE_HEADERS,
    )


@router.get("/voice/realtime-preview/session/{preview_session_id}/events")
async def get_realtime_preview_events(
    preview_session_id: uuid.UUID,
    req: Request,
    after: int = 0,
    limit: int = 50,
):
    if after < 0 or limit < 1 or limit > 100:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_event_cursor"},
        )
    owner_user_id = _owner_from_request(req)
    voice_session_id = await require_active_voice_session(
        req,
        owner_user_id,
    )
    session = _owned_session_or_404(
        preview_session_id=preview_session_id,
        owner_user_id=owner_user_id,
        voice_session_id=voice_session_id,
    )
    events, next_cursor = session.events_after(after, limit=limit)
    return JSONResponse(
        {
            "ok": True,
            "preview_session_id": str(preview_session_id),
            "events": events,
            "next_cursor": next_cursor,
        },
        headers=NO_STORE_HEADERS,
    )


@router.delete("/voice/realtime-preview/session/{preview_session_id}")
async def close_realtime_preview_session(
    preview_session_id: uuid.UUID,
    req: Request,
):
    owner_user_id = _owner_from_request(req)
    voice_session_id = _voice_session_id_from_request(req)
    session = preview_sessions.pop_owned(
        preview_session_id=preview_session_id,
        owner_user_id=owner_user_id,
        voice_session_id=voice_session_id,
    )
    if session is not None and session.controller is not None:
        await session.controller.close()
    return JSONResponse(
        {
            "ok": True,
            "preview_session_id": str(preview_session_id),
            "removed": session is not None,
        },
        headers=NO_STORE_HEADERS,
    )
