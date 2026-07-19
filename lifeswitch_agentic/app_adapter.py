from __future__ import annotations

import re
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg
from fastapi import APIRouter, HTTPException, Request

from .legacy_plan_adoption import LegacyPlanAdoptionService
from .plan_api import ActorContext, create_plan_router
from .plan_recommendations import (
    OpenAIPlanRecommendationProvider,
    PlanRecommendationService,
)
from .plan_repository import PlanRepository


PLAN_PERMISSION_SCOPES = frozenset({"plan:view", "plan:comment", "plan:edit"})
_SQL_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


def _identifier(value: str, *, field: str) -> str:
    cleaned = str(value or "").strip()
    if not _SQL_IDENTIFIER.fullmatch(cleaned):
        raise ValueError(f"{field} must be a simple PostgreSQL identifier")
    return cleaned


def _uuid(value: str, *, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise HTTPException(
            status_code=400,
            detail={"code": f"invalid_{field}", "message": f"{field} must be a UUID"},
        ) from error


def _timezone(value: str | None) -> str:
    cleaned = str(value or "UTC").strip() or "UTC"
    if len(cleaned) > 80:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_owner_timezone", "message": "owner timezone is invalid"},
        )
    try:
        ZoneInfo(cleaned)
    except ZoneInfoNotFoundError as error:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_owner_timezone", "message": "owner timezone is invalid"},
        ) from error
    return cleaned


class LifeSwitchPlanAppAdapter:
    def __init__(
        self,
        *,
        dsn: str,
        people_schema: str = "lifeswitch_people",
    ) -> None:
        self._dsn = str(dsn or "").strip()
        if not self._dsn:
            raise ValueError("PostgreSQL DSN is required")
        self._people_schema = _identifier(people_schema, field="people_schema")

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[asyncpg.Connection]:
        conn = await asyncpg.connect(self._dsn)
        try:
            yield conn
        finally:
            await conn.close()

    async def _delegated_scopes(
        self,
        *,
        owner_user_id: uuid.UUID,
        actor_user_id: uuid.UUID,
    ) -> frozenset[str]:
        async with self.connection() as conn:
            rows = await conn.fetch(
                f"""
                select distinct permission.permission_scope
                from {self._people_schema}.relationship_permission permission
                join {self._people_schema}.relationship relationship
                  on relationship.relationship_id = permission.relationship_id
                where permission.grantor_user_id = $1
                  and permission.grantee_user_id = $2
                  and permission.permission_scope = any($3::text[])
                  and permission.is_enabled = true
                  and permission.permission_level <> 'none'
                  and relationship.status = 'accepted'
                  and (
                    (relationship.requester_user_id = $1 and relationship.addressee_user_id = $2)
                    or
                    (relationship.requester_user_id = $2 and relationship.addressee_user_id = $1)
                  )
                """,
                owner_user_id,
                actor_user_id,
                sorted(PLAN_PERMISSION_SCOPES),
            )
        scopes = {str(row["permission_scope"]) for row in rows}
        if "plan:edit" in scopes:
            scopes.update({"plan:comment", "plan:view"})
        elif "plan:comment" in scopes:
            scopes.add("plan:view")
        return frozenset(scopes)

    async def actor_context(self, request: Request) -> ActorContext:
        raw_actor = str(request.headers.get("x-vs-actor-user-id") or "").strip()
        if not raw_actor:
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "missing_actor_user_id",
                    "message": "authenticated actor is required",
                },
            )
        actor_user_id = _uuid(raw_actor, field="actor_user_id")
        raw_target = str(request.query_params.get("target_user_id") or "").strip()
        owner_user_id = (
            _uuid(raw_target, field="target_user_id") if raw_target else actor_user_id
        )

        if owner_user_id == actor_user_id:
            scopes = frozenset()
            owner_timezone = _timezone(request.headers.get("x-vs-owner-timezone"))
        else:
            scopes = await self._delegated_scopes(
                owner_user_id=owner_user_id,
                actor_user_id=actor_user_id,
            )
            # Delegated actors cannot activate plans. Their browser timezone is
            # not authoritative for the owner and is never persisted.
            owner_timezone = "UTC"

        return ActorContext(
            actor_user_id=actor_user_id,
            owner_user_id=owner_user_id,
            owner_timezone=owner_timezone,
            permission_scopes=scopes,
        )


def create_lifeswitch_plan_app_router(
    *,
    dsn: str,
    people_schema: str = "lifeswitch_people",
    legacy_plan_schema: str = "lifeswitch_plan",
    openai_api_key: str | None = None,
    plan_recommendation_model: str = "gpt-5.2",
) -> APIRouter:
    adapter = LifeSwitchPlanAppAdapter(dsn=dsn, people_schema=people_schema)
    plan_repository = PlanRepository()
    recommendation_service = None
    if str(openai_api_key or "").strip():
        recommendation_service = PlanRecommendationService(
            OpenAIPlanRecommendationProvider(
                api_key=str(openai_api_key),
                model=plan_recommendation_model,
            )
        )
    return create_plan_router(
        connection_provider=adapter.connection,
        actor_dependency=adapter.actor_context,
        plan_repository=plan_repository,
        recommendation_service=recommendation_service,
        legacy_adoption_service=LegacyPlanAdoptionService(
            plan_repository=plan_repository,
            legacy_schema=legacy_plan_schema,
        ),
    )
