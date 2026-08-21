from __future__ import annotations

"""PostgreSQL effect boundary for the LifeSwitch My Exercises capability."""

import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from starlette.requests import Request

from seebx.adapters.lifeswitch_postgres import connect_lifeswitch


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


class PostgresLifeSwitchTrainingExercisesRepository:
    """Own all five retained My Exercises SQL effects and its transaction."""

    def __init__(self, connection: Any, *, training_schema: str) -> None:
        self._connection = connection
        self._schema = resolve_training_schema(training_schema)

    async def list_exercises(
        self, *, owner_user_id: str, include_inactive: bool
    ) -> tuple[Any, ...]:
        where_active = "" if include_inactive else "and me.is_active=true"
        rows = await self._connection.fetch(
            f"""
            select
              my_exercise_id, owner_user_id,
              exercise_id, display_name, kind, modality,
              brand_name, model_name, matched_text, matched_source,
              exercise_role,
              is_active, created_at, updated_at
            from {self._schema}.my_exercise as me
            where me.owner_user_id=$1::uuid
              {where_active}
            order by lower(display_name) asc
            """,
            owner_user_id,
        )
        return tuple(rows)

    async def upsert_exercise(
        self,
        *,
        owner_user_id: str,
        exercise_id: str,
        display_name: str,
        kind: str,
        modality: str,
        brand_name: str | None,
        model_name: str | None,
        matched_text: str | None,
        matched_source: str | None,
        exercise_role: str | None,
    ) -> Any | None:
        async with self._connection.transaction():
            existing = await self._connection.fetchrow(
                f"""
                select my_exercise_id, exercise_role
                from {self._schema}.my_exercise
                where owner_user_id=$1::uuid and exercise_id=$2
                for update
                """,
                owner_user_id,
                exercise_id,
            )
            row = await self._connection.fetchrow(
                f"""
                insert into {self._schema}.my_exercise
                  (owner_user_id, exercise_id, display_name, kind, modality,
                   brand_name, model_name, matched_text, matched_source,
                   exercise_role, is_active)
                values
                  ($1::uuid, $2, $3, $4, $5,
                   $6, $7, $8, $9, coalesce($10, 'strength'), true)
                on conflict (owner_user_id, exercise_id) do update
                  set display_name=excluded.display_name,
                      kind=excluded.kind,
                      modality=excluded.modality,
                      brand_name=excluded.brand_name,
                      model_name=excluded.model_name,
                      matched_text=excluded.matched_text,
                      matched_source=excluded.matched_source,
                      exercise_role=coalesce($10, {self._schema}.my_exercise.exercise_role),
                      updated_at=now(),
                      is_active=true
                returning
                  my_exercise_id, owner_user_id,
                  exercise_id, display_name, kind, modality,
                  brand_name, model_name, matched_text, matched_source,
                  exercise_role,
                  is_active, created_at, updated_at
                """,
                owner_user_id,
                exercise_id,
                display_name,
                kind,
                modality,
                brand_name,
                model_name,
                matched_text,
                matched_source,
                exercise_role,
            )
            previous_role = str(existing["exercise_role"]) if existing else None
            next_role = str(row["exercise_role"]) if row else ""
            if row and previous_role != next_role:
                await self._connection.execute(
                    f"""
                    insert into {self._schema}.my_exercise_role_event
                      (owner_user_id, my_exercise_id, exercise_id,
                       previous_role, new_role, changed_by_user_id, change_source)
                    values ($1::uuid, $2::uuid, $3, $4, $5, $1::uuid, 'user')
                    """,
                    owner_user_id,
                    row["my_exercise_id"],
                    row["exercise_id"],
                    previous_role,
                    next_role,
                )
        return row

    async def deactivate_exercise(
        self, *, my_exercise_id: str, owner_user_id: str
    ) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            update {self._schema}.my_exercise
               set is_active=false, updated_at=now()
             where my_exercise_id=$1::uuid
               and owner_user_id=$2::uuid
            returning my_exercise_id, owner_user_id, is_active, updated_at
            """,
            my_exercise_id,
            owner_user_id,
        )


@asynccontextmanager
async def lifeswitch_training_exercises_repository(
    request: Request,
    *,
    connection_factory: ConnectionFactory = connect_lifeswitch,
    training_schema: str | None = None,
) -> AsyncIterator[PostgresLifeSwitchTrainingExercisesRepository]:
    connection = await connection_factory(request)
    try:
        yield PostgresLifeSwitchTrainingExercisesRepository(
            connection,
            training_schema=resolve_training_schema(training_schema),
        )
    finally:
        await connection.close()
