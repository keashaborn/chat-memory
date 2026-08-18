from __future__ import annotations

"""PostgreSQL adapter for owner-scoped voice-session leases."""

import os
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import asyncpg


POSTGRES_DSN = (os.getenv("POSTGRES_DSN") or "").strip()
LEASE_SECONDS = 20


class VoiceSessionStoreUnconfigured(RuntimeError):
    pass


class VoiceSessionStoreUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VoiceSessionLease:
    session_id: UUID
    expires_at: datetime


async def _connect() -> asyncpg.Connection:
    if not POSTGRES_DSN:
        raise VoiceSessionStoreUnconfigured("voice_session_unconfigured")
    try:
        return await asyncpg.connect(POSTGRES_DSN, command_timeout=10)
    except Exception as exc:
        raise VoiceSessionStoreUnavailable(
            "voice_session_store_unavailable"
        ) from exc


async def is_active(owner_user_id: str, session_id: UUID) -> bool:
    conn = await _connect()
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.user_id', $1, true)",
                owner_user_id,
            )
            return bool(
                await conn.fetchval(
                    """
                    SELECT EXISTS(
                      SELECT 1
                      FROM public.voice_session_lease
                      WHERE owner_user_id=$1::uuid
                        AND session_id=$2::uuid
                        AND expires_at > clock_timestamp()
                    )
                    """,
                    owner_user_id,
                    session_id,
                )
            )
    finally:
        await conn.close()


async def acquire(owner_user_id: str, session_id: UUID) -> VoiceSessionLease:
    conn = await _connect()
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.user_id', $1, true)",
                owner_user_id,
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
                owner_user_id,
                session_id,
                LEASE_SECONDS,
            )
    finally:
        await conn.close()
    return VoiceSessionLease(
        session_id=row["session_id"],
        expires_at=row["expires_at"],
    )


async def heartbeat(
    owner_user_id: str,
    session_id: UUID,
) -> VoiceSessionLease | None:
    conn = await _connect()
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.user_id', $1, true)",
                owner_user_id,
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
                owner_user_id,
                session_id,
                LEASE_SECONDS,
            )
    finally:
        await conn.close()
    if row is None:
        return None
    return VoiceSessionLease(
        session_id=row["session_id"],
        expires_at=row["expires_at"],
    )


async def release(owner_user_id: str, session_id: UUID) -> bool:
    conn = await _connect()
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.user_id', $1, true)",
                owner_user_id,
            )
            result = await conn.execute(
                """
                DELETE FROM public.voice_session_lease
                WHERE owner_user_id=$1::uuid
                  AND session_id=$2::uuid
                """,
                owner_user_id,
                session_id,
            )
    finally:
        await conn.close()
    return result == "DELETE 1"


__all__ = [
    "LEASE_SECONDS",
    "VoiceSessionLease",
    "VoiceSessionStoreUnavailable",
    "VoiceSessionStoreUnconfigured",
    "acquire",
    "heartbeat",
    "is_active",
    "release",
]
