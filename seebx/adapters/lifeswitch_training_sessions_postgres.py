from __future__ import annotations

"""PostgreSQL effect boundary for LifeSwitch strength sessions and set logs."""

import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from starlette.requests import Request

from seebx.adapters.lifeswitch_postgres import connect_lifeswitch
from seebx.adapters.lifeswitch_training_writes_postgres import (
    correct_training_session as write_training_correction,
    create_training_session as write_training_session,
    set_transaction_actor,
    void_training_session as write_training_void,
)


ConnectionFactory = Callable[[Request], Awaitable[Any]]
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")


def resolve_training_schema(value: str | None = None) -> str:
    candidate = str(
        os.getenv("LIFESWITCH_TRAINING_SCHEMA", "lifeswitch_training")
        if value is None
        else value
    ).strip()
    if _IDENTIFIER.fullmatch(candidate) is None:
        raise RuntimeError("invalid LIFESWITCH_TRAINING_SCHEMA")
    return candidate


class PostgresLifeSwitchTrainingSessionsRepository:
    """Own all eight retained strength-session SQL effects and three transactions."""

    def __init__(self, connection: Any, *, training_schema: str) -> None:
        self._connection = connection
        self._schema = resolve_training_schema(training_schema)

    async def complete_session(
        self,
        *,
        owner: str,
        intent: dict[str, Any],
        idempotency_key: str,
    ) -> Any:
        async with self._connection.transaction():
            await set_transaction_actor(self._connection, actor_user_id=owner)
            session_id = await write_training_session(
                self._connection,
                intent=intent,
                idempotency_key=idempotency_key,
            )
            return await self._connection.fetchrow(
                f"""
                select
                  training_session_id, owner_user_id, day, workout_template_id,
                  name, notes, started_at, finished_at, is_active,
                  created_at, updated_at
                from {self._schema}.training_session
                where training_session_id=$1::uuid
                  and owner_user_id=$2::uuid
                """,
                session_id,
                owner,
            )

    async def list_sessions(
        self,
        *,
        owner: str,
        delegated: bool,
        day_val: Any | None,
        include_inactive: bool,
        limit: int,
    ) -> tuple[Any, ...]:
        session_source = (
            "training_session"
            if include_inactive
            else "training_session_current_v"
        )
        where = ["s.owner_user_id=$1::uuid"]
        args = [owner, delegated]
        if day_val:
            args.append(day_val)
            where.append(f"s.day=${len(args)}::date")
        if not include_inactive:
            where.append("s.is_active=true")
            where.append("s.finished_at is not null")
        having = (
            "having count(l.training_set_log_id) filter (where l.is_active=true) > 0"
            if not include_inactive
            else ""
        )
        rows = await self._connection.fetch(
            f"""
            with session_rollup as (
              select
                s.training_session_id, s.owner_user_id, s.day, s.workout_template_id,
                s.name, s.notes, s.started_at, s.finished_at, s.is_active,
                s.created_at, s.updated_at,
                base.workout_role_snapshot,
                role_event.assigned_role as historical_workout_role,
                coalesce(count(l.training_set_log_id) filter (where l.is_active=true), 0)::int as set_count,
                coalesce(count(distinct l.exercise_id) filter (where l.is_active=true), 0)::int as exercise_count,
                coalesce(sum(l.volume) filter (where l.is_active=true), 0)::float as volume,
                coalesce(count(l.training_set_log_id) filter (
                  where l.is_active=true
                    and role_resolution.effective_role='strength'
                ), 0)::int as strength_set_count,
                coalesce(count(distinct l.exercise_id) filter (
                  where l.is_active=true
                    and role_resolution.effective_role='strength'
                ), 0)::int as strength_exercise_count,
                coalesce(sum(l.volume) filter (
                  where l.is_active=true
                    and role_resolution.effective_role='strength'
                ), 0)::float as strength_volume,
                coalesce(count(l.training_set_log_id) filter (
                  where l.is_active=true
                    and role_resolution.effective_role='rehab'
                ), 0)::int as rehab_set_count,
                coalesce(count(distinct l.exercise_id) filter (
                  where l.is_active=true
                    and role_resolution.effective_role='rehab'
                ), 0)::int as rehab_exercise_count,
                coalesce(sum(l.volume) filter (
                  where l.is_active=true
                    and role_resolution.effective_role='rehab'
                ), 0)::float as rehab_volume,
                coalesce(count(l.training_set_log_id) filter (
                  where l.is_active=true
                    and role_resolution.effective_role='unknown'
                ), 0)::int as unknown_role_set_count,
                coalesce(count(l.training_set_log_id) filter (
                  where l.is_active=true and role_resolution.role_conflict
                ), 0)::int as role_conflict_set_count,
                coalesce(count(l.training_set_log_id) filter (
                  where l.is_active=true
                    and role_resolution.resolution_source='capture_role'
                ), 0)::int as capture_role_resolved_set_count,
                coalesce(count(l.training_set_log_id) filter (
                  where l.is_active=true
                    and role_resolution.resolution_source='exercise_role_snapshot'
                ), 0)::int as exercise_role_snapshot_resolved_set_count,
                coalesce(count(l.training_set_log_id) filter (
                  where l.is_active=true
                    and role_resolution.resolution_source='training_session_role_event'
                ), 0)::int as training_session_role_event_resolved_set_count,
                coalesce(count(l.training_set_log_id) filter (
                  where l.is_active=true
                    and role_resolution.resolution_source='workout_role_snapshot'
                ), 0)::int as workout_role_snapshot_resolved_set_count,
                coalesce(count(l.training_set_log_id) filter (
                  where l.is_active=true
                    and role_resolution.resolution_source='unresolved'
                ), 0)::int as unresolved_role_set_count
              from {self._schema}.{session_source} s
              join {self._schema}.training_session base
                on base.training_session_id=s.training_session_id
               and base.owner_user_id=s.owner_user_id
              left join {self._schema}.training_session_role_event role_event
                on role_event.training_session_id=s.training_session_id
               and role_event.owner_user_id=s.owner_user_id
              left join {self._schema}.training_set_log l
                on l.training_session_id=s.training_session_id
               and l.owner_user_id=s.owner_user_id
              left join {self._schema}.training_set_effective_role_v1 role_resolution
                on role_resolution.training_set_log_id=l.training_set_log_id
               and role_resolution.training_session_id=l.training_session_id
               and role_resolution.owner_user_id=l.owner_user_id
              where {' and '.join(where)}
              group by
                s.training_session_id, s.owner_user_id, s.day,
                s.workout_template_id, s.name, s.notes, s.started_at,
                s.finished_at, s.is_active, s.created_at, s.updated_at,
                base.workout_role_snapshot, role_event.assigned_role
              {having}
            ), classified as (
              select session_rollup.*,
                case
                  when strength_set_count > 0 and rehab_set_count > 0 then 'mixed'
                  when strength_set_count > 0 then 'strength'
                  when rehab_set_count > 0 then 'rehab'
                  else 'unclassified'
                end as session_role
              from session_rollup
            )
            select classified.*,
              $1::uuid as _target_user_id,
              $2::boolean as _delegated_view,
              session_role in ('strength', 'mixed') as counts_toward_strength
            from classified
            order by day desc, created_at desc
            limit {int(limit)}
            """,
            *args,
        )
        return tuple(rows)

    async def get_session(
        self, *, sid: str, owner: str, delegated: bool
    ) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            select
              training_session_id, owner_user_id, day, workout_template_id,
              name, notes, started_at, finished_at, is_active, created_at, updated_at,
              $3::uuid as _target_user_id,
              $4::boolean as _delegated_view
            from {self._schema}.training_session_current_v
            where training_session_id=$1::uuid
              and owner_user_id=$2::uuid
            """,
            sid,
            owner,
            owner,
            delegated,
        )

    async def list_strength_progression(
        self,
        *,
        owner: str,
        delegated: bool,
        start_date: Any,
        end_date: Any,
        limit: int,
    ) -> tuple[Any, ...]:
        rows = await self._connection.fetch(
            f"""
            select
              s.training_session_id,
              s.day,
              s.name as session_name,
              l.exercise_id,
              max(l.exercise_name) as exercise_name,
              count(l.training_set_log_id)::int as set_count,
              coalesce(sum(l.reps), 0)::int as total_reps,
              coalesce(max(l.weight), 0)::float as max_load,
              coalesce(sum(l.volume), 0)::float as total_volume,
              array_agg(distinct role_resolution.resolution_source
                order by role_resolution.resolution_source
              ) as role_resolution_sources,
              case
                when count(distinct nullif(trim(l.load_unit), '')) = 0 then null
                when count(distinct nullif(trim(l.load_unit), '')) = 1
                  then max(nullif(trim(l.load_unit), ''))
                else 'mixed'
              end as load_unit,
              $4::uuid as _target_user_id,
              $5::boolean as _delegated_view
            from {self._schema}.training_session_current_v s
            join {self._schema}.training_set_log l
              on l.training_session_id=s.training_session_id
             and l.owner_user_id=s.owner_user_id
            join {self._schema}.training_set_effective_role_v1 role_resolution
              on role_resolution.training_set_log_id=l.training_set_log_id
             and role_resolution.training_session_id=l.training_session_id
             and role_resolution.owner_user_id=l.owner_user_id
            where s.owner_user_id=$1::uuid
              and s.day between $2::date and $3::date
              and s.is_active=true
              and s.finished_at is not null
              and l.is_active=true
              and role_resolution.effective_role='strength'
            group by
              s.training_session_id,
              s.day,
              s.name,
              s.created_at,
              l.exercise_id,
              l.exercise_sort_order
            order by
              s.day desc,
              s.created_at desc,
              l.exercise_sort_order asc,
              exercise_name asc
            limit {int(limit)}
            """,
            owner,
            start_date,
            end_date,
            owner,
            delegated,
        )
        return tuple(rows)

    async def deactivate_session(
        self, *, sid: str, owner: str, reason: str
    ) -> Any:
        async with self._connection.transaction():
            await set_transaction_actor(self._connection, actor_user_id=owner)
            return await write_training_void(
                self._connection,
                training_session_id=sid,
                reason=reason,
            )

    async def correct_session(
        self,
        *,
        sid: str,
        owner: str,
        intent: dict[str, Any],
        idempotency_key: str,
    ) -> Any | None:
        async with self._connection.transaction():
            await set_transaction_actor(self._connection, actor_user_id=owner)
            replacement_id = await write_training_correction(
                self._connection,
                training_session_id=sid,
                intent=intent,
                idempotency_key=idempotency_key,
            )
            return await self._connection.fetchrow(
                f"""
                select
                  training_session_id, owner_user_id, day,
                  workout_template_id, name, notes, started_at, finished_at,
                  supersedes_training_session_id, is_active,
                  created_at, updated_at
                from {self._schema}.training_session_current_v
                where training_session_id=$1::uuid
                  and owner_user_id=$2::uuid
                """,
                replacement_id,
                owner,
            )

    async def list_session_sets(
        self,
        *,
        sid: str,
        owner: str,
        delegated: bool,
        include_inactive: bool,
    ) -> tuple[Any, ...]:
        where_active = "" if include_inactive else "and l.is_active=true"
        current_parent = "" if include_inactive else f"""
              and exists (
                select 1
                from {self._schema}.training_session_current_v current_session
                where current_session.training_session_id=l.training_session_id
                  and current_session.owner_user_id=l.owner_user_id
              )
        """
        rows = await self._connection.fetch(
            f"""
            select
              l.training_set_log_id, l.training_session_id, l.owner_user_id,
              l.workout_template_id, l.exercise_id, l.exercise_name,
              l.set_type, l.exercise_role_snapshot, l.capture_role,
              coalesce(l.capture_role, l.exercise_role_snapshot, 'unknown') as exercise_role,
              role_resolution.effective_role,
              role_resolution.resolution_source,
              role_resolution.role_conflict,
              l.exercise_sort_order, l.set_index, l.weight, l.reps, l.volume,
              l.load_unit,
              l.flags, l.notes, l.is_active, l.created_at, l.updated_at,
              $3::uuid as _target_user_id,
              $4::boolean as _delegated_view
            from {self._schema}.training_set_log l
            join {self._schema}.training_set_effective_role_v1 role_resolution
              on role_resolution.training_set_log_id=l.training_set_log_id
             and role_resolution.training_session_id=l.training_session_id
             and role_resolution.owner_user_id=l.owner_user_id
            where l.training_session_id=$1::uuid
              and l.owner_user_id=$2::uuid
              {where_active}
              {current_parent}
            order by l.exercise_sort_order asc, l.set_index asc, l.created_at asc
            """,
            sid,
            owner,
            owner,
            delegated,
        )
        return tuple(rows)

    async def list_set_segments(
        self,
        *,
        sid: str,
        setid: str,
        owner: str,
        delegated: bool,
    ) -> tuple[Any, ...] | None:
        parent = await self._connection.fetchrow(
            f"""
            select l.training_set_log_id
            from {self._schema}.training_set_log l
            join {self._schema}.training_session_current_v s
              on s.training_session_id=l.training_session_id
             and s.owner_user_id=l.owner_user_id
            where l.training_set_log_id=$1::uuid
              and l.training_session_id=$2::uuid
              and l.owner_user_id=$3::uuid
              and l.is_active=true
            """,
            setid,
            sid,
            owner,
        )
        if not parent:
            return None
        rows = await self._connection.fetch(
            f"""
            select
              training_set_log_segment_id,
              training_set_log_id,
              segment_index,
              label,
              weight,
              reps,
              volume,
              notes,
              created_at,
              updated_at,
              $2::uuid as _target_user_id,
              $3::boolean as _delegated_view
            from {self._schema}.training_set_log_segment
            where training_set_log_id=$1::uuid
            order by segment_index asc, created_at asc
            """,
            setid,
            owner,
            delegated,
        )
        return tuple(rows)


@asynccontextmanager
async def lifeswitch_training_sessions_repository(
    request: Request,
    *,
    connection_factory: ConnectionFactory = connect_lifeswitch,
    training_schema: str | None = None,
) -> AsyncIterator[PostgresLifeSwitchTrainingSessionsRepository]:
    connection = await connection_factory(request)
    try:
        yield PostgresLifeSwitchTrainingSessionsRepository(
            connection,
            training_schema=resolve_training_schema(training_schema),
        )
    finally:
        await connection.close()
