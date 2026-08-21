from __future__ import annotations

"""PostgreSQL effect boundary for LifeSwitch workout templates."""

import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from starlette.requests import Request

from seebx.adapters.lifeswitch_postgres import connect_lifeswitch
from seebx.adapters.lifeswitch_training_writes_postgres import set_transaction_actor


ConnectionFactory = Callable[[Request], Awaitable[Any]]
OwnerAuthorizer = Callable[[str], None]
ExistingOwnerGuard = Callable[[Any | None], None]
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


class TemplateUpsertError(RuntimeError):
    pass


@dataclass(frozen=True)
class OwnedResult:
    value: Any


class PostgresLifeSwitchTrainingTemplatesRepository:
    """Own all 21 template/exercise/segment SQL effects and two transactions."""

    def __init__(self, connection: Any, *, training_schema: str) -> None:
        self._connection = connection
        self._schema = resolve_training_schema(training_schema)

    async def list_templates(
        self, *, owner_user_id: str, include_inactive: bool
    ) -> tuple[Any, ...]:
        where_active = "" if include_inactive else "and is_active=true"
        rows = await self._connection.fetch(
            f"""
            select
              workout_template_id, owner_user_id,
              name, notes, workout_role, is_active, created_at, updated_at,
              (
                select count(*)::int
                from {self._schema}.training_session s
                where s.owner_user_id=wt.owner_user_id
                  and s.workout_template_id=wt.workout_template_id
                  and s.finished_at is not null
                  and s.is_active=true
                  and s.workout_role_snapshot is null
                  and not exists (
                    select 1 from {self._schema}.training_session_role_event re
                    where re.training_session_id=s.training_session_id
                  )
                  and not exists (
                    select 1
                    from {self._schema}.training_set_log l
                    join {self._schema}.training_set_effective_role_v1 role_resolution
                      on role_resolution.training_set_log_id=l.training_set_log_id
                     and role_resolution.training_session_id=l.training_session_id
                     and role_resolution.owner_user_id=l.owner_user_id
                    where l.training_session_id=s.training_session_id
                      and l.owner_user_id=s.owner_user_id
                      and l.is_active=true
                      and role_resolution.effective_role in ('strength', 'rehab')
                  )
              ) as unclassified_session_count
            from {self._schema}.workout_template wt
            where owner_user_id=$1::uuid
              {where_active}
            order by updated_at desc
            """,
            owner_user_id,
        )
        return tuple(rows)

    async def upsert_template(
        self,
        *,
        owner_user_id: str,
        workout_template_id: str | None,
        name: str,
        notes: str,
        workout_role: str,
        idempotency_key: str,
        verify_existing_owner: ExistingOwnerGuard,
    ) -> Any:
        async with self._connection.transaction():
            await set_transaction_actor(
                self._connection, actor_user_id=owner_user_id
            )
            if workout_template_id:
                existing_owner = await self._connection.fetchval(
                    f"select owner_user_id from {self._schema}.workout_template where workout_template_id=$1::uuid",
                    workout_template_id,
                )
                verify_existing_owner(existing_owner)
                row = await self._connection.fetchrow(
                    f"""
                    insert into {self._schema}.workout_template
                      (workout_template_id, owner_user_id, name, notes, is_active)
                    values
                      ($1::uuid, $2::uuid, $3, $4, true)
                    on conflict (workout_template_id) do update
                      set name=excluded.name,
                          notes=excluded.notes,
                          updated_at=now(),
                          is_active=true
                    returning workout_template_id
                    """,
                    workout_template_id,
                    owner_user_id,
                    name,
                    notes,
                )
            else:
                row = await self._connection.fetchrow(
                    f"""
                    insert into {self._schema}.workout_template
                      (owner_user_id, name, notes, is_active)
                    values
                      ($1::uuid, $2, $3, true)
                    on conflict (owner_user_id, name) do update
                      set notes=excluded.notes,
                          updated_at=now(),
                          is_active=true
                    returning workout_template_id
                    """,
                    owner_user_id,
                    name,
                    notes,
                )
            if not row:
                raise TemplateUpsertError("upsert_failed")
            saved_id = str(row["workout_template_id"])
            current_role = await self._connection.fetchval(
                f"select workout_role from {self._schema}.workout_template where owner_user_id=$1::uuid and workout_template_id=$2::uuid",
                owner_user_id,
                saved_id,
            )
            if current_role == workout_role:
                final_row = await self._connection.fetchrow(
                    f"select * from {self._schema}.workout_template where owner_user_id=$1::uuid and workout_template_id=$2::uuid",
                    owner_user_id,
                    saved_id,
                )
            else:
                final_row = await self._connection.fetchrow(
                    f"select * from {self._schema}.set_workout_template_role($1::uuid, $2::uuid, $3, $4, $5)",
                    owner_user_id,
                    saved_id,
                    workout_role,
                    "Workout role selected in Workouts",
                    idempotency_key,
                )
        return final_row

    async def classify_historical_sessions(
        self,
        *,
        owner_user_id: str,
        workout_template_id: str,
        workout_role: str,
        reason: str,
        idempotency_key: str,
    ) -> int:
        async with self._connection.transaction():
            await set_transaction_actor(
                self._connection, actor_user_id=owner_user_id
            )
            count = await self._connection.fetchval(
                f"select {self._schema}.classify_unclassified_training_sessions($1::uuid, $2::uuid, $3, $4, $5)",
                owner_user_id,
                workout_template_id,
                workout_role,
                reason,
                idempotency_key,
            )
        return int(count or 0)

    async def deactivate_template(
        self, *, workout_template_id: str, owner_user_id: str
    ) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            update {self._schema}.workout_template
               set is_active=false, updated_at=now()
             where workout_template_id=$1::uuid
               and owner_user_id=$2::uuid
            returning workout_template_id, owner_user_id, is_active, updated_at
            """,
            workout_template_id,
            owner_user_id,
        )

    async def list_template_exercises(
        self,
        *,
        workout_template_id: str,
        authorize_owner: OwnerAuthorizer,
    ) -> OwnedResult | None:
        owner = await self._connection.fetchval(
            f"select owner_user_id from {self._schema}.workout_template where workout_template_id=$1::uuid",
            workout_template_id,
        )
        if not owner:
            return None
        authorize_owner(str(owner))
        rows = await self._connection.fetch(
            f"""
            select
              workout_template_exercise_id, workout_template_id,
              exercise_id, display_name_snapshot, sort_order, set_type,
              planned_sets, default_weight, default_reps, flags,
              created_at, updated_at
            from {self._schema}.workout_template_exercise
            where workout_template_id=$1::uuid
            order by sort_order asc, created_at asc
            """,
            workout_template_id,
        )
        return OwnedResult(tuple(rows))

    async def upsert_template_exercise(
        self,
        *,
        workout_template_id: str,
        exercise_id: str,
        display_name_snapshot: str,
        sort_order: int,
        set_type: str,
        planned_sets: int,
        default_weight: float,
        default_reps: int,
        flags: str,
        authorize_owner: OwnerAuthorizer,
    ) -> OwnedResult | None:
        owner = await self._connection.fetchval(
            f"select owner_user_id from {self._schema}.workout_template where workout_template_id=$1::uuid and is_active",
            workout_template_id,
        )
        if not owner:
            return None
        authorize_owner(str(owner))
        row = await self._connection.fetchrow(
            f"""
            insert into {self._schema}.workout_template_exercise
              (workout_template_id, exercise_id, display_name_snapshot, sort_order, set_type, planned_sets, default_weight, default_reps, flags)
            values
              ($1::uuid, $2, nullif($3, ''), $4, $5, $6, $7, $8, $9)
            on conflict (workout_template_id, exercise_id) do update
              set display_name_snapshot=coalesce(excluded.display_name_snapshot, {self._schema}.workout_template_exercise.display_name_snapshot),
                  sort_order=excluded.sort_order,
                  set_type=excluded.set_type,
                  planned_sets=excluded.planned_sets,
                  default_weight=excluded.default_weight,
                  default_reps=excluded.default_reps,
                  flags=excluded.flags,
                  updated_at=now()
            returning
              workout_template_exercise_id, workout_template_id,
              exercise_id, display_name_snapshot, sort_order, set_type, planned_sets, default_weight, default_reps, flags,
              created_at, updated_at
            """,
            workout_template_id,
            exercise_id,
            display_name_snapshot,
            sort_order,
            set_type,
            planned_sets,
            default_weight,
            default_reps,
            flags,
        )
        return OwnedResult(row)

    async def delete_template_exercise(
        self,
        *,
        workout_template_id: str,
        workout_template_exercise_id: str,
        authorize_owner: OwnerAuthorizer,
    ) -> OwnedResult | None:
        owner = await self._connection.fetchval(
            f"select owner_user_id from {self._schema}.workout_template where workout_template_id=$1::uuid",
            workout_template_id,
        )
        if not owner:
            return None
        authorize_owner(str(owner))
        result = await self._connection.execute(
            f"""
            delete from {self._schema}.workout_template_exercise
             where workout_template_exercise_id=$1::uuid
               and workout_template_id=$2::uuid
            """,
            workout_template_exercise_id,
            workout_template_id,
        )
        return OwnedResult(result)

    async def list_exercise_segments(
        self,
        *,
        workout_template_exercise_id: str,
        authorize_owner: OwnerAuthorizer,
    ) -> OwnedResult | None:
        owner = await self._connection.fetchval(
            f"""
            select wt.owner_user_id
            from {self._schema}.workout_template_exercise e
            join {self._schema}.workout_template wt
              on wt.workout_template_id=e.workout_template_id
            where e.workout_template_exercise_id=$1::uuid
            """,
            workout_template_exercise_id,
        )
        if not owner:
            return None
        authorize_owner(str(owner))
        rows = await self._connection.fetch(
            f"""
            select
              workout_template_exercise_segment_id,
              workout_template_exercise_id,
              segment_index,
              label,
              default_weight,
              default_reps,
              created_at,
              updated_at
            from {self._schema}.workout_template_exercise_segment
            where workout_template_exercise_id=$1::uuid
            order by segment_index asc, created_at asc
            """,
            workout_template_exercise_id,
        )
        return OwnedResult(tuple(rows))

    async def upsert_exercise_segment(
        self,
        *,
        workout_template_exercise_id: str,
        segment_index: int,
        label: str,
        default_weight: float,
        default_reps: int,
        authorize_owner: OwnerAuthorizer,
    ) -> OwnedResult | None:
        owner = await self._connection.fetchval(
            f"""
            select wt.owner_user_id
            from {self._schema}.workout_template_exercise e
            join {self._schema}.workout_template wt
              on wt.workout_template_id=e.workout_template_id
            where e.workout_template_exercise_id=$1::uuid
              and wt.is_active=true
            """,
            workout_template_exercise_id,
        )
        if not owner:
            return None
        authorize_owner(str(owner))
        row = await self._connection.fetchrow(
            f"""
            insert into {self._schema}.workout_template_exercise_segment
              (workout_template_exercise_id, segment_index, label, default_weight, default_reps)
            values
              ($1::uuid, $2, $3, $4, $5)
            on conflict (workout_template_exercise_id, segment_index) do update
              set label=excluded.label,
                  default_weight=excluded.default_weight,
                  default_reps=excluded.default_reps,
                  updated_at=now()
            returning
              workout_template_exercise_segment_id,
              workout_template_exercise_id,
              segment_index,
              label,
              default_weight,
              default_reps,
              created_at,
              updated_at
            """,
            workout_template_exercise_id,
            segment_index,
            label,
            default_weight,
            default_reps,
        )
        return OwnedResult(row)

    async def delete_exercise_segment(
        self,
        *,
        workout_template_exercise_id: str,
        workout_template_exercise_segment_id: str,
        authorize_owner: OwnerAuthorizer,
    ) -> OwnedResult | None:
        owner = await self._connection.fetchval(
            f"""
            select wt.owner_user_id
            from {self._schema}.workout_template_exercise e
            join {self._schema}.workout_template wt
              on wt.workout_template_id=e.workout_template_id
            where e.workout_template_exercise_id=$1::uuid
            """,
            workout_template_exercise_id,
        )
        if not owner:
            return None
        authorize_owner(str(owner))
        result = await self._connection.execute(
            f"""
            delete from {self._schema}.workout_template_exercise_segment
             where workout_template_exercise_segment_id=$1::uuid
               and workout_template_exercise_id=$2::uuid
            """,
            workout_template_exercise_segment_id,
            workout_template_exercise_id,
        )
        return OwnedResult(result)


@asynccontextmanager
async def lifeswitch_training_templates_repository(
    request: Request,
    *,
    connection_factory: ConnectionFactory = connect_lifeswitch,
    training_schema: str | None = None,
) -> AsyncIterator[PostgresLifeSwitchTrainingTemplatesRepository]:
    connection = await connection_factory(request)
    try:
        yield PostgresLifeSwitchTrainingTemplatesRepository(
            connection,
            training_schema=resolve_training_schema(training_schema),
        )
    finally:
        await connection.close()
