from __future__ import annotations

"""Owner-authenticated HTTP boundary for exact chat-source erasure.

The route accepts only the closed Phase 6 conversation-deletion contract. It
derives owner authority from the verified Supabase actor and never accepts an
account, owner, cutoff timestamp, arbitrary Memory scope, or structured
LifeSwitch domain from the caller.
"""

from typing import Any, Protocol
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from .api import (
    CONVERSATION_ERASURE_ROUTE_SPECIFICATION,
    CONVERSATION_ERASURE_STATUS_ROUTE_SPECIFICATION,
)
from .auth import ActorScope
from .contracts import ContractViolation
from .conversation_deletion import (
    DeletionRepositoryError,
    DeletionRepositoryFailure,
    require_request_status_binding,
)
from .deletion_contracts import (
    BoundConversationDeletion,
    ConversationErasureState,
    ConversationErasureStatus,
    DeletionAuthority,
    bind_conversation_deletion,
    conversation_deletion_request_from_body,
)
from .http_api import (
    ActorResolver,
    MemoryHttpError,
    MemoryHttpFailure,
    _OwnerRequestDeadlineRoute,
    _disabled_response,
    _failure_response,
    _prohibit_identity_assertions,
    _read_closed_body,
    _resolve_owner_actor,
    _unconfigured_response,
)


class ConversationErasureRequester(Protocol):
    async def request_erasure(
        self,
        command: BoundConversationDeletion,
    ) -> ConversationErasureStatus: ...

    async def read_erasure_status(
        self,
        authority: DeletionAuthority,
        operation_id: UUID,
    ) -> ConversationErasureStatus | None: ...


def _coded_failure_response(code: str, status_code: int) -> JSONResponse:
    allowed = {
        (
            "governed_project_thread_erasure_required",
            409,
        ),
        ("conversation_erasure_replay_conflict", 409),
        ("conversation_erasure_already_active", 409),
        ("conversation_erasure_selector_not_found", 409),
        ("conversation_erasure_source_precondition_failed", 409),
        ("conversation_unavailable", 503),
    }
    if (code, status_code) not in allowed:
        return _failure_response(MemoryHttpFailure.INTERNAL_ERROR)
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code}},
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _status_response(status: ConversationErasureStatus) -> JSONResponse:
    content = {
        "operation_id": str(status.operation_id),
        "selector_kind": status.selector_kind.value,
        "state": status.state.value,
        "target_count": status.target_count,
        "selector_sha256": status.selector_sha256,
        "target_manifest_sha256": status.target_manifest_sha256,
        "governed_receipt_sha256": status.governed_receipt_sha256,
        "last_error_code": status.last_error_code,
        "created_at": status.created_at,
        "completed_at": status.completed_at,
    }
    location = (
        "/memory/conversations/erasure-requests/" + str(status.operation_id)
    )
    if status.state is ConversationErasureState.COMPLETED:
        status_code = 200
    elif status.state is ConversationErasureState.MANUAL_REVIEW:
        status_code = 409
    else:
        status_code = 202
    headers = {
        "Cache-Control": "no-store",
        "Location": location,
        "X-Content-Type-Options": "nosniff",
    }
    if status_code == 202:
        headers["Retry-After"] = "2"
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(content),
        headers=headers,
    )


def _canonical_operation_id(value: str) -> UUID:
    try:
        parsed = UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ContractViolation("invalid_deletion_operation") from exc
    if str(parsed) != value:
        raise ContractViolation("invalid_deletion_operation")
    return parsed


