from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

import asyncpg

from .plan_domain import PlanDomainError


SCHEMA = "lifeswitch_agentic"


def _json_object(value: Any, *, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise PlanDomainError("invalid_stored_json", f"{field} must be a JSON object")
    return dict(value)


@dataclass(frozen=True, slots=True)
class ActivePlanView:
    plan_version_id: uuid.UUID
    version_number: int
    status: str
    owner_timezone: str
    document: Mapping[str, Any]
    activated_at: dt.datetime


@dataclass(frozen=True, slots=True)
class PlanVersionSummary:
    plan_version_id: uuid.UUID
    version_number: int
    status: str
    phase_code: str | None
    goal_summary: str | None
    activated_at: dt.datetime
    superseded_at: dt.datetime | None


@dataclass(frozen=True, slots=True)
class PlanVersionView:
    plan_version_id: uuid.UUID
    version_number: int
    status: str
    owner_timezone: str
    document: Mapping[str, Any]
    document_sha256: str
    source_revision_id: uuid.UUID | None
    activated_at: dt.datetime
    superseded_at: dt.datetime | None


@dataclass(frozen=True, slots=True)
class RevisionChangeView:
    field_path: str
    old_present: bool
    old_value: Any
    new_present: bool
    new_value: Any
    rationale: str | None


@dataclass(frozen=True, slots=True)
class RevisionReviewView:
    revision_id: uuid.UUID
    state: str
    base_plan_version_id: uuid.UUID | None
    current_active_plan_version_id: uuid.UUID | None
    base_is_current: bool
    author_type: str
    trigger_type: str
    proposed_document: Mapping[str, Any]
    proposed_document_sha256: str
    validation_result: Mapping[str, Any] | None
    changes: tuple[RevisionChangeView, ...]
    proposed_at: dt.datetime | None
    resolved_at: dt.datetime | None
    activated_plan_version_id: uuid.UUID | None
    source: Mapping[str, Any] | None


class PlanReadRepository:
    async def get_active_plan(
        self,
        conn: asyncpg.Connection,
        *,
        owner_user_id: uuid.UUID,
    ) -> ActivePlanView | None:
        row = await conn.fetchrow(
            f"""
            select version.id, version.version_number, version.status,
                   version.owner_timezone, version.document,
                   version.activated_at
            from {SCHEMA}.plan_owner_state state
            join {SCHEMA}.plan_versions version
              on version.owner_user_id = state.owner_user_id
             and version.id = state.active_plan_version_id
            where state.owner_user_id = $1
            """,
            owner_user_id,
        )
        if row is None:
            return None
        return ActivePlanView(
            plan_version_id=row["id"],
            version_number=int(row["version_number"]),
            status=row["status"],
            owner_timezone=row["owner_timezone"],
            document=_json_object(row["document"], field="plan document"),
            activated_at=row["activated_at"],
        )

    async def list_plan_versions(
        self,
        conn: asyncpg.Connection,
        *,
        owner_user_id: uuid.UUID,
        limit: int,
        before_version: int | None = None,
    ) -> tuple[PlanVersionSummary, ...]:
        if not 1 <= limit <= 50:
            raise PlanDomainError("invalid_input", "history limit must be between 1 and 50")
        if before_version is not None and before_version < 1:
            raise PlanDomainError("invalid_input", "before_version must be positive")
        rows = await conn.fetch(
            f"""
            select id, version_number, status, phase_code,
                   goal_summary, activated_at, superseded_at
            from {SCHEMA}.plan_versions
            where owner_user_id = $1
              and ($2::bigint is null or version_number < $2)
            order by version_number desc
            limit $3
            """,
            owner_user_id,
            before_version,
            limit + 1,
        )
        return tuple(
            PlanVersionSummary(
                plan_version_id=row["id"],
                version_number=int(row["version_number"]),
                status=row["status"],
                phase_code=row["phase_code"],
                goal_summary=row["goal_summary"],
                activated_at=row["activated_at"],
                superseded_at=row["superseded_at"],
            )
            for row in rows
        )

    async def get_plan_version(
        self,
        conn: asyncpg.Connection,
        *,
        owner_user_id: uuid.UUID,
        plan_version_id: uuid.UUID,
    ) -> PlanVersionView | None:
        row = await conn.fetchrow(
            f"""
            select id, version_number, status, owner_timezone,
                   document, document_sha256, source_revision_id,
                   activated_at, superseded_at
            from {SCHEMA}.plan_versions
            where owner_user_id = $1 and id = $2
            """,
            owner_user_id,
            plan_version_id,
        )
        if row is None:
            return None
        return PlanVersionView(
            plan_version_id=row["id"],
            version_number=int(row["version_number"]),
            status=row["status"],
            owner_timezone=row["owner_timezone"],
            document=_json_object(row["document"], field="plan document"),
            document_sha256=row["document_sha256"],
            source_revision_id=row["source_revision_id"],
            activated_at=row["activated_at"],
            superseded_at=row["superseded_at"],
        )

    async def get_revision_review(
        self,
        conn: asyncpg.Connection,
        *,
        owner_user_id: uuid.UUID,
        revision_id: uuid.UUID,
    ) -> RevisionReviewView | None:
        row = await conn.fetchrow(
            f"""
            select revision.id, revision.state,
                   revision.base_plan_version_id,
                   owner_state.active_plan_version_id,
                   revision.author_type, revision.trigger_type,
                   revision.proposed_document,
                   revision.proposed_document_sha256,
                   revision.validation_result,
                   revision.proposed_at, revision.resolved_at,
                   revision.activated_plan_version_id,
                   (
                     select event.detail
                     from {SCHEMA}.plan_revision_events event
                     where event.owner_user_id = revision.owner_user_id
                       and event.plan_revision_id = revision.id
                       and event.event_type in (
                         'plan_revision_legacy_adopted',
                         'plan_revision_legacy_refreshed'
                       )
                     order by event.id desc
                     limit 1
                   ) as legacy_source
            from {SCHEMA}.plan_revisions revision
            left join {SCHEMA}.plan_owner_state owner_state
              on owner_state.owner_user_id = revision.owner_user_id
            where revision.owner_user_id = $1 and revision.id = $2
            """,
            owner_user_id,
            revision_id,
        )
        if row is None:
            return None
        change_rows = await conn.fetch(
            f"""
            select field_path, old_present, old_value,
                   new_present, new_value, rationale
            from {SCHEMA}.plan_revision_changes
            where plan_revision_id = $1
            order by field_path
            """,
            revision_id,
        )
        base_plan_version_id = row["base_plan_version_id"]
        active_plan_version_id = row["active_plan_version_id"]
        return RevisionReviewView(
            revision_id=row["id"],
            state=row["state"],
            base_plan_version_id=base_plan_version_id,
            current_active_plan_version_id=active_plan_version_id,
            base_is_current=(
                base_plan_version_id == active_plan_version_id
                if base_plan_version_id is not None
                else active_plan_version_id is None
            ),
            author_type=row["author_type"],
            trigger_type=row["trigger_type"],
            proposed_document=_json_object(
                row["proposed_document"],
                field="proposed document",
            ),
            proposed_document_sha256=row["proposed_document_sha256"],
            validation_result=(
                _json_object(row["validation_result"], field="validation result")
                if row["validation_result"] is not None
                else None
            ),
            changes=tuple(
                RevisionChangeView(
                    field_path=change["field_path"],
                    old_present=bool(change["old_present"]),
                    old_value=(
                        json.loads(change["old_value"])
                        if isinstance(change["old_value"], str)
                        else change["old_value"]
                    ),
                    new_present=bool(change["new_present"]),
                    new_value=(
                        json.loads(change["new_value"])
                        if isinstance(change["new_value"], str)
                        else change["new_value"]
                    ),
                    rationale=change["rationale"],
                )
                for change in change_rows
            ),
            proposed_at=row["proposed_at"],
            resolved_at=row["resolved_at"],
            activated_plan_version_id=row["activated_plan_version_id"],
            source=(
                _json_object(row["legacy_source"], field="revision source")
                if row["legacy_source"] is not None
                else None
            ),
        )

    async def get_latest_open_revision(
        self,
        conn: asyncpg.Connection,
        *,
        owner_user_id: uuid.UUID,
    ) -> RevisionReviewView | None:
        revision_id = await conn.fetchval(
            f"""
            select id
            from {SCHEMA}.plan_revisions
            where owner_user_id = $1
              and state in ('draft', 'proposed', 'needs_changes')
            order by created_at desc, id desc
            limit 1
            """,
            owner_user_id,
        )
        if revision_id is None:
            return None
        return await self.get_revision_review(
            conn,
            owner_user_id=owner_user_id,
            revision_id=revision_id,
        )
