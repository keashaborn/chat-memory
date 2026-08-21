from __future__ import annotations

"""PostgreSQL effect boundary for LifeSwitch nutrition logs."""

import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from starlette.requests import Request

from seebx.adapters.lifeswitch_postgres import connect_lifeswitch


ConnectionFactory = Callable[[Request], Awaitable[Any]]
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")


def _schema(value: str, variable: str) -> str:
    candidate = str(value).strip()
    if _IDENTIFIER.fullmatch(candidate) is None:
        raise RuntimeError(f"invalid {variable}")
    return candidate


def resolve_nutrition_schema(value: str | None = None) -> str:
    return _schema(
        os.getenv("LIFESWITCH_NUTRITION_SCHEMA", "lifeswitch_nutrition")
        if value is None
        else value,
        "LIFESWITCH_NUTRITION_SCHEMA",
    )


def resolve_people_schema(value: str | None = None) -> str:
    return _schema(
        os.getenv("LIFESWITCH_PEOPLE_SCHEMA", "lifeswitch_people")
        if value is None
        else value,
        "LIFESWITCH_PEOPLE_SCHEMA",
    )


@dataclass(frozen=True)
class NutritionLogRangeRows:
    day_rows: tuple[Any, ...]
    entry_rows: tuple[Any, ...]


@dataclass(frozen=True)
class NutritionLogDayRows:
    day_row: Any | None
    entry_rows: tuple[Any, ...]




