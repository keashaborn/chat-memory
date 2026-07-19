from __future__ import annotations

import datetime as dt
import uuid
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Protocol

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .legacy_plan_adoption import LegacyPlanAdoptionResult, LegacyPlanAdoptionService
from .plan_domain import PlanDocumentV1, PlanDomainError, RevisionState, RevisionTrigger
from .plan_recommendations import (
    PlanRecommendationError,
    PlanRecommendationService,
    RecommendationFocus,
)
from .plan_read_repository import (
    ActivePlanView,
    PlanReadRepository,
    PlanVersionSummary,
    PlanVersionView,
    RevisionReviewView,
)
from .plan_repository import ActivationResult, PlanRepository, ProposalResult, RevisionRecord


class ConnectionProvider(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[asyncpg.Connection]: ...


@dataclass(frozen=True, slots=True)
class ActorContext:
    actor_user_id: uuid.UUID
    owner_user_id: uuid.UUID
    owner_timezone: str
    permission_scopes: frozenset[str] = frozenset()

    @property
    def is_owner(self) -> bool:
        return self.actor_user_id == self.owner_user_id

    def permits(self, scope: str) -> bool:
        return self.is_owner or scope in self.permission_scopes


ActorDependency = Callable[[Request], Awaitable[ActorContext]]


class StrictRequestModel(BaseModel):
    class Config:
        extra = "forbid"


class CreateDraftRequest(StrictRequestModel):
    document: dict[str, Any]
    base_plan_version_id: uuid.UUID | None = None
    supersedes_revision_id: uuid.UUID | None = None


class SaveDraftRequest(StrictRequestModel):
    document: dict[str, Any]


class PlanRecommendationRequest(StrictRequestModel):
    focus: RecommendationFocus = "whole_plan"
    user_request: str = Field(default="", max_length=1200)


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    if value is None:
        value = request.headers.get("x-request-id")
    cleaned = str(value or "").strip()
    return cleaned or None


def _require(context: ActorContext, scope: str) -> None:
    if not context.permits(scope):
        raise PlanDomainError("permission_denied", "required plan permission is unavailable")


def _domain_http_error(error: PlanDomainError) -> HTTPException:
    if error.code == "invalid_stored_json":
        return _internal_http_error()
    if error.code in {"permission_denied", "owner_approval_required", "owner_actor_mismatch"}:
        status_code = 403
    elif error.code in {
        "entity_out_of_scope",
        "legacy_plan_unavailable",
        "legacy_adoption_unavailable",
    }:
        status_code = 404
    elif error.code in {
        "state_conflict",
        "idempotency_conflict",
        "idempotency_in_progress",
        "revision_not_proposed",
        "validation_version_stale",
        "active_plan_required",
        "no_plan_changes",
        "revision_not_draft",
    }:
        status_code = 409
    elif error.code in {
        "revision_not_valid",
        "invalid_input",
        "invalid_phase",
        "invalid_date",
        "invalid_section",
        "unknown_plan_field",
        "unsupported_schema_version",
        "field_too_long",
        "invalid_json_key",
        "invalid_json_number",
        "invalid_json_value",
        "invalid_actor",
        "invalid_author",
        "invalid_owner_timezone",
        "request_id_too_long",
    }:
        status_code = 422
    else:
        status_code = 400
    message = "Resource unavailable." if error.code == "entity_out_of_scope" else str(error)
    return HTTPException(
        status_code=status_code,
        detail={"code": error.code, "message": message, "retryable": False},
    )


def _internal_http_error() -> HTTPException:
    return HTTPException(
        status_code=500,
        detail={
            "code": "internal_error",
            "message": "The plan request could not be completed.",
            "retryable": True,
        },
    )


def _recommendation_http_error(error: PlanRecommendationError) -> HTTPException:
    return HTTPException(
        status_code=503 if error.retryable else 422,
        detail={
            "code": error.code,
            "message": str(error),
            "retryable": error.retryable,
        },
    )


def _iso(value: dt.date | dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _revision_record(result: RevisionRecord) -> dict[str, Any]:
    return {
        "revision_id": str(result.revision_id),
        "base_plan_version_id": (
            str(result.base_plan_version_id) if result.base_plan_version_id else None
        ),
        "state": result.state.value,
        "document_sha256": result.document_sha256,
        "active_plan_changed": False,
    }


def _proposal_result(result: ProposalResult) -> dict[str, Any]:
    return {
        "revision_id": str(result.revision_id),
        "state": result.state.value,
        "validation_status": result.validation_status,
        "change_count": result.change_count,
        "active_plan_changed": False,
    }


def _activation_result(result: ActivationResult) -> dict[str, Any]:
    return {
        "revision_id": str(result.revision_id),
        "plan_version_id": str(result.plan_version_id),
        "version_number": result.version_number,
        "prior_plan_version_id": (
            str(result.prior_plan_version_id) if result.prior_plan_version_id else None
        ),
        "state": RevisionState.ACTIVATED.value,
        "active_plan_changed": True,
    }


def _legacy_adoption_result(result: LegacyPlanAdoptionResult) -> dict[str, Any]:
    payload = _revision_record(result.revision)
    payload["source"] = {
        "kind": result.source_kind,
        "action": (
            "refresh" if result.refreshed else "revision" if result.imported else "adoption"
        ),
        "legacy_plan_profile_id": str(result.legacy_plan_profile_id),
        "legacy_profile_updated_at": _iso(result.legacy_profile_updated_at),
    }
    payload["created"] = result.created
    payload["refreshed"] = result.refreshed
    payload["imported"] = result.imported
    return payload


def _active_plan(view: ActivePlanView) -> dict[str, Any]:
    return {
        "plan_version_id": str(view.plan_version_id),
        "version_number": view.version_number,
        "status": view.status,
        "owner_timezone": view.owner_timezone,
        "document": dict(view.document),
        "activated_at": _iso(view.activated_at),
    }


def _version_summary(view: PlanVersionSummary) -> dict[str, Any]:
    return {
        "plan_version_id": str(view.plan_version_id),
        "version_number": view.version_number,
        "status": view.status,
        "phase_code": view.phase_code,
        "goal_summary": view.goal_summary,
        "activated_at": _iso(view.activated_at),
        "superseded_at": _iso(view.superseded_at),
    }


def _version(view: PlanVersionView) -> dict[str, Any]:
    return {
        "plan_version_id": str(view.plan_version_id),
        "version_number": view.version_number,
        "status": view.status,
        "owner_timezone": view.owner_timezone,
        "document": dict(view.document),
        "document_sha256": view.document_sha256,
        "source_revision_id": str(view.source_revision_id) if view.source_revision_id else None,
        "activated_at": _iso(view.activated_at),
        "superseded_at": _iso(view.superseded_at),
    }


def _revision_review(view: RevisionReviewView, context: ActorContext) -> dict[str, Any]:
    return {
        "revision_id": str(view.revision_id),
        "state": view.state,
        "base_plan_version_id": (
            str(view.base_plan_version_id) if view.base_plan_version_id else None
        ),
        "current_active_plan_version_id": (
            str(view.current_active_plan_version_id)
            if view.current_active_plan_version_id
            else None
        ),
        "base_is_current": view.base_is_current,
        "author_type": view.author_type,
        "trigger_type": view.trigger_type,
        "proposed_document": dict(view.proposed_document),
        "proposed_document_sha256": view.proposed_document_sha256,
        "validation_result": (
            dict(view.validation_result) if view.validation_result is not None else None
        ),
        "changes": [
            {
                "field_path": change.field_path,
                "old_present": change.old_present,
                "old_value": change.old_value,
                "new_present": change.new_present,
                "new_value": change.new_value,
                "rationale": change.rationale,
            }
            for change in view.changes
        ],
        "proposed_at": _iso(view.proposed_at),
        "resolved_at": _iso(view.resolved_at),
        "activated_plan_version_id": (
            str(view.activated_plan_version_id) if view.activated_plan_version_id else None
        ),
        "source": dict(view.source) if view.source is not None else None,
        "can_approve": (
            context.is_owner
            and view.state == RevisionState.PROPOSED.value
            and view.base_is_current
        ),
    }


def create_plan_router(
    *,
    connection_provider: ConnectionProvider,
    actor_dependency: ActorDependency,
    plan_repository: PlanRepository | None = None,
    read_repository: PlanReadRepository | None = None,
    legacy_adoption_service: LegacyPlanAdoptionService | None = None,
    recommendation_service: PlanRecommendationService | None = None,
) -> APIRouter:
    write_repo = plan_repository or PlanRepository()
    read_repo = read_repository or PlanReadRepository()
    adoption_service = legacy_adoption_service or LegacyPlanAdoptionService(
        plan_repository=write_repo
    )
    router = APIRouter(tags=["LifeSwitch Plan Agentic"])

    @router.get("/workspace")
    async def get_plan_workspace(
        context: ActorContext = Depends(actor_dependency),
    ) -> dict[str, Any]:
        try:
            _require(context, "plan:view")
            async with connection_provider() as conn:
                active = await read_repo.get_active_plan(
                    conn,
                    owner_user_id=context.owner_user_id,
                )
                open_revision = await read_repo.get_latest_open_revision(
                    conn,
                    owner_user_id=context.owner_user_id,
                )
            return {
                "active_plan": _active_plan(active) if active else None,
                "open_revision": (
                    _revision_review(open_revision, context) if open_revision else None
                ),
                "capabilities": {
                    "can_edit": context.permits("plan:edit"),
                    "can_approve": context.is_owner,
                },
            }
        except PlanDomainError as error:
            raise _domain_http_error(error) from error
        except asyncpg.PostgresError as error:
            raise _internal_http_error() from error

    @router.get("/active")
    async def get_active_plan(
        context: ActorContext = Depends(actor_dependency),
    ) -> dict[str, Any]:
        try:
            _require(context, "plan:view")
            async with connection_provider() as conn:
                view = await read_repo.get_active_plan(conn, owner_user_id=context.owner_user_id)
            return {"active_plan": _active_plan(view) if view else None}
        except PlanDomainError as error:
            raise _domain_http_error(error) from error
        except asyncpg.PostgresError as error:
            raise _internal_http_error() from error

    @router.get("/versions")
    async def list_plan_versions(
        limit: int = Query(20, ge=1, le=50),
        before_version: int | None = Query(None, ge=1),
        context: ActorContext = Depends(actor_dependency),
    ) -> dict[str, Any]:
        try:
            _require(context, "plan:view")
            async with connection_provider() as conn:
                versions = await read_repo.list_plan_versions(
                    conn,
                    owner_user_id=context.owner_user_id,
                    limit=limit,
                    before_version=before_version,
                )
            visible_versions = versions[:limit]
            next_before = (
                visible_versions[-1].version_number if len(versions) > limit else None
            )
            return {
                "versions": [_version_summary(version) for version in visible_versions],
                "next_before_version": next_before,
            }
        except PlanDomainError as error:
            raise _domain_http_error(error) from error
        except asyncpg.PostgresError as error:
            raise _internal_http_error() from error

    @router.get("/versions/{plan_version_id}")
    async def get_plan_version(
        plan_version_id: uuid.UUID,
        context: ActorContext = Depends(actor_dependency),
    ) -> dict[str, Any]:
        try:
            _require(context, "plan:view")
            async with connection_provider() as conn:
                view = await read_repo.get_plan_version(
                    conn,
                    owner_user_id=context.owner_user_id,
                    plan_version_id=plan_version_id,
                )
            if view is None:
                raise PlanDomainError("entity_out_of_scope", "plan version is unavailable")
            return {"plan_version": _version(view)}
        except PlanDomainError as error:
            raise _domain_http_error(error) from error
        except asyncpg.PostgresError as error:
            raise _internal_http_error() from error

    @router.get("/revisions/{revision_id}")
    async def get_revision_review(
        revision_id: uuid.UUID,
        context: ActorContext = Depends(actor_dependency),
    ) -> dict[str, Any]:
        try:
            _require(context, "plan:view")
            async with connection_provider() as conn:
                view = await read_repo.get_revision_review(
                    conn,
                    owner_user_id=context.owner_user_id,
                    revision_id=revision_id,
                )
            if view is None:
                raise PlanDomainError("entity_out_of_scope", "revision is unavailable")
            return {"revision": _revision_review(view, context)}
        except PlanDomainError as error:
            raise _domain_http_error(error) from error
        except asyncpg.PostgresError as error:
            raise _internal_http_error() from error

    @router.post("/revisions/{revision_id}/recommendations")
    async def review_draft_with_sage(
        revision_id: uuid.UUID,
        body: PlanRecommendationRequest,
        context: ActorContext = Depends(actor_dependency),
    ) -> dict[str, Any]:
        try:
            _require(context, "plan:edit")
            if recommendation_service is None:
                raise PlanRecommendationError(
                    "recommendation_provider_unavailable",
                    "Sage Plan review is not configured.",
                    retryable=True,
                )
            async with connection_provider() as conn:
                view = await read_repo.get_revision_review(
                    conn,
                    owner_user_id=context.owner_user_id,
                    revision_id=revision_id,
                )
            if view is None:
                raise PlanDomainError("entity_out_of_scope", "revision is unavailable")
            if view.state != RevisionState.DRAFT.value:
                raise PlanDomainError(
                    "revision_not_draft",
                    "Sage can review only an inactive draft revision",
                )
            document = PlanDocumentV1.from_mapping(view.proposed_document)
            recommendation = await recommendation_service.review_draft(
                document=document,
                focus=body.focus,
                user_request=body.user_request.strip(),
            )
            return {
                "revision_id": str(view.revision_id),
                "state": view.state,
                "active_plan_changed": False,
                "recommendation": recommendation,
            }
        except PlanDomainError as error:
            raise _domain_http_error(error) from error
        except PlanRecommendationError as error:
            raise _recommendation_http_error(error) from error
        except asyncpg.PostgresError as error:
            raise _internal_http_error() from error

    @router.post("/revisions", status_code=201)
    async def create_draft(
        body: CreateDraftRequest,
        request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        context: ActorContext = Depends(actor_dependency),
    ) -> dict[str, Any]:
        try:
            _require(context, "plan:edit")
            document = PlanDocumentV1.from_mapping(body.document)
            trigger = (
                RevisionTrigger.INITIAL_PLAN
                if body.base_plan_version_id is None
                else (
                    RevisionTrigger.OWNER_REQUEST
                    if context.is_owner
                    else RevisionTrigger.COACH_REQUEST
                )
            )
            async with connection_provider() as conn:
                result = await write_repo.create_draft(
                    conn,
                    owner_user_id=context.owner_user_id,
                    document=document,
                    trigger=trigger,
                    base_plan_version_id=body.base_plan_version_id,
                    author_type="owner" if context.is_owner else "coach",
                    idempotency_key=idempotency_key,
                    author_actor_user_id=context.actor_user_id,
                    supersedes_revision_id=body.supersedes_revision_id,
                    request_id=_request_id(request),
                )
            return _revision_record(result)
        except PlanDomainError as error:
            raise _domain_http_error(error) from error
        except asyncpg.PostgresError as error:
            raise _internal_http_error() from error

    @router.post("/revisions/adopt-current-profile", status_code=201)
    async def adopt_current_profile(
        request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        context: ActorContext = Depends(actor_dependency),
    ) -> dict[str, Any]:
        try:
            if not context.is_owner:
                raise PlanDomainError(
                    "owner_approval_required",
                    "only the owner can adopt the current plan profile",
                )
            async with connection_provider() as conn:
                result = await adoption_service.adopt_current_profile_as_draft(
                    conn,
                    owner_user_id=context.owner_user_id,
                    idempotency_key=idempotency_key,
                    request_id=_request_id(request),
                )
            return _legacy_adoption_result(result)
        except PlanDomainError as error:
            raise _domain_http_error(error) from error
        except asyncpg.PostgresError as error:
            raise _internal_http_error() from error

    @router.post("/revisions/from-current-profile", status_code=201)
    async def create_revision_from_current_profile(
        request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        context: ActorContext = Depends(actor_dependency),
    ) -> dict[str, Any]:
        try:
            _require(context, "plan:edit")
            async with connection_provider() as conn:
                result = await adoption_service.create_revision_from_current_profile(
                    conn,
                    owner_user_id=context.owner_user_id,
                    actor_user_id=context.actor_user_id,
                    author_type="owner" if context.is_owner else "coach",
                    trigger=(
                        RevisionTrigger.OWNER_REQUEST
                        if context.is_owner
                        else RevisionTrigger.COACH_REQUEST
                    ),
                    idempotency_key=idempotency_key,
                    request_id=_request_id(request),
                )
            return _legacy_adoption_result(result)
        except PlanDomainError as error:
            raise _domain_http_error(error) from error
        except asyncpg.PostgresError as error:
            raise _internal_http_error() from error

    @router.post("/revisions/adopt-current-profile/refresh")
    async def refresh_adopted_profile(
        request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        context: ActorContext = Depends(actor_dependency),
    ) -> dict[str, Any]:
        try:
            if not context.is_owner:
                raise PlanDomainError(
                    "owner_approval_required",
                    "only the owner can refresh the adopted plan draft",
                )
            async with connection_provider() as conn:
                result = await adoption_service.refresh_adopted_draft_from_current_profile(
                    conn,
                    owner_user_id=context.owner_user_id,
                    idempotency_key=idempotency_key,
                    request_id=_request_id(request),
                )
            return _legacy_adoption_result(result)
        except PlanDomainError as error:
            raise _domain_http_error(error) from error
        except asyncpg.PostgresError as error:
            raise _internal_http_error() from error

    @router.post("/revisions/{revision_id}/refresh-current-profile")
    async def refresh_revision_from_current_profile(
        revision_id: uuid.UUID,
        request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        context: ActorContext = Depends(actor_dependency),
    ) -> dict[str, Any]:
        try:
            _require(context, "plan:edit")
            async with connection_provider() as conn:
                result = await adoption_service.refresh_draft_from_current_profile(
                    conn,
                    owner_user_id=context.owner_user_id,
                    revision_id=revision_id,
                    actor_user_id=context.actor_user_id,
                    idempotency_key=idempotency_key,
                    request_id=_request_id(request),
                )
            return _legacy_adoption_result(result)
        except PlanDomainError as error:
            raise _domain_http_error(error) from error
        except asyncpg.PostgresError as error:
            raise _internal_http_error() from error

    @router.put("/revisions/{revision_id}/draft")
    async def save_draft(
        revision_id: uuid.UUID,
        body: SaveDraftRequest,
        request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        context: ActorContext = Depends(actor_dependency),
    ) -> dict[str, Any]:
        try:
            _require(context, "plan:edit")
            document = PlanDocumentV1.from_mapping(body.document)
            async with connection_provider() as conn:
                result = await write_repo.save_draft(
                    conn,
                    owner_user_id=context.owner_user_id,
                    revision_id=revision_id,
                    document=document,
                    idempotency_key=idempotency_key,
                    actor_user_id=context.actor_user_id,
                    request_id=_request_id(request),
                )
            return _revision_record(result)
        except PlanDomainError as error:
            raise _domain_http_error(error) from error
        except asyncpg.PostgresError as error:
            raise _internal_http_error() from error

    @router.post("/revisions/{revision_id}/propose")
    async def propose_revision(
        revision_id: uuid.UUID,
        request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        context: ActorContext = Depends(actor_dependency),
    ) -> dict[str, Any]:
        try:
            _require(context, "plan:edit")
            async with connection_provider() as conn:
                result = await write_repo.propose_revision(
                    conn,
                    owner_user_id=context.owner_user_id,
                    revision_id=revision_id,
                    idempotency_key=idempotency_key,
                    actor_user_id=context.actor_user_id,
                    request_id=_request_id(request),
                )
            return _proposal_result(result)
        except PlanDomainError as error:
            raise _domain_http_error(error) from error
        except asyncpg.PostgresError as error:
            raise _internal_http_error() from error

    @router.post("/revisions/{revision_id}/approve-and-activate")
    async def approve_and_activate(
        revision_id: uuid.UUID,
        request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        context: ActorContext = Depends(actor_dependency),
    ) -> dict[str, Any]:
        try:
            if not context.is_owner:
                raise PlanDomainError(
                    "owner_approval_required",
                    "only the owner can approve activation",
                )
            async with connection_provider() as conn:
                result = await write_repo.approve_and_activate(
                    conn,
                    owner_user_id=context.owner_user_id,
                    revision_id=revision_id,
                    approving_actor_user_id=context.actor_user_id,
                    owner_timezone=context.owner_timezone,
                    idempotency_key=idempotency_key,
                    request_id=_request_id(request),
                )
            return _activation_result(result)
        except PlanDomainError as error:
            raise _domain_http_error(error) from error
        except asyncpg.PostgresError as error:
            raise _internal_http_error() from error

    return router