def create_conversation_erasure_router(
    *,
    actor_resolver: ActorResolver | None = None,
    requester: ConversationErasureRequester | None = None,
    feature_enabled: bool = False,
) -> APIRouter:
    """Create one inactive-by-default route with injected dependencies."""

    router = APIRouter(route_class=_OwnerRequestDeadlineRoute)

    async def request_erasure_endpoint(request: Request) -> JSONResponse:
        if feature_enabled is not True:
            return _disabled_response()
        if actor_resolver is None or requester is None:
            return _unconfigured_response()
        try:
            _prohibit_identity_assertions(request)
            body = await _read_closed_body(
                request,
                CONVERSATION_ERASURE_ROUTE_SPECIFICATION,
            )
            deletion_request = conversation_deletion_request_from_body(body)
        except MemoryHttpError as exc:
            return _failure_response(exc.failure)
        except ContractViolation:
            return _failure_response(MemoryHttpFailure.REQUEST_INVALID)

        try:
            actor = await _resolve_owner_actor(
                request,
                resolver=actor_resolver,
                scopes=(ActorScope.ERASE_CONVERSATIONS,),
            )
        except MemoryHttpError as exc:
            return _failure_response(exc.failure)
        try:
            command = bind_conversation_deletion(
                actor=actor,
                request=deletion_request,
            )
            status = require_request_status_binding(
                command,
                await requester.request_erasure(command),
            )
        except DeletionRepositoryError as exc:
            if exc.failure is DeletionRepositoryFailure.REQUEST_INVALID:
                return _failure_response(MemoryHttpFailure.REQUEST_INVALID)
            if (
                exc.failure
                is DeletionRepositoryFailure.LEGACY_PROJECT_THREAD_DEPENDENCY
            ):
                return _coded_failure_response(exc.code, 409)
            if exc.failure in {
                DeletionRepositoryFailure.REPLAY_CONFLICT,
                DeletionRepositoryFailure.ALREADY_ACTIVE,
                DeletionRepositoryFailure.SELECTOR_NOT_FOUND,
                DeletionRepositoryFailure.SOURCE_PRECONDITION_FAILED,
            }:
                return _coded_failure_response(exc.code, 409)
            if (
                exc.failure
                is DeletionRepositoryFailure.CONVERSATION_UNAVAILABLE
            ):
                return _coded_failure_response(exc.code, 503)
            return _failure_response(
                MemoryHttpFailure.OPERATION_OUTCOME_UNKNOWN
            )
        except Exception:
            return _failure_response(
                MemoryHttpFailure.OPERATION_OUTCOME_UNKNOWN
            )
        return _status_response(status)

    async def read_erasure_status_endpoint(
        operation_id: str,
        request: Request,
    ) -> JSONResponse:
        if feature_enabled is not True:
            return _disabled_response()
        if actor_resolver is None or requester is None:
            return _unconfigured_response()
        try:
            _prohibit_identity_assertions(request)
            await _read_closed_body(
                request,
                CONVERSATION_ERASURE_STATUS_ROUTE_SPECIFICATION,
            )
            parsed_operation_id = _canonical_operation_id(operation_id)
            actor = await _resolve_owner_actor(
                request,
                resolver=actor_resolver,
                scopes=(ActorScope.ERASE_CONVERSATIONS,),
            )
            authority = DeletionAuthority.from_verified_actor(actor)
            status = await requester.read_erasure_status(
                authority,
                parsed_operation_id,
            )
        except MemoryHttpError as exc:
            return _failure_response(exc.failure)
        except ContractViolation:
            return _failure_response(MemoryHttpFailure.REQUEST_INVALID)
        except DeletionRepositoryError as exc:
            if exc.failure is DeletionRepositoryFailure.CONVERSATION_UNAVAILABLE:
                return _coded_failure_response(exc.code, 503)
            return _failure_response(MemoryHttpFailure.SUCCESSOR_UNAVAILABLE)
        except Exception:
            return _failure_response(MemoryHttpFailure.SUCCESSOR_UNAVAILABLE)
        if status is None:
            return _failure_response(MemoryHttpFailure.RESOURCE_NOT_FOUND)
        return _status_response(status)

    specification = CONVERSATION_ERASURE_ROUTE_SPECIFICATION
    router.add_api_route(
        specification.path,
        request_erasure_endpoint,
        methods=[specification.method.value],
        name=specification.operation,
        response_class=JSONResponse,
    )
    status_specification = CONVERSATION_ERASURE_STATUS_ROUTE_SPECIFICATION
    router.add_api_route(
        status_specification.path,
        read_erasure_status_endpoint,
        methods=[status_specification.method.value],
        name=status_specification.operation,
        response_class=JSONResponse,
    )
    return router


__all__ = [
    "ConversationErasureRequester",
    "create_conversation_erasure_router",
]
