from __future__ import annotations

"""PostgreSQL effect boundary for LifeSwitch workout-template sharing."""

import datetime as dt
import os
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
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


def _clean_text(value: Any, max_len: int | None = None) -> str:
    text = str(value or "").strip()
    if max_len is not None and len(text) > max_len:
        text = text[:max_len]
    return text


class WorkoutShareImportError(RuntimeError):
    """A stable, transport-neutral workout-share import rejection."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class WorkoutSharePreview:
    share: Any
    exercises: tuple[Any, ...]
    segments: tuple[Any, ...]
    expired: bool


@dataclass(frozen=True)
class WorkoutShareImport:
    imported_workout: Any
    copied_exercises: tuple[Any, ...]


class PostgresLifeSwitchTrainingSharingRepository:
    """Own all 17 retained workout-sharing SQL effects and its transaction."""

    def __init__(self, connection: Any, *, training_schema: str) -> None:
        self._connection = connection
        self._schema = resolve_training_schema(training_schema)

    async def _unique_imported_workout_name(
        self, *, owner_user_id: str, base_name: str
    ) -> str:
        base = _clean_text(base_name or "Imported Workout", 160) or "Imported Workout"
        candidate = base
        for index in range(0, 50):
            if index == 0:
                candidate = base
            elif index == 1:
                candidate = f"{base} (Imported)"
            else:
                candidate = f"{base} (Imported {index})"

            row = await self._connection.fetchrow(
                f"""
                select workout_template_id
                from {self._schema}.workout_template
                where owner_user_id=$1::uuid
                  and lower(name)=lower($2)
                limit 1
                """,
                owner_user_id,
                candidate,
            )
            if not row:
                return candidate

        return f"{base} (Imported {secrets.token_hex(3)})"

    async def create_share(
        self,
        *,
        owner_user_id: str,
        workout_template_id: str,
        token_hash: str,
        label: str,
        notes: str,
    ) -> tuple[Any | None, Any | None]:
        template = await self._connection.fetchrow(
            f"""
            select workout_template_id, owner_user_id, name, notes, is_active
            from {self._schema}.workout_template
            where workout_template_id=$1::uuid
              and owner_user_id=$2::uuid
              and is_active=true
            limit 1
            """,
            workout_template_id,
            owner_user_id,
        )
        if not template:
            return None, None

        share = await self._connection.fetchrow(
            f"""
            insert into {self._schema}.workout_template_share (
              token_hash,
              created_by_user_id,
              workout_template_id,
              status,
              label,
              notes
            )
            values ($1, $2::uuid, $3::uuid, 'active', $4, $5)
            returning
              workout_template_share_id,
              token_hash,
              created_by_user_id,
              workout_template_id,
              status,
              label,
              notes,
              expires_at,
              revoked_at,
              created_at,
              updated_at
            """,
            token_hash,
            owner_user_id,
            workout_template_id,
            label,
            notes,
        )
        return template, share

    async def list_shares(
        self, *, owner_user_id: str, include_inactive: bool
    ) -> tuple[Any, ...]:
        status_filter = "" if include_inactive else "and s.status='active'"
        rows = await self._connection.fetch(
            f"""
            select
              s.workout_template_share_id,
              s.created_by_user_id,
              s.workout_template_id,
              wt.name as workout_name,
              s.status,
              s.label,
              s.notes,
              s.expires_at,
              s.revoked_at,
              s.created_at,
              s.updated_at
            from {self._schema}.workout_template_share s
            join {self._schema}.workout_template wt
              on wt.workout_template_id=s.workout_template_id
            where s.created_by_user_id=$1::uuid
              {status_filter}
            order by s.created_at desc
            limit 100
            """,
            owner_user_id,
        )
        return tuple(rows)

    async def preview_share(
        self, *, token_hash: str, now: dt.datetime
    ) -> WorkoutSharePreview | None:
        share = await self._connection.fetchrow(
            f"""
            select
              s.workout_template_share_id,
              s.created_by_user_id,
              coalesce(p.display_name, s.created_by_user_id::text) as creator_display_name,
              s.workout_template_id,
              s.status,
              s.label,
              s.notes,
              s.expires_at,
              s.revoked_at,
              s.created_at,
              s.updated_at,
              wt.name as workout_name,
              wt.notes as workout_notes,
              wt.is_active as workout_is_active
            from {self._schema}.workout_template_share s
            join {self._schema}.workout_template wt
              on wt.workout_template_id=s.workout_template_id
            left join lifeswitch_people.user_profile p
              on p.user_id=s.created_by_user_id
            where s.token_hash=$1
            limit 1
            """,
            token_hash,
        )
        if not share:
            return None

        if share["status"] == "active" and share["expires_at"] < now:
            await self._connection.execute(
                f"""
                update {self._schema}.workout_template_share
                   set status='expired',
                       updated_at=now()
                 where workout_template_share_id=$1::uuid
                """,
                str(share["workout_template_share_id"]),
            )
            return WorkoutSharePreview(share, (), (), True)

        exercises = await self._connection.fetch(
            f"""
            select
              workout_template_exercise_id,
              workout_template_id,
              exercise_id,
              display_name_snapshot,
              sort_order,
              set_type,
              planned_sets,
              default_weight,
              default_reps,
              flags,
              created_at,
              updated_at
            from {self._schema}.workout_template_exercise
            where workout_template_id=$1::uuid
            order by sort_order asc, created_at asc
            """,
            str(share["workout_template_id"]),
        )

        exercise_ids = [str(row["workout_template_exercise_id"]) for row in exercises]
        segments: tuple[Any, ...] = ()
        if exercise_ids:
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
                where workout_template_exercise_id = any($1::uuid[])
                order by workout_template_exercise_id, segment_index asc
                """,
                exercise_ids,
            )
            segments = tuple(rows)

        return WorkoutSharePreview(share, tuple(exercises), segments, False)

    async def import_share(
        self, *, importer_user_id: str, token_hash: str, now: dt.datetime
    ) -> WorkoutShareImport:
        async with self._connection.transaction():
            share = await self._connection.fetchrow(
                f"""
                select
                  s.workout_template_share_id,
                  s.created_by_user_id,
                  s.workout_template_id,
                  s.status,
                  s.expires_at,
                  wt.name as workout_name,
                  wt.notes as workout_notes,
                  wt.is_active as workout_is_active
                from {self._schema}.workout_template_share s
                join {self._schema}.workout_template wt
                  on wt.workout_template_id=s.workout_template_id
                where s.token_hash=$1
                for update
                """,
                token_hash,
            )
            if not share:
                raise WorkoutShareImportError("not_found", "share not found")
            if str(share["status"]) != "active":
                raise WorkoutShareImportError(
                    "inactive", f"share is {share['status']}"
                )
            if share["expires_at"] and share["expires_at"] < now:
                await self._connection.execute(
                    f"""
                    update {self._schema}.workout_template_share
                       set status='expired',
                           updated_at=now()
                     where workout_template_share_id=$1::uuid
                    """,
                    str(share["workout_template_share_id"]),
                )
                raise WorkoutShareImportError("expired", "share expired")
            if not share["workout_is_active"]:
                raise WorkoutShareImportError(
                    "workout_inactive", "shared workout is inactive"
                )

            new_name = await self._unique_imported_workout_name(
                owner_user_id=importer_user_id,
                base_name=str(share["workout_name"] or "Imported Workout"),
            )

            new_template = await self._connection.fetchrow(
                f"""
                insert into {self._schema}.workout_template (
                  owner_user_id,
                  name,
                  notes,
                  is_active
                )
                values ($1::uuid, $2, $3, true)
                returning workout_template_id, owner_user_id, name, notes, is_active, created_at, updated_at
                """,
                importer_user_id,
                new_name,
                share["workout_notes"] or "",
            )

            source_exercises = await self._connection.fetch(
                f"""
                select
                  workout_template_exercise_id,
                  exercise_id,
                  display_name_snapshot,
                  sort_order,
                  set_type,
                  planned_sets,
                  default_weight,
                  default_reps,
                  flags
                from {self._schema}.workout_template_exercise
                where workout_template_id=$1::uuid
                order by sort_order asc, created_at asc
                """,
                str(share["workout_template_id"]),
            )

            copied: list[Any] = []
            for exercise in source_exercises:
                exercise_id = str(exercise["exercise_id"])
                display_name = (
                    _clean_text(exercise["display_name_snapshot"] or exercise_id, 240)
                    or exercise_id
                )

                await self._connection.execute(
                    f"""
                    insert into {self._schema}.my_exercise (
                      owner_user_id,
                      exercise_id,
                      display_name,
                      kind,
                      modality,
                      matched_source,
                      is_active
                    )
                    values ($1::uuid, $2, $3, 'strength', 'imported', 'workout_share_import', true)
                    on conflict (owner_user_id, exercise_id)
                    do update set
                      display_name=case
                        when {self._schema}.my_exercise.display_name='' then excluded.display_name
                        else {self._schema}.my_exercise.display_name
                      end,
                      is_active=true,
                      updated_at=now()
                    """,
                    importer_user_id,
                    exercise_id,
                    display_name,
                )

                new_exercise = await self._connection.fetchrow(
                    f"""
                    insert into {self._schema}.workout_template_exercise (
                      workout_template_id,
                      exercise_id,
                      display_name_snapshot,
                      sort_order,
                      set_type,
                      planned_sets,
                      default_weight,
                      default_reps,
                      flags
                    )
                    values ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9)
                    returning
                      workout_template_exercise_id,
                      workout_template_id,
                      exercise_id,
                      display_name_snapshot,
                      sort_order,
                      set_type,
                      planned_sets,
                      default_weight,
                      default_reps,
                      flags,
                      created_at,
                      updated_at
                    """,
                    str(new_template["workout_template_id"]),
                    exercise_id,
                    exercise["display_name_snapshot"],
                    int(exercise["sort_order"] or 10),
                    str(exercise["set_type"] or "straight"),
                    int(exercise["planned_sets"] or 3),
                    exercise["default_weight"] or 0,
                    int(exercise["default_reps"] or 10),
                    exercise["flags"] or "",
                )

                source_segments = await self._connection.fetch(
                    f"""
                    select segment_index, label, default_weight, default_reps
                    from {self._schema}.workout_template_exercise_segment
                    where workout_template_exercise_id=$1::uuid
                    order by segment_index asc
                    """,
                    str(exercise["workout_template_exercise_id"]),
                )

                for segment in source_segments:
                    await self._connection.execute(
                        f"""
                        insert into {self._schema}.workout_template_exercise_segment (
                          workout_template_exercise_id,
                          segment_index,
                          label,
                          default_weight,
                          default_reps
                        )
                        values ($1::uuid, $2, $3, $4, $5)
                        on conflict (workout_template_exercise_id, segment_index)
                        do update set
                          label=excluded.label,
                          default_weight=excluded.default_weight,
                          default_reps=excluded.default_reps,
                          updated_at=now()
                        """,
                        str(new_exercise["workout_template_exercise_id"]),
                        int(segment["segment_index"] or 1),
                        segment["label"] or "",
                        segment["default_weight"] or 0,
                        int(segment["default_reps"] or 0),
                    )

                copied.append(new_exercise)

        return WorkoutShareImport(new_template, tuple(copied))

    async def revoke_share(
        self, *, workout_template_share_id: str, owner_user_id: str
    ) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            update {self._schema}.workout_template_share
               set status='revoked',
                   revoked_at=now(),
                   updated_at=now()
             where workout_template_share_id=$1::uuid
               and created_by_user_id=$2::uuid
               and status='active'
            returning
              workout_template_share_id,
              created_by_user_id,
              workout_template_id,
              status,
              label,
              notes,
              expires_at,
              revoked_at,
              created_at,
              updated_at
            """,
            workout_template_share_id,
            owner_user_id,
        )


@asynccontextmanager
async def lifeswitch_training_sharing_repository(
    request: Request,
    *,
    connection_factory: ConnectionFactory = connect_lifeswitch,
    training_schema: str | None = None,
) -> AsyncIterator[PostgresLifeSwitchTrainingSharingRepository]:
    connection = await connection_factory(request)
    try:
        yield PostgresLifeSwitchTrainingSharingRepository(
            connection,
            training_schema=resolve_training_schema(training_schema),
        )
    finally:
        await connection.close()
