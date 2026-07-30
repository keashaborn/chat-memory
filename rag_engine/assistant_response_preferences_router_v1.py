from __future__ import annotations

"""Authenticated API for explicit AI response preferences."""

import asyncio
import os
from uuid import UUID

import asyncpg
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from rag_engine.assistant_response_preference_compiler_store_v1 import (
    PreferenceCompilationCandidateUnavailableV1,
    approve_compilation_candidate_v1,
    load_preference_public_state_v1,
    store_compilation_candidate_v1,
)
from rag_engine.assistant_response_preference_compiler_v1 import (
    AssistantPreferenceCompilationApprovalV1,
    AssistantPreferenceCompilationInputV1,
    OpenAIAssistantPreferenceCompilerV1,
    PreferenceCompilerUnavailableV1,
)
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
    public_state: dict[str, object],
    *,
    status_code: int = 200,
) -> JSONResponse:
    payload = value.model_dump(mode="json")
    payload["custom_instructions"] = public_state["custom_instructions"]
    payload["compilation"] = public_state["compilation"]
    return JSONResponse(
        payload,
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
            public_state = await load_preference_public_state_v1(conn, owner)
        return _response(value, public_state)
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
                public_state = await load_preference_public_state_v1(
                    conn,
                    owner,
                )
        except AssistantResponsePreferenceConflictV1 as exc:
            raise HTTPException(
                status_code=409,
                detail="preferences_revision_conflict",
            ) from exc
        return _response(saved, public_state)
    finally:
        await conn.close()


@router.post("/{owner_user_id}/compile")
async def compile_assistant_response_preferences_v1(
    owner_user_id: UUID,
    value: AssistantPreferenceCompilationInputV1,
    req: Request,
) -> JSONResponse:
    if not DSN:
        raise HTTPException(status_code=503, detail="preferences_unconfigured")
    owner = UUID(await require_memory_actor_v1(req, str(owner_user_id)))

    conn = await asyncpg.connect(DSN, command_timeout=10)
    try:
        async with conn.transaction():
            await set_preference_actor_v1(conn, owner)
            current = await load_assistant_response_preferences_v1(conn, owner)
        if current.revision != value.expected_revision:
            raise HTTPException(
                status_code=409,
                detail="preferences_revision_conflict",
            )
    finally:
        await conn.close()

    try:
        candidate = await asyncio.to_thread(
            OpenAIAssistantPreferenceCompilerV1().compile,
            owner_user_id=owner,
            source_revision=value.expected_revision,
            narrative=value.narrative,
        )
    except PreferenceCompilerUnavailableV1 as exc:
        raise HTTPException(
            status_code=503,
            detail="preference_compiler_unavailable",
        ) from exc

    conn = await asyncpg.connect(DSN, command_timeout=10)
    try:
        try:
            async with conn.transaction():
                await set_preference_actor_v1(conn, owner)
                await store_compilation_candidate_v1(conn, candidate)
        except AssistantResponsePreferenceConflictV1 as exc:
            raise HTTPException(
                status_code=409,
                detail="preferences_revision_conflict",
            ) from exc
        return JSONResponse(
            candidate.public_payload(),
            headers=NO_STORE_HEADERS,
        )
    finally:
        await conn.close()


@router.post("/{owner_user_id}/approve")
async def approve_assistant_response_preferences_v1(
    owner_user_id: UUID,
    value: AssistantPreferenceCompilationApprovalV1,
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
                saved = await approve_compilation_candidate_v1(
                    conn,
                    owner_user_id=owner,
                    candidate_id=value.candidate_id,
                    expected_revision=value.expected_revision,
                )
                public_state = await load_preference_public_state_v1(
                    conn,
                    owner,
                )
        except AssistantResponsePreferenceConflictV1 as exc:
            raise HTTPException(
                status_code=409,
                detail="preferences_revision_conflict",
            ) from exc
        except PreferenceCompilationCandidateUnavailableV1 as exc:
            raise HTTPException(
                status_code=409,
                detail="preference_compilation_candidate_unavailable",
            ) from exc
        return _response(saved, public_state)
    finally:
        await conn.close()


__all__ = ["router"]
