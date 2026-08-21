from __future__ import annotations

"""PostgreSQL stored-function adapter for atomic LifeSwitch Training writes."""

import json
import os
import re
import uuid
from typing import Any

import asyncpg


_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")


def resolve_training_schema(value: str | None = None) -> str:
    candidate = str(
        os.getenv("LIFESWITCH_TRAINING_SCHEMA", "lifeswitch_training")
        if value is None
        else value
    ).strip()
    if _IDENTIFIER.fullmatch(candidate) is None:
        raise RuntimeError("invalid LIFESWITCH_TRAINING_SCHEMA")
    return candidate


class TrainingWriterError(RuntimeError):
    """Provider-neutral failure returned to the Training capability."""

    def __init__(
        self,
        *,
        detail: str,
        sqlstate: str | None = None,
        no_result: bool = False,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.sqlstate = sqlstate
        self.no_result = no_result


async def set_transaction_actor(conn: Any, *, actor_user_id: str) -> None:
    """Bind the authenticated actor to the caller-owned database transaction."""
    await conn.fetchval(
        "select set_config('app.user_id', $1::text, true)",
        actor_user_id,
    )


async def _call_uuid_writer(
    conn: Any,
    sql: str,
    *args: Any,
    error_message: str,
) -> uuid.UUID:
    try:
        result = await conn.fetchval(sql, *args)
    except asyncpg.PostgresError as error:
        raise TrainingWriterError(
            sqlstate=getattr(error, "sqlstate", None),
            detail=getattr(error, "message", None) or "training write failed",
        ) from error
    if not result:
        raise TrainingWriterError(detail=error_message, no_result=True)
    return result


async def create_training_session(
    conn: Any,
    *,
    intent: dict[str, Any],
    idempotency_key: str,
) -> uuid.UUID:
    schema = resolve_training_schema()
    return await _call_uuid_writer(
        conn,
        f"select {schema}.create_training_session($1::jsonb, $2::text)",
        json.dumps(intent, ensure_ascii=False, separators=(",", ":")),
        idempotency_key,
        error_message="training writer returned no session",
    )


async def correct_training_session(
    conn: Any,
    *,
    training_session_id: str | uuid.UUID,
    intent: dict[str, Any],
    idempotency_key: str,
) -> uuid.UUID:
    schema = resolve_training_schema()
    return await _call_uuid_writer(
        conn,
        f"select {schema}.correct_training_session($1::uuid, $2::jsonb, $3::text)",
        training_session_id,
        json.dumps(intent, ensure_ascii=False, separators=(",", ":")),
        idempotency_key,
        error_message="training correction returned no session",
    )


async def void_training_session(
    conn: Any,
    *,
    training_session_id: str | uuid.UUID,
    reason: str,
) -> uuid.UUID:
    schema = resolve_training_schema()
    return await _call_uuid_writer(
        conn,
        f"select {schema}.void_training_session($1::uuid, $2::text)",
        training_session_id,
        reason,
        error_message="training void returned no session",
    )


async def create_conditioning_session(
    conn: Any,
    *,
    intent: dict[str, Any],
    idempotency_key: str,
) -> uuid.UUID:
    schema = resolve_training_schema()
    return await _call_uuid_writer(
        conn,
        f"select {schema}.create_conditioning_session($1::jsonb, $2::text)",
        json.dumps(intent, ensure_ascii=False, separators=(",", ":")),
        idempotency_key,
        error_message="conditioning writer returned no session",
    )


async def correct_conditioning_session(
    conn: Any,
    *,
    conditioning_session_log_id: str | uuid.UUID,
    intent: dict[str, Any],
    idempotency_key: str,
) -> uuid.UUID:
    schema = resolve_training_schema()
    return await _call_uuid_writer(
        conn,
        f"select {schema}.correct_conditioning_session($1::uuid, $2::jsonb, $3::text)",
        conditioning_session_log_id,
        json.dumps(intent, ensure_ascii=False, separators=(",", ":")),
        idempotency_key,
        error_message="conditioning correction returned no session",
    )


async def void_conditioning_session(
    conn: Any,
    *,
    conditioning_session_log_id: str | uuid.UUID,
    reason: str,
) -> uuid.UUID:
    schema = resolve_training_schema()
    return await _call_uuid_writer(
        conn,
        f"select {schema}.void_conditioning_session($1::uuid, $2::text)",
        conditioning_session_log_id,
        reason,
        error_message="conditioning void returned no session",
    )