class NutritionLogRepositoryError(RuntimeError):
    def __init__(self, code: str, *, item_index: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.item_index = item_index


@dataclass(frozen=True)
class NutritionLogEntryWrite:
    day: date
    meal_id: str | None
    my_food_id: str | None
    qty_g: float | None
    my_food_serving_id: str | None
    qty_servings: float | None
    sort_order: int
    notes: str | None


@dataclass(frozen=True)
class NutritionLogBatchEntryWrite:
    my_food_id: str
    qty_g: float | None
    my_food_serving_id: str | None
    qty_servings: float | None
    sort_order: int
    notes: str | None


@dataclass(frozen=True)
class NutritionLogEntryUpdate:
    nutrition_entry_id: str
    qty_g: float | None
    my_food_serving_id: str | None
    qty_servings: float | None
    sort_order: int | None
    notes: str | None


@dataclass(frozen=True)
class NutritionLogCreateRows:
    day_row: Any
    entry_row: Any | None


@dataclass(frozen=True)
class NutritionLogBatchRows:
    day_row: Any
    entry_rows: tuple[Any, ...]


@dataclass(frozen=True)
class NutritionDayCompletionRows:
    day_row: Any
    changed: bool


class LifeSwitchNutritionLogRepository(Protocol):
    async def has_people_permission(
        self,
        *,
        grantor_user_id: str,
        grantee_user_id: str,
        scope: str,
    ) -> bool: ...

    async def read_range(
        self,
        *,
        owner_user_id: str,
        start_day: date,
        end_day: date,
    ) -> NutritionLogRangeRows: ...

    async def read_day(
        self,
        *,
        owner_user_id: str,
        day: date,
    ) -> NutritionLogDayRows: ...


    async def create_entry(
        self,
        *,
        owner_user_id: str,
        value: NutritionLogEntryWrite,
    ) -> NutritionLogCreateRows: ...

    async def create_entries_batch(
        self,
        *,
        owner_user_id: str,
        day: date,
        items: tuple[NutritionLogBatchEntryWrite, ...],
    ) -> NutritionLogBatchRows: ...

    async def update_entry(
        self,
        *,
        owner_user_id: str,
        value: NutritionLogEntryUpdate,
    ) -> Any: ...

    async def delete_entry(
        self,
        *,
        owner_user_id: str,
        nutrition_entry_id: str,
    ) -> Any | None: ...

    async def set_day_completion(
        self,
        *,
        owner_user_id: str,
        day: date,
        completed: bool,
    ) -> NutritionDayCompletionRows: ...


class PostgresLifeSwitchNutritionLogRepository:
    """Own all retained nutrition-log SQL, transactions, and connection lifetime."""

    def __init__(
        self,
        connection: Any,
        *,
        nutrition_schema: str,
        people_schema: str,
    ) -> None:
        self._connection = connection
        self._nutrition_schema = resolve_nutrition_schema(nutrition_schema)
        self._people_schema = resolve_people_schema(people_schema)

    async def has_people_permission(
        self,
        *,
        grantor_user_id: str,
        grantee_user_id: str,
        scope: str,
    ) -> bool:
        row = await self._connection.fetchrow(
            f"""
            select rp.relationship_permission_id
            from {self._people_schema}.relationship_permission rp
            join {self._people_schema}.relationship r
              on r.relationship_id=rp.relationship_id
            where rp.grantor_user_id=$1::uuid
              and rp.grantee_user_id=$2::uuid
              and rp.permission_scope=$3
              and rp.is_enabled=true
              and r.status='accepted'
            limit 1
            """,
            grantor_user_id,
            grantee_user_id,
            scope,
        )
        return bool(row)

    async def read_range(
        self,
        *,
        owner_user_id: str,
        start_day: date,
        end_day: date,
    ) -> NutritionLogRangeRows:
        day_rows = await self._connection.fetch(
            f"""
            select nutrition_day_id, owner_user_id, day, notes, completed_at, created_at, updated_at
            from {self._nutrition_schema}.nutrition_day
            where owner_user_id=$1::uuid
              and day between $2::date and $3::date
            order by day desc
            """,
            owner_user_id,
            start_day,
            end_day,
        )
        entry_rows = await self._connection.fetch(
            f"""
            select
              nd.day as nutrition_day_date,
              e.nutrition_entry_id, e.nutrition_day_id, e.meal_id, e.my_food_id, e.qty_g,
              e.my_food_serving_id, e.qty_servings, e.sort_order, e.notes,
              e.created_at, e.updated_at,

              coalesce(m.name, f.display_name) as label,
              m.meal_type as meal_type,
              s.name as serving_name,
              s.grams as serving_grams,

              f.brand as food_brand,
              f.variant as food_variant,
              f.source_type as food_source_type,
              f.source_id as food_source_id,

              f.kcal as food_kcal_100g,
              f.protein_g as food_protein_100g,
              f.carbs_g as food_carbs_100g,
              f.fat_g as food_fat_100g,

              mt.kcal as meal_kcal,
              mt.protein_g as meal_protein,
              mt.carbs_g as meal_carbs,
              mt.fat_g as meal_fat

            from {self._nutrition_schema}.nutrition_day nd
            join {self._nutrition_schema}.nutrition_entry e
              on e.nutrition_day_id = nd.nutrition_day_id
            left join {self._nutrition_schema}.meal m on m.meal_id = e.meal_id
            left join {self._nutrition_schema}.my_food f on f.my_food_id = e.my_food_id
            left join {self._nutrition_schema}.my_food_serving s
              on s.my_food_serving_id = e.my_food_serving_id
             and s.my_food_id = e.my_food_id

            left join lateral (
              select
                sum((mf.kcal * coalesce(mi.qty_g, ms.grams * mi.qty_servings))/100.0) as kcal,
                sum((mf.protein_g * coalesce(mi.qty_g, ms.grams * mi.qty_servings))/100.0) as protein_g,
                sum((mf.carbs_g * coalesce(mi.qty_g, ms.grams * mi.qty_servings))/100.0) as carbs_g,
                sum((mf.fat_g * coalesce(mi.qty_g, ms.grams * mi.qty_servings))/100.0) as fat_g
              from {self._nutrition_schema}.meal_item mi
              join {self._nutrition_schema}.my_food mf on mf.my_food_id = mi.my_food_id
              left join {self._nutrition_schema}.my_food_serving ms
                on ms.my_food_serving_id = mi.my_food_serving_id
               and ms.my_food_id = mi.my_food_id
              where mi.meal_id = e.meal_id
            ) mt on true

            where nd.owner_user_id=$1::uuid
              and nd.day between $2::date and $3::date
            order by nd.day desc, e.sort_order, e.created_at
            """,
            owner_user_id,
            start_day,
            end_day,
        )
        return NutritionLogRangeRows(tuple(day_rows), tuple(entry_rows))

    async def read_day(
        self,
        *,
        owner_user_id: str,
        day: date,
    ) -> NutritionLogDayRows:
        day_row = await self._connection.fetchrow(
            f"""
            select nutrition_day_id, owner_user_id, day, notes, completed_at, created_at, updated_at
            from {self._nutrition_schema}.nutrition_day
            where owner_user_id=$1::uuid and day=$2::date
            """,
            owner_user_id,
            day,
        )
        if not day_row:
            return NutritionLogDayRows(None, ())

        rows = await self._connection.fetch(
            f"""
            select
              e.nutrition_entry_id, e.nutrition_day_id, e.meal_id, e.my_food_id, e.qty_g, e.my_food_serving_id, e.qty_servings, e.sort_order, e.notes,
              e.created_at, e.updated_at,

              coalesce(m.name, f.display_name) as label,
              m.meal_type as meal_type,
              s.name as serving_name,
              s.grams as serving_grams,

              f.brand as food_brand,
              f.variant as food_variant,
              f.source_type as food_source_type,
              f.source_id as food_source_id,

              f.kcal as food_kcal_100g,
              f.protein_g as food_protein_100g,
              f.carbs_g as food_carbs_100g,
              f.fat_g as food_fat_100g,

              mt.kcal as meal_kcal, mt.protein_g as meal_protein, mt.carbs_g as meal_carbs, mt.fat_g as meal_fat

            from {self._nutrition_schema}.nutrition_entry e
            left join {self._nutrition_schema}.meal m on m.meal_id = e.meal_id
            left join {self._nutrition_schema}.my_food f on f.my_food_id = e.my_food_id
            left join {self._nutrition_schema}.my_food_serving s
              on s.my_food_serving_id = e.my_food_serving_id
             and s.my_food_id = e.my_food_id

            left join lateral (
              select
                sum((mf.kcal * coalesce(mi.qty_g, ms.grams * mi.qty_servings))/100.0) as kcal,
                sum((mf.protein_g * coalesce(mi.qty_g, ms.grams * mi.qty_servings))/100.0) as protein_g,
                sum((mf.carbs_g * coalesce(mi.qty_g, ms.grams * mi.qty_servings))/100.0) as carbs_g,
                sum((mf.fat_g * coalesce(mi.qty_g, ms.grams * mi.qty_servings))/100.0) as fat_g
              from {self._nutrition_schema}.meal_item mi
              join {self._nutrition_schema}.my_food mf on mf.my_food_id = mi.my_food_id
              left join {self._nutrition_schema}.my_food_serving ms
                on ms.my_food_serving_id = mi.my_food_serving_id
               and ms.my_food_id = mi.my_food_id
              where mi.meal_id = e.meal_id
            ) mt on true

            where e.nutrition_day_id = $1::uuid
            order by e.sort_order, e.created_at
            """,
            str(day_row["nutrition_day_id"]),
        )
        return NutritionLogDayRows(day_row, tuple(rows))


    async def create_entry(
        self,
        *,
        owner_user_id: str,
        value: NutritionLogEntryWrite,
    ) -> NutritionLogCreateRows:
        if value.meal_id is not None:
            active = await self._connection.fetchval(
                f"""
                select is_active
                from {self._nutrition_schema}.meal
                where meal_id=$1::uuid
                  and owner_user_id=$2::uuid
                """,
                value.meal_id,
                owner_user_id,
            )
            if active is not True:
                raise NutritionLogRepositoryError("meal_not_found_or_inactive")

        resolved_qty_g = value.qty_g
        if value.my_food_id is not None:
            active = await self._connection.fetchval(
                f"""
                select is_active
                from {self._nutrition_schema}.my_food
                where my_food_id=$1::uuid
                  and owner_user_id=$2::uuid
                """,
                value.my_food_id,
                owner_user_id,
            )
            if active is not True:
                raise NutritionLogRepositoryError("food_not_found_or_inactive")

            if value.my_food_serving_id is not None:
                serving_grams = await self._connection.fetchval(
                    f"""
                    select grams
                    from {self._nutrition_schema}.my_food_serving
                    where my_food_serving_id=$1::uuid
                      and my_food_id=$2::uuid
                      and is_active=true
                    """,
                    value.my_food_serving_id,
                    value.my_food_id,
                )
                if serving_grams is None:
                    raise NutritionLogRepositoryError("serving_not_found_for_food")
                resolved_qty_g = float(serving_grams) * float(value.qty_servings)

        day_row = await self._connection.fetchrow(
            f"""
            insert into {self._nutrition_schema}.nutrition_day (owner_user_id, day)
            values ($1::uuid, $2::date)
            on conflict (owner_user_id, day) do update
              set updated_at=now()
            returning
              nutrition_day_id, owner_user_id, day, notes, completed_at,
              created_at, updated_at
            """,
            owner_user_id,
            value.day,
        )
        if not day_row:
            raise NutritionLogRepositoryError("nutrition_day_create_failed")

        nutrition_day_id = str(day_row["nutrition_day_id"])
        entry_row = await self._connection.fetchrow(
            f"""
            insert into {self._nutrition_schema}.nutrition_entry
              (
                nutrition_day_id, meal_id, my_food_id, qty_g,
                my_food_serving_id, qty_servings,
                sort_order, notes
              )
            values
              ($1::uuid, $2::uuid, $3::uuid, $4, $5::uuid, $6, $7, $8)
            returning
              nutrition_entry_id, nutrition_day_id, meal_id, my_food_id,
              qty_g, my_food_serving_id, qty_servings,
              sort_order, notes, created_at, updated_at
            """,
            nutrition_day_id,
            value.meal_id,
            value.my_food_id,
            resolved_qty_g,
            value.my_food_serving_id,
            value.qty_servings,
            value.sort_order,
            value.notes,
        )
        current_day = await self._connection.fetchrow(
            f"""
            select
              nutrition_day_id, owner_user_id, day, notes, completed_at,
              created_at, updated_at
            from {self._nutrition_schema}.nutrition_day
            where nutrition_day_id=$1::uuid
            """,
            nutrition_day_id,
        )
        return NutritionLogCreateRows(current_day, entry_row)

    async def create_entries_batch(
        self,
        *,
        owner_user_id: str,
        day: date,
        items: tuple[NutritionLogBatchEntryWrite, ...],
    ) -> NutritionLogBatchRows:
        async with self._connection.transaction():
            prepared = []
            for index, item in enumerate(items):
                food_active = await self._connection.fetchval(
                    f"""
                    select is_active
                    from {self._nutrition_schema}.my_food
                    where my_food_id=$1::uuid
                      and owner_user_id=$2::uuid
                    """,
                    item.my_food_id,
                    owner_user_id,
                )
                if food_active is not True:
                    raise NutritionLogRepositoryError(
                        "batch_food_not_found_or_inactive",
                        item_index=index,
                    )

                resolved_qty_g = item.qty_g
                if item.my_food_serving_id is not None:
                    serving_grams = await self._connection.fetchval(
                        f"""
                        select grams
                        from {self._nutrition_schema}.my_food_serving
                        where my_food_serving_id=$1::uuid
                          and my_food_id=$2::uuid
                          and is_active=true
                        """,
                        item.my_food_serving_id,
                        item.my_food_id,
                    )
                    if serving_grams is None:
                        raise NutritionLogRepositoryError(
                            "batch_serving_not_found_for_food",
                            item_index=index,
                        )
                    resolved_qty_g = float(serving_grams) * float(item.qty_servings)

                prepared.append((item, resolved_qty_g))

            day_row = await self._connection.fetchrow(
                f"""
                insert into {self._nutrition_schema}.nutrition_day (owner_user_id, day)
                values ($1::uuid, $2::date)
                on conflict (owner_user_id, day) do update set updated_at=now()
                returning nutrition_day_id, owner_user_id, day, notes, completed_at, created_at, updated_at
                """,
                owner_user_id,
                day,
            )
            entries = []
            for item, resolved_qty_g in prepared:
                row = await self._connection.fetchrow(
                    f"""
                    insert into {self._nutrition_schema}.nutrition_entry
                      (nutrition_day_id, my_food_id, qty_g, my_food_serving_id,
                       qty_servings, sort_order, notes)
                    values ($1::uuid, $2::uuid, $3, $4::uuid, $5, $6, $7)
                    returning nutrition_entry_id, nutrition_day_id, meal_id, my_food_id,
                              qty_g, my_food_serving_id, qty_servings,
                              sort_order, notes, created_at, updated_at
                    """,
                    day_row["nutrition_day_id"],
                    item.my_food_id,
                    resolved_qty_g,
                    item.my_food_serving_id,
                    item.qty_servings,
                    item.sort_order,
                    item.notes,
                )
                entries.append(row)

            current_day = await self._connection.fetchrow(
                f"""
                select
                  nutrition_day_id, owner_user_id, day, notes, completed_at,
                  created_at, updated_at
                from {self._nutrition_schema}.nutrition_day
                where nutrition_day_id=$1::uuid
                """,
                day_row["nutrition_day_id"],
            )
            return NutritionLogBatchRows(current_day, tuple(entries))

    async def update_entry(
        self,
        *,
        owner_user_id: str,
        value: NutritionLogEntryUpdate,
    ) -> Any:
        async with self._connection.transaction():
            entry = await self._connection.fetchrow(
                f"""
                select e.my_food_id, e.meal_id
                from {self._nutrition_schema}.nutrition_entry e
                join {self._nutrition_schema}.nutrition_day d
                  on d.nutrition_day_id=e.nutrition_day_id
                where d.owner_user_id=$1::uuid
                  and e.nutrition_entry_id=$2::uuid
                """,
                owner_user_id,
                value.nutrition_entry_id,
            )
            if not entry:
                raise NutritionLogRepositoryError("entry_not_found")
            if entry["my_food_id"] is None:
                raise NutritionLogRepositoryError("entry_not_single_food")

            resolved_qty_g = value.qty_g
            if value.my_food_serving_id is not None:
                serving_grams = await self._connection.fetchval(
                    f"""
                    select grams
                    from {self._nutrition_schema}.my_food_serving
                    where my_food_serving_id=$1::uuid
                      and my_food_id=$2::uuid
                      and is_active
                    """,
                    value.my_food_serving_id,
                    entry["my_food_id"],
                )
                if serving_grams is None:
                    raise NutritionLogRepositoryError("active_serving_not_found")
                resolved_qty_g = float(serving_grams) * float(value.qty_servings)

            return await self._connection.fetchrow(
                f"""
                update {self._nutrition_schema}.nutrition_entry e
                set
                  qty_g = $3,
                  my_food_serving_id = $4::uuid,
                  qty_servings = $5,
                  sort_order = coalesce($6, e.sort_order),
                  notes = coalesce($7, e.notes),
                  updated_at = now()
                from {self._nutrition_schema}.nutrition_day d
                where e.nutrition_day_id = d.nutrition_day_id
                  and d.owner_user_id = $1::uuid
                  and e.nutrition_entry_id = $2::uuid
                returning
                  e.nutrition_entry_id, e.nutrition_day_id, e.meal_id, e.my_food_id,
                  e.qty_g, e.my_food_serving_id, e.qty_servings, e.sort_order, e.notes,
                  e.created_at, e.updated_at
                """,
                owner_user_id,
                value.nutrition_entry_id,
                resolved_qty_g,
                value.my_food_serving_id,
                value.qty_servings if value.my_food_serving_id is not None else None,
                value.sort_order,
                value.notes,
            )

    async def delete_entry(
        self,
        *,
        owner_user_id: str,
        nutrition_entry_id: str,
    ) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            delete from {self._nutrition_schema}.nutrition_entry e
            using {self._nutrition_schema}.nutrition_day d
            where e.nutrition_day_id = d.nutrition_day_id
              and d.owner_user_id = $1::uuid
              and e.nutrition_entry_id = $2::uuid
            returning
              e.nutrition_entry_id, e.nutrition_day_id, e.meal_id, e.my_food_id, e.qty_g, e.my_food_serving_id, e.qty_servings, e.sort_order, e.notes,
              e.created_at, e.updated_at
            """,
            owner_user_id,
            nutrition_entry_id,
        )

    async def set_day_completion(
        self,
        *,
        owner_user_id: str,
        day: date,
        completed: bool,
    ) -> NutritionDayCompletionRows:
        async with self._connection.transaction():
            day_row = await self._connection.fetchrow(
                f"""
                select
                  nutrition_day_id, owner_user_id, day, notes, completed_at,
                  created_at, updated_at
                from {self._nutrition_schema}.nutrition_day
                where owner_user_id=$1::uuid and day=$2::date
                for update
                """,
                owner_user_id,
                day,
            )
            if not day_row:
                raise NutritionLogRepositoryError("nutrition_day_not_found")

            was_completed = day_row["completed_at"] is not None
            if was_completed == completed:
                return NutritionDayCompletionRows(day_row, False)

            updated = await self._connection.fetchrow(
                f"""
                update {self._nutrition_schema}.nutrition_day
                set completed_at = case when $3::boolean then now() else null end
                where nutrition_day_id=$1::uuid
                  and owner_user_id=$2::uuid
                returning
                  nutrition_day_id, owner_user_id, day, notes, completed_at,
                  created_at, updated_at
                """,
                day_row["nutrition_day_id"],
                owner_user_id,
                completed,
            )
            if not updated:
                raise NutritionLogRepositoryError("nutrition_day_completion_conflict")

            await self._connection.execute(
                f"""
                insert into {self._nutrition_schema}.nutrition_day_completion_event
                  (nutrition_day_id, owner_user_id, actor_user_id, action, source)
                values ($1::uuid, $2::uuid, $2::uuid, $3, 'user')
                """,
                day_row["nutrition_day_id"],
                owner_user_id,
                "completed" if completed else "reopened",
            )
            return NutritionDayCompletionRows(updated, True)


@asynccontextmanager
async def lifeswitch_nutrition_log_repository(
    request: Request,
    *,
    connection_factory: ConnectionFactory = connect_lifeswitch,
    nutrition_schema: str | None = None,
    people_schema: str | None = None,
) -> AsyncIterator[PostgresLifeSwitchNutritionLogRepository]:
    resolved_nutrition_schema = resolve_nutrition_schema(nutrition_schema)
    resolved_people_schema = resolve_people_schema(people_schema)
    connection = await connection_factory(request)
    try:
        yield PostgresLifeSwitchNutritionLogRepository(
            connection,
            nutrition_schema=resolved_nutrition_schema,
            people_schema=resolved_people_schema,
        )
    finally:
        await connection.close()
