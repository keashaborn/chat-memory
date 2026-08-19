"""Canonical HTTP boundary for owner-scoped conversation erasure."""

from __future__ import annotations

import asyncio
from typing import Literal, Optional
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from seebx.adapters.zep_cloud import ZepConfigurationError
from seebx.capabilities.conversation.erasure import (
    ChatHistoryClearError,
    ConversationErasureService,
)
from seebx.contracts.identifiers import CanonicalJsonUUID
from seebx.core.identity import require_verified_supabase_request_identity


SUCCESSOR_MEMORY_REFUSAL_HEADERS = {
    "cache-control": "private, no-store, max-age=0, must-revalidate",
    "pragma": "no-cache",
    "expires": "0",
}


class ChatHistoryClearReq(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    scope: Literal["all", "recent", "thread", "message_tail"]
    thread_id: Optional[CanonicalJsonUUID] = None
    anchor_message_id: Optional[CanonicalJsonUUID] = None
    recent_window_seconds: Optional[int] = None
    confirmation: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def exact_scope(self) -> "ChatHistoryClearReq":
        expected_confirmation = {
            "all": "CLEAR CHAT HISTORY",
            "recent": "CLEAR RECENT CHAT HISTORY",
            "thread": "CLEAR CHAT",
            "message_tail": "CLEAR MESSAGE TAIL",
        }[self.scope]
        if self.confirmation != expected_confirmation:
            raise ValueError("invalid confirmation")
        if self.scope == "thread":
            if (
                self.thread_id is None
                or self.anchor_message_id is not None
                or self.recent_window_seconds is not None
            ):
                raise ValueError("invalid thread clear shape")
        elif self.scope == "message_tail":
            if (
                self.thread_id is None
                or self.anchor_message_id is None
                or self.recent_window_seconds is not None
            ):
                raise ValueError("invalid message tail clear shape")
        elif self.scope == "recent":
            if (
                self.thread_id is not None
                or self.anchor_message_id is not None
                or self.recent_window_seconds
                not in {3_600, 86_400, 604_800, 2_592_000}
            ):
                raise ValueError("invalid recent clear shape")
        elif (
            self.thread_id is not None
            or self.anchor_message_id is not None
            or self.recent_window_seconds is not None
        ):
            raise ValueError("invalid all clear shape")
        return self


def _parse_uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _identity_error_response(error: HTTPException) -> JSONResponse:
    status = {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        503: "unavailable",
    }.get(error.status_code, "error")
    return JSONResponse(
        {"status": status, "detail": str(error.detail)},
        status_code=error.status_code,
    )


async def _require_verified_deletion_actor(
    request: Request,
) -> tuple[JSONResponse | None, uuid.UUID | None]:
    try:
        identity = await require_verified_supabase_request_identity(request)
    except HTTPException as error:
        return _identity_error_response(error), None
    owner_user_id = _parse_uuid(identity.actor_user_id)
    if owner_user_id is None:
        return JSONResponse(
            {"status": "unavailable", "detail": "invalid_verified_actor"},
            status_code=503,
        ), None
    return None, owner_user_id


def _conversation_erasure_required(
    operation: str,
    selector_kind: str,
) -> JSONResponse:
    return JSONResponse(
        {
            "status": "conflict",
            "detail": "legacy_conversation_deletion_route_retired",
            "operation": operation,
            "selector_kind": selector_kind,
            "canonical_route": (
                "/memory/chat-and-zep/clear"
                if selector_kind == "all_conversations"
                else "/chat-history/clear"
            ),
        },
        status_code=410,
        headers=SUCCESSOR_MEMORY_REFUSAL_HEADERS,
    )


def create_conversation_erasure_router(
    service: ConversationErasureService,
) -> APIRouter:
    if service is None:
        raise ValueError("conversation erasure service is required")

    router = APIRouter()

    @router.post("/chat-history/clear")
    async def chat_history_clear(body: ChatHistoryClearReq, req: Request):
        denied, owner_user_id = await _require_verified_deletion_actor(req)
        if denied is not None:
            return denied
        authorization = (req.headers.get("authorization") or "").strip()
        operation_id = (
            body.anchor_message_id
            if body.scope == "message_tail"
            else uuid.uuid4()
        )

        try:
            result = await service.clear_history(
                owner_user_id=owner_user_id,
                authorization=authorization,
                operation_id=operation_id,
                scope=body.scope,
                thread_id=body.thread_id,
                recent_window_seconds=body.recent_window_seconds,
            )
        except ChatHistoryClearError as error:
            return JSONResponse(
                {"status": "error", "detail": error.code},
                status_code=error.status_code,
                headers=SUCCESSOR_MEMORY_REFUSAL_HEADERS,
            )
        return result.as_dict()

    @router.delete("/memory/chat-and-zep/clear")
    async def chat_and_zep_full_clear(req: Request):
        denied, owner_user_id = await _require_verified_deletion_actor(req)
        if denied is not None:
            return denied
        authorization = (req.headers.get("authorization") or "").strip()
        operation_id = uuid.uuid4()

        try:
            result = await service.clear_all_chat_and_memory(
                owner_user_id=owner_user_id,
                authorization=authorization,
                operation_id=operation_id,
            )
        except ChatHistoryClearError as error:
            return JSONResponse(
                {"status": "error", "detail": error.code},
                status_code=error.status_code,
                headers=SUCCESSOR_MEMORY_REFUSAL_HEADERS,
            )
        except (ZepConfigurationError, asyncio.TimeoutError):
            return JSONResponse(
                {"status": "error", "detail": "zep_memory_deletion_unavailable"},
                status_code=503,
                headers=SUCCESSOR_MEMORY_REFUSAL_HEADERS,
            )
        except Exception:
            return JSONResponse(
                {"status": "error", "detail": "full_ai_data_deletion_unavailable"},
                status_code=503,
                headers=SUCCESSOR_MEMORY_REFUSAL_HEADERS,
            )

        return {
            "contract_version": "chat_and_zep_full_clear_v1",
            "status": "completed",
            "operation_id": str(result.operation_id),
            "deleted_message_count": result.deleted_message_count,
            "deleted_thread_count": result.deleted_thread_count,
            "deleted_outbox_count": result.deleted_outbox_count,
            "chat_receipt_sha256": result.receipt_sha256,
            "completed_at": result.completed_at,
            "memory_retained": False,
            "zep_called": True,
            "zep_deleted": True,
        }

    @router.delete("/threads/{thread_id}/messages/{message_id}/truncate")
    async def threads_truncate_from_message(
        thread_id: str,
        message_id: str,
        req: Request,
    ):
        return _conversation_erasure_required(
            "message_tail_delete",
            "message_tail",
        )

    @router.delete("/threads/{thread_id}")
    async def threads_delete(thread_id: str, req: Request):
        return _conversation_erasure_required("thread_delete", "thread")

    @router.delete("/user/{user_id}/data")
    async def delete_all_user_data(user_id: str, req: Request):
        return _conversation_erasure_required(
            "delete_all_user_data",
            "all_conversations",
        )

    @router.delete("/user/{user_id}/recent")
    async def delete_recent_user_data(
        user_id: str,
        req: Request,
        minutes: int = 60,
    ):
        return _conversation_erasure_required(
            "delete_recent_user_data",
            "recent",
        )

    return router


__all__ = [
    "ChatHistoryClearReq",
    "SUCCESSOR_MEMORY_REFUSAL_HEADERS",
    "create_conversation_erasure_router",
]
