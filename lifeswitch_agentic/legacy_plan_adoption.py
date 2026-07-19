from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

import asyncpg

from .plan_domain import PlanDocumentV1, PlanDomainError, RevisionState, RevisionTrigger
from .plan_repository import PlanRepository, RevisionRecord


AGENTIC_SCHEMA = "lifeswitch_agentic"
_SQL_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
_LEGACY_JSON_FIELDS = (
    "body_state",
    "nutrition_targets",
    "training_targets",
    "conditioning_targets",
    "activity_targets",
    "recovery_targets",
    "monitoring_rules",
)


def _identifier(value: str, *, field: str) -> str:
    cleaned = str(value or "").strip()
    if not _SQL_IDENTIFIER.fullmatch(cleaned):
        raise ValueError(f"{field} must be a simple PostgreSQL identifier")
    return cleaned


def _json_object(value: Any, *, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise PlanDomainError(
                "invalid_stored_json",
                f"{field} must contain valid JSON",
            ) from error
    if not isinstance(value, Mapping):
        raise PlanDomainError("invalid_stored_json", f"{field} must be a JSON object")
    return dict(value)


def _event_detail(value: Any) -> dict[str, Any]:
    return _json_object(value, field="legacy adoption event detail")


@dataclass(frozen=True, slots=True)
class LegacyPlanAdoptionResult:
    revision: RevisionRecord
    source_kind: str
    legacy_plan_profile_id: uuid.UUID
    legacy_profile_updated_at: dt.datetime
    created: bool
    refreshed: bool = False


@dataclass(frozen=True, slots=True)
class _LegacyProfileSnapshot:
    plan_profile_id: uuid.UUID
    updated_at: dt.datetime
    document: PlanDocumentV1


class LegacyPlanAdoptionService:
    def __init__(
        self,
        *,
        plan_repository: PlanRepository | None = None,
        legacy_schema: str = "lifeswitch_plan",
    ) -> None:
        self._plan_repository = plan_repository or PlanRepository()
        self._legacy_schema = _identifier(legacy_schema, field="legacy_schema")

    async def _load_current_legacy_profile(
        self,
        conn: asyncpg.Connection,
        *,
        owner_user_id: uuid.UUID,
    ) -> _LegacyProfileSnapshot:
        legacy = await conn.fetchrow(
            f"""
            select plan_profile_id, phase, phase_label, primary_goal,
                   start_date, review_date, review_cadence,
                   body_state, nutrition_targets, training_targets,
                   conditioning_targets, activity_targets,
                   recovery_targets, monitoring_rules, coach_notes,
                   updated_at
            from {self._legacy_schema}.plan_profile
            where owner_user_id = $1 and is_active = true
            for key share
            """,
            owner_user_id,
        )
        if legacy is None:
            raise PlanDomainError(
                "legacy_plan_unavailable",
                "current legacy plan profile is unavailable",
            )
        legacy_mapping = dict(legacy)
        for field in _LEGACY_JSON_FIELDS:
            legacy_mapping[field] = _json_object(
                legacy_mapping[field],
                field=f"legacy {field}",
            )
        return _LegacyProfileSnapshot(
            plan_profile_id=legacy["plan_profile_id"],
            updated_at=legacy["updated_at"],
            document=PlanDocumentV1.from_legacy_profile(legacy_mapping),
        )

    async def _existing_adoption(
        self,
        conn: asyncpg.Connection,
        *,
        owner_user_id: uuid.UUID,
    ) -> LegacyPlanAdoptionResult | None:
        row = await conn.fetchrow(
            f"""
            select revision.id, revision.base_plan_version_id,
                   revision.state, revision.proposed_document_sha256,
                   event.detail
            from {AGENTIC_SCHEMA}.plan_revision_events event
            join {AGENTIC_SCHEMA}.plan_revisions revision
              on revision.owner_user_id = event.owner_user_id
             and revision.id = event.plan_revision_id
            where event.owner_user_id = $1
              and event.event_type = 'plan_revision_legacy_adopted'
            limit 1
            """,
            owner_user_id,
        )
        if row is None:
            return None
        detail = _event_detail(row["detail"])
        try:
            source_kind = str(detail["source_kind"])
            profile_id = uuid.UUID(str(detail["legacy_plan_profile_id"]))
            profile_updated_at = dt.datetime.fromisoformat(
                str(detail["legacy_profile_updated_at"])
            )
        except (KeyError, TypeError, ValueError) as error:
            raise PlanDomainError(
                "invalid_stored_json",
                "legacy adoption provenance is invalid",
            ) from error
        return LegacyPlanAdoptionResult(
            revision=RevisionRecord(
                revision_id=row["id"],
                owner_user_id=owner_user_id,
                base_plan_version_id=row["base_plan_version_id"],
                state=RevisionState(row["state"]),
                document_sha256=row["proposed_document_sha256"],
            ),
            source_kind=source_kind,
            legacy_plan_profile_id=profile_id,
            legacy_profile_updated_at=profile_updated_at,
            created=False,
        )

    async def adopt_current_profile_as_draft(
        self,
        conn: asyncpg.Connection,
        *,
        owner_user_id: uuid.UUID,
        idempotency_key: str,
        request_id: str | None = None,
    ) -> LegacyPlanAdoptionResult:
        async with conn.transaction():
            await conn.execute(
                f"""
                insert into {AGENTIC_SCHEMA}.plan_owner_state (owner_user_id)
                values ($1)
                on conflict (owner_user_id) do nothing
                """,
                owner_user_id,
            )
            owner_state = await conn.fetchrow(
                f"""
                select active_plan_version_id
                from {AGENTIC_SCHEMA}.plan_owner_state
                where owner_user_id = $1
                for update
                """,
                owner_user_id,
            )
            if owner_state is None:
                raise RuntimeError("plan owner state was not created")

            existing = await self._existing_adoption(
                conn,
                owner_user_id=owner_user_id,
            )
            if existing is not None:
                return existing
            if owner_state["active_plan_version_id"] is not None:
                raise PlanDomainError(
                    "state_conflict",
                    "an active agentic plan already exists",
                )

            legacy = await self._load_current_legacy_profile(
                conn,
                owner_user_id=owner_user_id,
            )
            revision = await self._plan_repository.create_draft(
                conn,
                owner_user_id=owner_user_id,
                document=legacy.document,
                trigger=RevisionTrigger.INITIAL_PLAN,
                base_plan_version_id=None,
                author_type="owner",
                idempotency_key=idempotency_key,
                author_actor_user_id=owner_user_id,
                request_id=request_id,
            )
            detail = {
                "source_kind": f"{self._legacy_schema}.plan_profile",
                "legacy_plan_profile_id": str(legacy.plan_profile_id),
                "legacy_profile_updated_at": legacy.updated_at.isoformat(),
                "adopted_document_sha256": legacy.document.sha256(),
            }
            await conn.execute(
                f"""
                insert into {AGENTIC_SCHEMA}.plan_revision_events (
                  plan_revision_id, owner_user_id, event_type,
                  actor_user_id, prior_state, new_state,
                  reason_code, detail, request_id
                ) values (
                  $1, $2, 'plan_revision_legacy_adopted',
                  $2, 'draft', 'draft',
                  'owner_requested_adoption', $3::jsonb, $4
                )
                """,
                revision.revision_id,
                owner_user_id,
                json.dumps(detail, sort_keys=True, separators=(",", ":")),
                request_id,
            )
            return LegacyPlanAdoptionResult(
                revision=revision,
                source_kind=detail["source_kind"],
                legacy_plan_profile_id=legacy.plan_profile_id,
                legacy_profile_updated_at=legacy.updated_at,
                created=True,
            )

    async def refresh_adopted_draft_from_current_profile(
        self,
        conn: asyncpg.Connection,
        *,
        owner_user_id: uuid.UUID,
        idempotency_key: str,
        request_id: str | None = None,
    ) -> LegacyPlanAdoptionResult:
        async with conn.transaction():
            owner_state = await conn.fetchrow(
                f"""
                select active_plan_version_id
                from {AGENTIC_SCHEMA}.plan_owner_state
                where owner_user_id = $1
                for update
                """,
                owner_user_id,
            )
            if owner_state is None:
                raise PlanDomainError(
                    "legacy_adoption_unavailable",
                    "legacy plan adoption has not been started",
                )
            existing = await self._existing_adoption(
                conn,
                owner_user_id=owner_user_id,
            )
            if existing is None:
                raise PlanDomainError(
                    "legacy_adoption_unavailable",
                    "legacy plan adoption has not been started",
                )
            if existing.revision.state is not RevisionState.DRAFT:
                raise PlanDomainError(
                    "state_conflict",
                    "only an adoption draft can be refreshed",
                )
            if owner_state["active_plan_version_id"] is not None:
                raise PlanDomainError(
                    "state_conflict",
                    "the adopted plan has already been activated",
                )

            legacy = await self._load_current_legacy_profile(
                conn,
                owner_user_id=owner_user_id,
            )
            revision = await self._plan_repository.save_draft(
                conn,
                owner_user_id=owner_user_id,
                revision_id=existing.revision.revision_id,
                document=legacy.document,
                idempotency_key=idempotency_key,
                actor_user_id=owner_user_id,
                request_id=request_id,
            )
            idempotency_key_sha256 = hashlib.sha256(
                idempotency_key.strip().encode("utf-8")
            ).hexdigest()
            detail = {
                "source_kind": f"{self._legacy_schema}.plan_profile",
                "legacy_plan_profile_id": str(legacy.plan_profile_id),
                "legacy_profile_updated_at": legacy.updated_at.isoformat(),
                "refreshed_document_sha256": legacy.document.sha256(),
                "idempotency_key_sha256": idempotency_key_sha256,
            }
            await conn.execute(
                f"""
                insert into {AGENTIC_SCHEMA}.plan_revision_events (
                  plan_revision_id, owner_user_id, event_type,
                  actor_user_id, prior_state, new_state,
                  reason_code, detail, request_id
                )
                select
                  $1, $2, 'plan_revision_legacy_refreshed',
                  $2, 'draft', 'draft',
                  'owner_requested_refresh', $3::jsonb, $4
                where not exists (
                  select 1
                  from {AGENTIC_SCHEMA}.plan_revision_events
                  where owner_user_id = $2
                    and plan_revision_id = $1
                    and event_type = 'plan_revision_legacy_refreshed'
                    and detail->>'idempotency_key_sha256' = $5
                )
                """,
                revision.revision_id,
                owner_user_id,
                json.dumps(detail, sort_keys=True, separators=(",", ":")),
                request_id,
                idempotency_key_sha256,
            )
            return LegacyPlanAdoptionResult(
                revision=revision,
                source_kind=detail["source_kind"],
                legacy_plan_profile_id=legacy.plan_profile_id,
                legacy_profile_updated_at=legacy.updated_at,
                created=False,
                refreshed=True,
            )
