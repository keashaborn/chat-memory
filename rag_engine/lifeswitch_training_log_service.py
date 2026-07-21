from __future__ import annotations

import json
import os
import uuid
from typing import Any

import asyncpg
from fastapi import HTTPException


SCHEMA = os.getenv("LIFESWITCH_TRAINING_SCHEMA", "lifeswitch_training")


def _writer_http_error(exc: asyncpg.PostgresError) -> HTTPException:
    """Translate the stable Training writer contract into route-level errors."""
    sqlstate = getattr(exc, "sqlstate", None)
    detail = getattr(exc, "message", None) or "training write failed"
    if sqlstate in {"22023", "23514"}:
        return HTTPException(status_code=400, detail=detail)
    if sqlstate in {"22001", "22P02", "23502", "23503"}:
        return HTTPException(status_code=400, detail="invalid training write request")
    if sqlstate == "P0002":
        return HTTPException(status_code=404, detail=detail)
    if sqlstate == "23505":
        return HTTPException(status_code=409, detail=detail)
    if sqlstate == "28000":
        return HTTPException(status_code=403, detail="training write not authorized")
    if sqlstate in {"40001", "40P01"}:
        return HTTPException(
            status_code=503,
            detail="training write should be retried",
            headers={"Retry-After": "1"},
        )
    if sqlstate in {"42501", "42883"}:
        return HTTPException(
            status_code=503,
            detail="training write temporarily unavailable",
        )
    return HTTPException(status_code=500, detail="training write failed")


async def set_transaction_actor(conn, *, actor_user_id: str) -> None:
    """Bind the authenticated actor to the current database transaction."""
    await conn.fetchval(
        "select set_config('app.user_id', $1::text, true)",
        actor_user_id,
    )


async def _call_uuid_writer(conn, sql: str, *args: Any, error_message: str) -> uuid.UUID:
    try:
        result = await conn.fetchval(sql, *args)
    except asyncpg.PostgresError as exc:
        raise _writer_http_error(exc) from exc
    if not result:
        raise HTTPException(status_code=500, detail=error_message)
    return result


async def create_training_session(
    conn,
    *,
    intent: dict[str, Any],
    idempotency_key: str,
) -> uuid.UUID:
    return await _call_uuid_writer(
        conn,
        f"select {SCHEMA}.create_training_session($1::jsonb, $2::text)",
        json.dumps(intent, ensure_ascii=False, separators=(",", ":")),
        idempotency_key,
        error_message="training writer returned no session",
    )


async def correct_training_session(
    conn,
    *,
    training_session_id: str | uuid.UUID,
    intent: dict[str, Any],
    idempotency_key: str,
) -> uuid.UUID:
    return await _call_uuid_writer(
        conn,
        f"select {SCHEMA}.correct_training_session($1::uuid, $2::jsonb, $3::text)",
        training_session_id,
        json.dumps(intent, ensure_ascii=False, separators=(",", ":")),
        idempotency_key,
        error_message="training correction returned no session",
    )


async def void_training_session(
    conn,
    *,
    training_session_id: str | uuid.UUID,
    reason: str,
) -> uuid.UUID:
    return await _call_uuid_writer(
        conn,
        f"select {SCHEMA}.void_training_session($1::uuid, $2::text)",
        training_session_id,
        reason,
        error_message="training void returned no session",
    )


async def create_conditioning_session(
    conn,
    *,
    intent: dict[str, Any],
    idempotency_key: str,
) -> uuid.UUID:
    return await _call_uuid_writer(
        conn,
        f"select {SCHEMA}.create_conditioning_session($1::jsonb, $2::text)",
        json.dumps(intent, ensure_ascii=False, separators=(",", ":")),
        idempotency_key,
        error_message="conditioning writer returned no session",
    )


async def correct_conditioning_session(
    conn,
    *,
    conditioning_session_log_id: str | uuid.UUID,
    intent: dict[str, Any],
    idempotency_key: str,
) -> uuid.UUID:
    return await _call_uuid_writer(
        conn,
        f"select {SCHEMA}.correct_conditioning_session($1::uuid, $2::jsonb, $3::text)",
        conditioning_session_log_id,
        json.dumps(intent, ensure_ascii=False, separators=(",", ":")),
        idempotency_key,
        error_message="conditioning correction returned no session",
    )


async def void_conditioning_session(
    conn,
    *,
    conditioning_session_log_id: str | uuid.UUID,
    reason: str,
) -> uuid.UUID:
    return await _call_uuid_writer(
        conn,
        f"select {SCHEMA}.void_conditioning_session($1::uuid, $2::text)",
        conditioning_session_log_id,
        reason,
        error_message="conditioning void returned no session",
    )
