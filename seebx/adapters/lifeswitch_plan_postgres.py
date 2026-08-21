from __future__ import annotations

"""PostgreSQL effect boundary for the canonical LifeSwitch Plan."""

import datetime as dt
import decimal
import json
import os
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from starlette.requests import Request

from seebx.adapters.lifeswitch_postgres import connect_lifeswitch


ConnectionFactory = Callable[[Request], Awaitable[Any]]
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")

JSON_FIELDS = {
    "body_state",
    "nutrition_targets",
    "training_targets",
    "conditioning_targets",
    "activity_targets",
    "recovery_targets",
    "monitoring_rules",
    "snapshot",
}


def resolve_plan_schema(value: str | None = None) -> str:
    schema = (
        os.getenv("LIFESWITCH_PLAN_SCHEMA", "lifeswitch_plan")
        if value is None
        else value
    ).strip()
    if _IDENTIFIER.fullmatch(schema) is None:
        raise RuntimeError("invalid LIFESWITCH_PLAN_SCHEMA")
    return schema


def plan_row_to_jsonable(row: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in dict(row).items():
        if isinstance(value, uuid.UUID):
            value = str(value)
        elif isinstance(value, decimal.Decimal):
            value = float(value)
        elif isinstance(value, (dt.datetime, dt.date)):
            value = value.isoformat()
        if key in JSON_FIELDS and isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                pass
        result[key] = value
    return result


def _json_object(value: Any) -> str:
    if value is None:
        value = {}
    if not isinstance(value, (dict, list)):
        raise ValueError("JSON sections must be objects or arrays")
    return json.dumps(value)


@dataclass(frozen=True)
class PlanProfileWrite:
    owner_user_id: str
    phase: str
    phase_label: str
    primary_goal: str
    start_date: dt.date | None
    review_date: dt.date | None
    review_cadence: str
    body_state: dict[str, Any]
    nutrition_targets: dict[str, Any]
    training_targets: dict[str, Any]
    conditioning_targets: dict[str, Any]
    activity_targets: dict[str, Any]
    recovery_targets: dict[str, Any]
    monitoring_rules: dict[str, Any]
    coach_notes: str


class PostgresLifeSwitchPlanRepository:
    """Own every query and transaction for the canonical Plan capability."""

    def __init__(self, connection: Any, *, plan_schema: str) -> None:
        self._connection = connection
        self._plan_schema = resolve_plan_schema(plan_schema)

    async def fetch_active_profile(self, *, owner_user_id: str) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            select
              plan_profile_id, owner_user_id,
              phase, phase_label, primary_goal,
              start_date, review_date, review_cadence,
              body_state, nutrition_targets, training_targets,
              conditioning_targets, activity_targets, recovery_targets,
              monitoring_rules, coach_notes,
              is_active, created_at, updated_at
            from {self._plan_schema}.plan_profile
            where owner_user_id=$1::uuid
              and is_active=true
            limit 1
            """,
            owner_user_id,
        )

    async def get_or_create_profile(
        self,
        *,
        owner_user_id: str,
        create_if_missing: bool,
    ) -> Any | None:
        row = await self.fetch_active_profile(owner_user_id=owner_user_id)
        if row is not None or not create_if_missing:
            return row
        return await self._connection.fetchrow(
            f"""
            insert into {self._plan_schema}.plan_profile (owner_user_id)
            values ($1::uuid)
            on conflict (owner_user_id) do update
              set is_active=true,
                  updated_at=now()
            returning
              plan_profile_id, owner_user_id,
              phase, phase_label, primary_goal,
              start_date, review_date, review_cadence,
              body_state, nutrition_targets, training_targets,
              conditioning_targets, activity_targets, recovery_targets,
              monitoring_rules, coach_notes,
              is_active, created_at, updated_at
            """,
            owner_user_id,
        )

    async def upsert_profile(
        self,
        value: PlanProfileWrite,
        *,
        snapshot_reason: str,
    ) -> Any | None:
        async with self._connection.transaction():
            existing = await self.fetch_active_profile(
                owner_user_id=value.owner_user_id
            )
            if existing is not None:
                await self._connection.execute(
                    f"""
                    insert into {self._plan_schema}.plan_profile_history
                      (plan_profile_id, owner_user_id, snapshot_reason, snapshot)
                    values ($1::uuid, $2::uuid, $3, $4::jsonb)
                    """,
                    str(existing["plan_profile_id"]),
                    value.owner_user_id,
                    snapshot_reason,
                    json.dumps(plan_row_to_jsonable(existing)),
                )

            return await self._connection.fetchrow(
                f"""
                insert into {self._plan_schema}.plan_profile (
                  owner_user_id,
                  phase, phase_label, primary_goal,
                  start_date, review_date, review_cadence,
                  body_state, nutrition_targets, training_targets,
                  conditioning_targets, activity_targets, recovery_targets,
                  monitoring_rules, coach_notes, is_active
                )
                values (
                  $1::uuid, $2, $3, $4, $5::date, $6::date, $7,
                  $8::jsonb, $9::jsonb, $10::jsonb, $11::jsonb,
                  $12::jsonb, $13::jsonb, $14::jsonb, $15, true
                )
                on conflict (owner_user_id) do update
                  set phase=excluded.phase,
                      phase_label=excluded.phase_label,
                      primary_goal=excluded.primary_goal,
                      start_date=excluded.start_date,
                      review_date=excluded.review_date,
                      review_cadence=excluded.review_cadence,
                      body_state=excluded.body_state,
                      nutrition_targets=excluded.nutrition_targets,
                      training_targets=excluded.training_targets,
                      conditioning_targets=excluded.conditioning_targets,
                      activity_targets=excluded.activity_targets,
                      recovery_targets=excluded.recovery_targets,
                      monitoring_rules=excluded.monitoring_rules,
                      coach_notes=excluded.coach_notes,
                      is_active=true,
                      updated_at=now()
                returning
                  plan_profile_id, owner_user_id,
                  phase, phase_label, primary_goal,
                  start_date, review_date, review_cadence,
                  body_state, nutrition_targets, training_targets,
                  conditioning_targets, activity_targets, recovery_targets,
                  monitoring_rules, coach_notes,
                  is_active, created_at, updated_at
                """,
                value.owner_user_id,
                value.phase,
                value.phase_label,
                value.primary_goal,
                value.start_date,
                value.review_date,
                value.review_cadence,
                _json_object(value.body_state),
                _json_object(value.nutrition_targets),
                _json_object(value.training_targets),
                _json_object(value.conditioning_targets),
                _json_object(value.activity_targets),
                _json_object(value.recovery_targets),
                _json_object(value.monitoring_rules),
                value.coach_notes,
            )

    async def list_comments(
        self,
        *,
        owner_user_id: str,
        limit: int,
    ) -> list[Any]:
        profile = await self.fetch_active_profile(owner_user_id=owner_user_id)
        if profile is None:
            return []
        rows = await self._connection.fetch(
            f"""
            select
              c.plan_comment_id, c.plan_profile_id, c.target_user_id,
              c.author_user_id,
              c.author_user_id::text as author_display_name,
              c.comment_text, c.comment_kind, c.is_active,
              c.resolved_at, c.created_at, c.updated_at
            from {self._plan_schema}.plan_comment c
            where c.plan_profile_id=$1::uuid
              and c.target_user_id=$2::uuid
              and c.is_active=true
            order by c.created_at desc
            limit $3
            """,
            str(profile["plan_profile_id"]),
            owner_user_id,
            limit,
        )
        return list(rows)

    async def create_comment(
        self,
        *,
        owner_user_id: str,
        author_user_id: str,
        comment_text: str,
        comment_kind: str,
    ) -> Any | None:
        profile = await self.fetch_active_profile(owner_user_id=owner_user_id)
        if profile is None:
            return None
        return await self._connection.fetchrow(
            f"""
            insert into {self._plan_schema}.plan_comment
              (plan_profile_id, target_user_id, author_user_id,
               comment_text, comment_kind)
            values ($1::uuid, $2::uuid, $3::uuid, $4, $5)
            returning
              plan_comment_id, plan_profile_id, target_user_id,
              author_user_id, comment_text, comment_kind,
              is_active, resolved_at, created_at, updated_at
            """,
            str(profile["plan_profile_id"]),
            owner_user_id,
            author_user_id,
            comment_text,
            comment_kind,
        )

    async def list_history(
        self,
        *,
        owner_user_id: str,
        limit: int,
    ) -> list[Any]:
        rows = await self._connection.fetch(
            f"""
            select
              plan_profile_history_id, plan_profile_id, owner_user_id,
              snapshot_reason, snapshot, created_at
            from {self._plan_schema}.plan_profile_history
            where owner_user_id=$1::uuid
            order by created_at desc
            limit $2
            """,
            owner_user_id,
            limit,
        )
        return list(rows)


@asynccontextmanager
async def lifeswitch_plan_repository(
    request: Request,
    *,
    connection_factory: ConnectionFactory = connect_lifeswitch,
) -> AsyncIterator[PostgresLifeSwitchPlanRepository]:
    connection = await connection_factory(request)
    try:
        yield PostgresLifeSwitchPlanRepository(
            connection,
            plan_schema=resolve_plan_schema(),
        )
    finally:
        await connection.close()


__all__ = [
    "PlanProfileWrite",
    "PostgresLifeSwitchPlanRepository",
    "lifeswitch_plan_repository",
    "plan_row_to_jsonable",
    "resolve_plan_schema",
]
