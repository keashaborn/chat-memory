from __future__ import annotations

"""PostgreSQL read boundary for LifeSwitch nutrition logs."""

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


class LifeSwitchNutritionLogReadRepository(Protocol):
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


class PostgresLifeSwitchNutritionLogReadRepository:
    """Own delegated permission and the two retained nutrition-log read projections."""

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


@asynccontextmanager
async def lifeswitch_nutrition_log_read_repository(
    request: Request,
    *,
    connection_factory: ConnectionFactory = connect_lifeswitch,
    nutrition_schema: str | None = None,
    people_schema: str | None = None,
) -> AsyncIterator[PostgresLifeSwitchNutritionLogReadRepository]:
    resolved_nutrition_schema = resolve_nutrition_schema(nutrition_schema)
    resolved_people_schema = resolve_people_schema(people_schema)
    connection = await connection_factory(request)
    try:
        yield PostgresLifeSwitchNutritionLogReadRepository(
            connection,
            nutrition_schema=resolved_nutrition_schema,
            people_schema=resolved_people_schema,
        )
    finally:
        await connection.close()
