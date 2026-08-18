from __future__ import annotations

import os
from datetime import datetime
from uuid import UUID

import asyncpg
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, field_validator

from seebx.core.ownership import require_actor_matches_owner


router = APIRouter()

DSN = (os.getenv("POSTGRES_DSN") or "").strip()
VOICE_SESSION_HEADER = "x-vs-voice-session-id"
LEASE_SECONDS = 20
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


def _owner_from_request(req: Request) -> str:
    owner_user_id = (req.headers.get("x-vs-owner-user-id") or "").strip()
    if not owner_user_id:
        raise HTTPException(status_code=400, detail="missing_owner_user_id")
    return require_actor_matches_owner(req, owner_user_id)


def _session_id_from_request(req: Request) -> UUID:
    raw = (req.headers.get(VOICE_SESSION_HEADER) or "").strip()
    if not raw:
        raise HTTPException(
            status_code=409,
            detail={"error": "voice_session_required"},
        )
    try:
        return UUID(raw)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_voice_session_id"},
        ) from None


async def _connect() -> asyncpg.Connection:
    if not DSN:
        raise HTTPException(status_code=503, detail="voice_session_unconfigured")
    try:
        return await asyncpg.connect(DSN, command_timeout=10)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "voice_session_store_unavailable"},
        ) from exc


async def require_active_voice_session(
    req: Request,
    owner_user_id: str,
) -> UUID:
    owner = require_actor_matches_owner(req, owner_user_id)
    session_id = _session_id_from_request(req)
    conn = await _connect()
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.user_id', $1, true)",
                owner,
            )
            active = await conn.fetchval(
                """
                SELECT EXISTS(
                  SELECT 1
                  FROM public.voice_session_lease
                  WHERE owner_user_id=$1::uuid
                    AND session_id=$2::uuid
                    AND expires_at > clock_timestamp()
                )
                """,
                owner,
                session_id,
            )
    finally:
        await conn.close()

    if not active:
        raise HTTPException(
            status_code=409,
            detail={"error": "voice_session_not_active"},
        )
    return session_id


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
    req: Request,
):
    owner = _owner_from_request(req)
    conn = await _connect()
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.user_id', $1, true)",
                owner,
            )
            row = await conn.fetchrow(
                """
                INSERT INTO public.voice_session_lease(
                  owner_user_id,
                  session_id,
                  acquired_at,
                  renewed_at,
                  expires_at
                )
                VALUES(
                  $1::uuid,
                  $2::uuid,
                  clock_timestamp(),
                  clock_timestamp(),
                  clock_timestamp() + make_interval(secs => $3)
                )
                ON CONFLICT(owner_user_id) DO UPDATE
                SET
                  session_id=EXCLUDED.session_id,
                  acquired_at=CASE
                    WHEN voice_session_lease.session_id=EXCLUDED.session_id
                    THEN voice_session_lease.acquired_at
                    ELSE clock_timestamp()
                  END,
                  renewed_at=clock_timestamp(),
                  expires_at=clock_timestamp() + make_interval(secs => $3)
                RETURNING session_id, expires_at
                """,
                owner,
                payload.session_id,
                LEASE_SECONDS,
            )
    finally:
        await conn.close()

    return _lease_response(
        session_id=row["session_id"],
        expires_at=row["expires_at"],
        acquired=True,
    )


@router.post("/voice/session/heartbeat")
async def heartbeat_voice_session(
    payload: VoiceSessionRequestV1,
    req: Request,
):
    owner = _owner_from_request(req)
    conn = await _connect()
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.user_id', $1, true)",
                owner,
            )
            row = await conn.fetchrow(
                """
                UPDATE public.voice_session_lease
                SET
                  renewed_at=clock_timestamp(),
                  expires_at=clock_timestamp() + make_interval(secs => $3)
                WHERE owner_user_id=$1::uuid
                  AND session_id=$2::uuid
                  AND expires_at > clock_timestamp()
                RETURNING session_id, expires_at
                """,
                owner,
                payload.session_id,
                LEASE_SECONDS,
            )
    finally:
        await conn.close()

    if row is None:
        raise HTTPException(
            status_code=409,
            detail={"error": "voice_session_not_active"},
        )
    return _lease_response(
        session_id=row["session_id"],
        expires_at=row["expires_at"],
        renewed=True,
    )


@router.post("/voice/session/release")
async def release_voice_session(
    payload: VoiceSessionRequestV1,
    req: Request,
):
    owner = _owner_from_request(req)
    conn = await _connect()
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.user_id', $1, true)",
                owner,
            )
            released = (
                await conn.execute(
                    """
                    DELETE FROM public.voice_session_lease
                    WHERE owner_user_id=$1::uuid
                      AND session_id=$2::uuid
                    """,
                    owner,
                    payload.session_id,
                )
                == "DELETE 1"
            )
    finally:
        await conn.close()

    return JSONResponse(
        {
            "ok": True,
            "session_id": str(payload.session_id),
            "released": released,
        },
        headers=NO_STORE_HEADERS,
    )
