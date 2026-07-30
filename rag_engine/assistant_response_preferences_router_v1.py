from __future__ import annotations

"""Authenticated API for explicit AI response preferences."""

import os
from uuid import UUID

import asyncpg
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from rag_engine.assistant_response_preferences_store_v1 import (
    AssistantResponsePreferenceConflictV1,
    load_assistant_response_preferences_v1,
    save_assistant_response_preferences_v1,
    set_preference_actor_v1,
)
from rag_engine.assistant_response_preferences_v1 import (
    AssistantResponsePreferencesInputV1,
    AssistantResponsePreferencesV1,
)
from rag_engine.memory_actor_auth_v1 import require_memory_actor_v1


router = APIRouter()
DSN = (os.getenv("POSTGRES_DSN") or "").strip()
NO_STORE_HEADERS = {
    "cache-control": "private, no-store, max-age=0, must-revalidate",
    "pragma": "no-cache",
    "expires": "0",
}


def _response(
    value: AssistantResponsePreferencesV1,
    *,
    status_code: int = 200,
) -> JSONResponse:
    return JSONResponse(
        value.model_dump(mode="json"),
        status_code=status_code,
        headers=NO_STORE_HEADERS,
    )


@router.get("/{owner_user_id}")
async def get_assistant_response_preferences_v1(
    owner_user_id: UUID,
    req: Request,
) -> JSONResponse:
    if not DSN:
        raise HTTPException(status_code=503, detail="preferences_unconfigured")
    owner = UUID(await require_memory_actor_v1(req, str(owner_user_id)))
    conn = await asyncpg.connect(DSN, command_timeout=10)
    try:
        async with conn.transaction():
            await set_preference_actor_v1(conn, owner)
            value = await load_assistant_response_preferences_v1(conn, owner)
        return _response(value)
    finally:
        await conn.close()


@router.put("/{owner_user_id}")
async def put_assistant_response_preferences_v1(
    owner_user_id: UUID,
    value: AssistantResponsePreferencesInputV1,
    req: Request,
) -> JSONResponse:
    if not DSN:
        raise HTTPException(status_code=503, detail="preferences_unconfigured")
    owner = UUID(await require_memory_actor_v1(req, str(owner_user_id)))
    conn = await asyncpg.connect(DSN, command_timeout=10)
    try:
        try:
            async with conn.transaction():
                await set_preference_actor_v1(conn, owner)
                saved = await save_assistant_response_preferences_v1(
                    conn,
                    owner,
                    value,
                )
        except AssistantResponsePreferenceConflictV1 as exc:
            raise HTTPException(
                status_code=409,
                detail="preferences_revision_conflict",
            ) from exc
        return _response(saved)
    finally:
        await conn.close()


__all__ = ["router"]
