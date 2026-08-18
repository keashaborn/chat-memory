from __future__ import annotations

"""HTTP contract for owner-scoped voice-session leases."""

from datetime import datetime
from typing import Awaitable, TypeVar
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, field_validator

from seebx.adapters import voice_session as store
from seebx.core.ownership import require_actor_matches_owner
from seebx.core.voice_identity import (
    require_active_voice_session,
    voice_session_store_http_exception,
)


router = APIRouter()
ResultT = TypeVar("ResultT")

LEASE_SECONDS = store.LEASE_SECONDS
NO_STORE_HEADERS = {
    "cache-control": "private, no-store, max-age=0, must-revalidate",
    "pragma": "no-cache",
    "expires": "0",
}


class VoiceSessionRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    session_id: UUID

    @field_validator("session_id", mode="before")
    @classmethod
    def parse_wire_uuid(cls, value: object) -> object:
        if isinstance(value, UUID):
            return value
        if not isinstance(value, str):
            raise ValueError("session_id must be a UUID string")
        try:
            return UUID(value)
        except ValueError:
            raise ValueError("session_id is invalid") from None


def _owner_from_request(request: Request) -> str:
    owner_user_id = (
        request.headers.get("x-vs-owner-user-id") or ""
    ).strip()
    if not owner_user_id:
        raise HTTPException(status_code=400, detail="missing_owner_user_id")
    return require_actor_matches_owner(request, owner_user_id)


async def _store_call(awaitable: Awaitable[ResultT]) -> ResultT:
    try:
        return await awaitable
    except (
        store.VoiceSessionStoreUnconfigured,
        store.VoiceSessionStoreUnavailable,
    ) as exc:
        raise voice_session_store_http_exception(exc) from exc


def _lease_response(
    *,
    session_id: UUID,
    expires_at: datetime,
    acquired: bool | None = None,
    renewed: bool | None = None,
) -> JSONResponse:
    payload: dict[str, object] = {
        "ok": True,
        "session_id": str(session_id),
        "lease_seconds": LEASE_SECONDS,
        "expires_at": expires_at.isoformat(),
    }
    if acquired is not None:
        payload["acquired"] = acquired
    if renewed is not None:
        payload["renewed"] = renewed
    return JSONResponse(payload, headers=NO_STORE_HEADERS)


@router.post("/voice/session/acquire")
async def acquire_voice_session(
    payload: VoiceSessionRequestV1,
    request: Request,
):
    owner = _owner_from_request(request)
    lease = await _store_call(store.acquire(owner, payload.session_id))
    return _lease_response(
        session_id=lease.session_id,
        expires_at=lease.expires_at,
        acquired=True,
    )


@router.post("/voice/session/heartbeat")
async def heartbeat_voice_session(
    payload: VoiceSessionRequestV1,
    request: Request,
):
    owner = _owner_from_request(request)
    lease = await _store_call(store.heartbeat(owner, payload.session_id))
    if lease is None:
        raise HTTPException(
            status_code=409,
            detail={"error": "voice_session_not_active"},
        )
    return _lease_response(
        session_id=lease.session_id,
        expires_at=lease.expires_at,
        renewed=True,
    )


@router.post("/voice/session/release")
async def release_voice_session(
    payload: VoiceSessionRequestV1,
    request: Request,
):
    owner = _owner_from_request(request)
    released = await _store_call(store.release(owner, payload.session_id))
    return JSONResponse(
        {
            "ok": True,
            "session_id": str(payload.session_id),
            "released": released,
        },
        headers=NO_STORE_HEADERS,
    )


__all__ = [
    "LEASE_SECONDS",
    "NO_STORE_HEADERS",
    "VoiceSessionRequestV1",
    "acquire_voice_session",
    "heartbeat_voice_session",
    "release_voice_session",
    "require_active_voice_session",
    "router",
]
