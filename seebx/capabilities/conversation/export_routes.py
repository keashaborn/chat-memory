from __future__ import annotations

"""HTTP boundary for the owner-scoped conversation and memory export."""

from datetime import datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from seebx.capabilities.conversation.export import (
    ConversationExportError,
    ConversationExportService,
)
from seebx.core.identity import require_verified_supabase_request_identity


NO_STORE_HEADERS = {
    "cache-control": "private, no-store, max-age=0, must-revalidate",
    "pragma": "no-cache",
    "expires": "0",
    "x-content-type-options": "nosniff",
}


def _response_headers(request: Request) -> dict[str, str]:
    request_id = (
        request.headers.get("x-request-id")
        or request.headers.get("x-correlation-id")
        or ""
    ).strip()
    if not request_id or len(request_id) > 128:
        request_id = str(uuid4())
    return {**NO_STORE_HEADERS, "x-request-id": request_id}


def create_conversation_export_router(
    service: ConversationExportService,
) -> APIRouter:
    router = APIRouter()

    @router.get("/conversation/export")
    async def export_conversation_data(request: Request) -> Response:
        headers = _response_headers(request)
        try:
            identity = await require_verified_supabase_request_identity(request)
            owner_user_id = UUID(identity.actor_user_id)
        except HTTPException as error:
            return JSONResponse(
                {
                    "status": {
                        400: "bad_request",
                        401: "unauthorized",
                        403: "forbidden",
                        503: "unavailable",
                    }.get(error.status_code, "error"),
                    "detail": str(error.detail),
                },
                status_code=error.status_code,
                headers=headers,
            )
        except Exception:
            return JSONResponse(
                {"status": "unavailable", "detail": "invalid_verified_actor"},
                status_code=503,
                headers=headers,
            )
        try:
            artifact = await service.build(owner_user_id)
        except ConversationExportError as error:
            return JSONResponse(
                {"status": "error", "detail": error.code},
                status_code=error.status_code,
                headers=headers,
            )
        stamp = datetime.fromisoformat(
            artifact.generated_at.replace("Z", "+00:00")
        ).strftime("%Y%m%dT%H%M%SZ")
        return Response(
            content=artifact.body,
            media_type="application/json",
            headers={
                **headers,
                "content-disposition": (
                    "attachment; filename=\"lifeswitch-conversation-memory-"
                    f"{stamp}.json\""
                ),
                "x-content-sha256": artifact.sha256,
            },
        )

    return router


__all__ = ["create_conversation_export_router"]
