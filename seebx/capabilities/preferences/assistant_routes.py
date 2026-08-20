from __future__ import annotations

"""Authenticated HTTP boundary for explicit assistant response preferences."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypeVar
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from seebx.adapters.assistant_preferences_postgres import (
    PostgresAssistantPreferencesRepository,
    PreferenceCandidateUnavailable,
    PreferencesConflict,
)
from seebx.adapters.postgres import PostgresConnectionProvider
from seebx.capabilities.preferences.assistant_compiler import (
    CompilerUnavailable,
    OpenAIPreferenceCompiler,
)
from seebx.capabilities.preferences.assistant_contracts import (
    AssistantPreferencesPublic,
    AssistantPreferencesUpdate,
    PreferenceApprovalRequest,
    PreferenceCompilationCandidate,
    PreferenceCompilationRequest,
)
from seebx.core.identity import require_verified_supabase_actor


NO_STORE_HEADERS = {
    "cache-control": "private, no-store, max-age=0, must-revalidate",
    "pragma": "no-cache",
    "expires": "0",
    "x-content-type-options": "nosniff",
}

IdentityVerifier = Callable[[Request, str], Awaitable[str]]
ModelT = TypeVar("ModelT", bound=BaseModel)


class PreferenceCompiler(Protocol):
    def compile(
        self,
        *,
        owner_user_id: UUID,
        source_revision: int,
        narrative: str,
    ) -> PreferenceCompilationCandidate: ...


class PreferenceRepository(Protocol):
    async def get(self, owner: UUID) -> Any: ...

    async def put(self, owner: UUID, value: AssistantPreferencesUpdate) -> Any: ...

    async def revision(self, owner: UUID) -> int: ...

    async def store_candidate(
        self,
        candidate: PreferenceCompilationCandidate,
    ) -> None: ...

    async def approve(
        self,
        owner: UUID,
        candidate_id: UUID,
        expected_revision: int,
    ) -> Any: ...


def _json(payload: dict[str, Any], *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        payload,
        status_code=status_code,
        headers=NO_STORE_HEADERS,
    )


def _preferences(value: AssistantPreferencesPublic) -> JSONResponse:
    return _json(value.model_dump(mode="json"))


async def _body(request: Request, model: type[ModelT], detail: str) -> ModelT:
    try:
        raw = await request.json()
        return model.model_validate(raw)
    except (ValidationError, ValueError, TypeError):
        raise HTTPException(
            status_code=400,
            detail=detail,
            headers=NO_STORE_HEADERS,
        ) from None


async def _verified_owner(
    request: Request,
    owner_user_id: UUID,
    identity_verifier: IdentityVerifier,
) -> UUID:
    try:
        actor = await identity_verifier(request, str(owner_user_id))
    except HTTPException as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers={**(exc.headers or {}), **NO_STORE_HEADERS},
        ) from exc
    try:
        owner = UUID(str(actor))
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(
            status_code=503,
            detail="invalid_verified_actor",
            headers=NO_STORE_HEADERS,
        ) from None
    if owner != owner_user_id:
        raise HTTPException(
            status_code=403,
            detail="supabase_actor_owner_mismatch",
            headers=NO_STORE_HEADERS,
        )
    return owner


def _database_unavailable(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="preferences_unavailable",
        headers=NO_STORE_HEADERS,
    )


def create_assistant_preferences_router(
    postgres: PostgresConnectionProvider | None = None,
    *,
    repository: PreferenceRepository | None = None,
    compiler_factory: Callable[[], PreferenceCompiler] = OpenAIPreferenceCompiler,
    identity_verifier: IdentityVerifier = require_verified_supabase_actor,
) -> APIRouter:
    if repository is None:
        if postgres is None:
            raise ValueError("PostgreSQL provider is required")
        repository = PostgresAssistantPreferencesRepository(postgres)
    repo = repository
    router = APIRouter(prefix="/assistant-preferences")

    @router.get("/{owner_user_id}")
    async def get_preferences(
        owner_user_id: UUID,
        request: Request,
    ) -> JSONResponse:
        owner = await _verified_owner(request, owner_user_id, identity_verifier)
        try:
            bundle = await repo.get(owner)
        except Exception as exc:
            raise _database_unavailable(exc) from exc
        return _preferences(bundle.public_value())

    @router.put("/{owner_user_id}")
    async def put_preferences(
        owner_user_id: UUID,
        request: Request,
    ) -> JSONResponse:
        owner = await _verified_owner(request, owner_user_id, identity_verifier)
        value = await _body(
            request,
            AssistantPreferencesUpdate,
            "invalid_preferences_request",
        )
        try:
            bundle = await repo.put(owner, value)
        except PreferencesConflict as exc:
            raise HTTPException(
                status_code=409,
                detail="preferences_revision_conflict",
                headers=NO_STORE_HEADERS,
            ) from exc
        except Exception as exc:
            raise _database_unavailable(exc) from exc
        return _preferences(bundle.public_value())

    @router.post("/{owner_user_id}/compile")
    async def compile_preferences(
        owner_user_id: UUID,
        request: Request,
    ) -> JSONResponse:
        owner = await _verified_owner(request, owner_user_id, identity_verifier)
        value = await _body(
            request,
            PreferenceCompilationRequest,
            "invalid_preference_compilation_request",
        )
        try:
            current_revision = await repo.revision(owner)
        except Exception as exc:
            raise _database_unavailable(exc) from exc
        if current_revision != value.expected_revision:
            raise HTTPException(
                status_code=409,
                detail="preferences_revision_conflict",
                headers=NO_STORE_HEADERS,
            )
        try:
            candidate = await asyncio.to_thread(
                compiler_factory().compile,
                owner_user_id=owner,
                source_revision=value.expected_revision,
                narrative=value.narrative,
            )
        except CompilerUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail="preference_compiler_unavailable",
                headers=NO_STORE_HEADERS,
            ) from exc
        try:
            await repo.store_candidate(candidate)
        except PreferencesConflict as exc:
            raise HTTPException(
                status_code=409,
                detail="preferences_revision_conflict",
                headers=NO_STORE_HEADERS,
            ) from exc
        except Exception as exc:
            raise _database_unavailable(exc) from exc
        return _json(candidate.public_payload())

    @router.post("/{owner_user_id}/approve")
    async def approve_preferences(
        owner_user_id: UUID,
        request: Request,
    ) -> JSONResponse:
        owner = await _verified_owner(request, owner_user_id, identity_verifier)
        value = await _body(
            request,
            PreferenceApprovalRequest,
            "invalid_preference_approval_request",
        )
        try:
            bundle = await repo.approve(
                owner,
                value.candidate_id,
                value.expected_revision,
            )
        except PreferencesConflict as exc:
            raise HTTPException(
                status_code=409,
                detail="preferences_revision_conflict",
                headers=NO_STORE_HEADERS,
            ) from exc
        except PreferenceCandidateUnavailable as exc:
            raise HTTPException(
                status_code=409,
                detail="preference_compilation_candidate_unavailable",
                headers=NO_STORE_HEADERS,
            ) from exc
        except Exception as exc:
            raise _database_unavailable(exc) from exc
        return _preferences(bundle.public_value())

    return router


__all__ = ["NO_STORE_HEADERS", "create_assistant_preferences_router"]
