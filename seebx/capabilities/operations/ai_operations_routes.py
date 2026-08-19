from __future__ import annotations

"""Verified HTTP boundary for the SeeBx AI Operations capability."""

import hmac
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from seebx.capabilities.operations.ai_operations import (
    MANAGE_CAPABILITY,
    READ_CAPABILITY,
    AiOperationsError,
    acknowledge_admin_ai_operations_incident_v1,
    list_admin_ai_operations_incidents_v1,
    resolve_admin_ai_operations_incident_v1,
)
from seebx.core.identity import require_verified_supabase_request_identity

NO_STORE_HEADERS = {
    "cache-control": "private, no-store, max-age=0, must-revalidate",
    "pragma": "no-cache",
    "expires": "0",
    "x-content-type-options": "nosniff",
}


def _request_id(request: Request) -> str:
    value = str(getattr(request.state, "request_id", "") or "").strip()
    return value if value and len(value) <= 128 else str(uuid4())


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
) -> JSONResponse:
    request_id = _request_id(request)
    return JSONResponse(
        {
            "ok": False,
            "error": code,
            "request_id": request_id,
        },
        status_code=status_code,
        headers={**NO_STORE_HEADERS, "x-request-id": request_id},
    )


def _identity_error_response(
    request: Request,
    error: HTTPException,
) -> JSONResponse:
    return _error_response(
        request,
        status_code=error.status_code,
        code=str(error.detail),
    )


async def _require_ai_operations_actor(
    request: Request,
    required_capability: str,
) -> tuple[JSONResponse | None, str | None]:
    try:
        identity = await require_verified_supabase_request_identity(request)
        actor_user_id = str(UUID(identity.actor_user_id))
    except HTTPException as error:
        return _identity_error_response(request, error), None
    except (TypeError, ValueError, AttributeError):
        return (
            _error_response(
                request,
                status_code=503,
                code="invalid_verified_actor",
            ),
            None,
        )

    capability = (
        request.headers.get("x-vs-authorized-capability") or ""
    ).strip()
    if not hmac.compare_digest(capability, required_capability):
        return (
            _error_response(
                request,
                status_code=403,
                code="capability_required",
            ),
            None,
        )
    return None, actor_user_id


def create_ai_operations_router(dsn: str) -> APIRouter:
    if not str(dsn or "").strip():
        raise ValueError("ai_operations_dsn_required")

    router = APIRouter()

    @router.get("/admin/ai-operations/incidents")
    async def admin_ai_operations_incidents(request: Request):
        denied, actor = await _require_ai_operations_actor(
            request,
            READ_CAPABILITY,
        )
        if denied is not None:
            return denied
        params = request.query_params
        state = (params.get("state") or "").strip() or None
        try:
            limit = int(params.get("limit") or 50)
            return await list_admin_ai_operations_incidents_v1(
                dsn=dsn,
                actor_user_id=actor,
                state=state,
                limit=limit,
            )
        except (TypeError, ValueError):
            return _error_response(
                request,
                status_code=400,
                code="invalid_ai_operations_query",
            )
        except AiOperationsError as error:
            return _error_response(
                request,
                status_code=error.status_code,
                code=error.code,
            )

    @router.post(
        "/admin/ai-operations/incidents/{incident_id}/acknowledge"
    )
    async def admin_ai_operations_acknowledge(
        incident_id: str,
        request: Request,
    ):
        denied, actor = await _require_ai_operations_actor(
            request,
            MANAGE_CAPABILITY,
        )
        if denied is not None:
            return denied
        try:
            return await acknowledge_admin_ai_operations_incident_v1(
                dsn=dsn,
                actor_user_id=actor,
                incident_id=incident_id,
            )
        except (TypeError, ValueError):
            return _error_response(
                request,
                status_code=400,
                code="invalid_incident_id",
            )
        except AiOperationsError as error:
            return _error_response(
                request,
                status_code=error.status_code,
                code=error.code,
            )

    @router.post(
        "/admin/ai-operations/incidents/{incident_id}/resolve"
    )
    async def admin_ai_operations_resolve(
        incident_id: str,
        request: Request,
    ):
        denied, actor = await _require_ai_operations_actor(
            request,
            MANAGE_CAPABILITY,
        )
        if denied is not None:
            return denied
        try:
            return await resolve_admin_ai_operations_incident_v1(
                dsn=dsn,
                actor_user_id=actor,
                incident_id=incident_id,
            )
        except (TypeError, ValueError):
            return _error_response(
                request,
                status_code=400,
                code="invalid_incident_id",
            )
        except AiOperationsError as error:
            return _error_response(
                request,
                status_code=error.status_code,
                code=error.code,
            )

    return router


__all__ = ["create_ai_operations_router"]
