from __future__ import annotations

"""PostgreSQL effect boundary for LifeSwitch meal plans."""

import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Protocol

from starlette.requests import Request

from seebx.adapters.lifeswitch_postgres import connect_lifeswitch


ConnectionFactory = Callable[[Request], Awaitable[Any]]
OwnerAuthorizer = Callable[[str], None]
ItemAuthorizer = Callable[[Any], None]
IdentifierParser = Callable[[str], str]
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")


def resolve_nutrition_schema(value: str | None = None) -> str:
    candidate = str(
        os.getenv("LIFESWITCH_NUTRITION_SCHEMA", "lifeswitch_nutrition")
        if value is None
        else value
    ).strip()
    if _IDENTIFIER.fullmatch(candidate) is None:
        raise RuntimeError("invalid LIFESWITCH_NUTRITION_SCHEMA")
    return candidate


class MealPlansRepositoryError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class LifeSwitchMealPlansRepository(Protocol):
    async def list_plans(self, *, owner_user_id: str) -> tuple[Any, ...]: ...
    async def create_plan(self, **values: Any) -> Any | None: ...
    async def active_plan_owner(self, *, meal_plan_id: str) -> Any | None: ...
    async def owned_food_is_active(self, *, my_food_id: str, owner_user_id: str) -> bool: ...
    async def serving_is_active(self, *, serving_id: str, my_food_id: str) -> bool: ...
    async def catalog_food_is_approved(self, *, food_id: str) -> bool: ...
    async def create_item(self, **values: Any) -> Any | None: ...
    async def update_item(self, **values: Any) -> Any: ...
    async def plan_owner(self, *, meal_plan_id: str) -> Any | None: ...
    async def delete_item(self, *, meal_plan_id: str, item_id: str) -> Any | None: ...
    async def list_items(self, *, meal_plan_id: str) -> tuple[Any, ...]: ...


class PostgresLifeSwitchMealPlansRepository:
    """Own all retained meal-plan SQL and its one transaction."""

    def __init__(self, connection: Any, *, nutrition_schema: str) -> None:
        self._connection = connection
        self._schema = resolve_nutrition_schema(nutrition_schema)

    async def list_plans(self, *, owner_user_id: str) -> tuple[Any, ...]:
        rows = await self._connection.fetch(
            f"""
            select meal_plan_id, owner_user_id, name, goal,
                   target_kcal, target_protein_g, target_carbs_g, target_fat_g,
                   is_active, created_at, updated_at
            from {self._schema}.meal_plan
            where owner_user_id = $1::uuid
            order by updated_at desc
            """,
            owner_user_id,
        )
        return tuple(rows)

    async def create_plan(
        self,
        *,
        owner_user_id: str,
        name: str,
        goal: str,
        target_kcal: float | None,
        target_protein_g: float | None,
        target_carbs_g: float | None,
        target_fat_g: float | None,
    ) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            insert into {self._schema}.meal_plan
              (owner_user_id, name, goal, target_kcal, target_protein_g, target_carbs_g, target_fat_g)
            values
              ($1::uuid, $2, $3, $4, $5, $6, $7)
            on conflict (owner_user_id, name) do update
              set goal=excluded.goal,
                  target_kcal=excluded.target_kcal,
                  target_protein_g=excluded.target_protein_g,
                  target_carbs_g=excluded.target_carbs_g,
                  target_fat_g=excluded.target_fat_g,
                  updated_at=now(),
                  is_active=true
            returning meal_plan_id, owner_user_id, name, goal,
                      target_kcal, target_protein_g, target_carbs_g, target_fat_g,
                      is_active, created_at, updated_at
            """,
            owner_user_id,
            name,
            goal,
            target_kcal,
            target_protein_g,
            target_carbs_g,
            target_fat_g,
        )

    async def active_plan_owner(self, *, meal_plan_id: str) -> Any | None:
        return await self._connection.fetchval(
            f"select owner_user_id from {self._schema}.meal_plan where meal_plan_id=$1::uuid and is_active",
            meal_plan_id,
        )

    async def owned_food_is_active(self, *, my_food_id: str, owner_user_id: str) -> bool:
        value = await self._connection.fetchval(
            f"select is_active from {self._schema}.my_food where my_food_id=$1::uuid and owner_user_id=$2::uuid",
            my_food_id,
            owner_user_id,
        )
        return value is True

    async def serving_is_active(self, *, serving_id: str, my_food_id: str) -> bool:
        value = await self._connection.fetchval(
            f"""
            select 1
            from {self._schema}.my_food_serving
            where my_food_serving_id=$1::uuid
              and my_food_id=$2::uuid
              and is_active
            """,
            serving_id,
            my_food_id,
        )
        return bool(value)

    async def catalog_food_is_approved(self, *, food_id: str) -> bool:
        value = await self._connection.fetchval(
            "select (is_public and is_active) from catalog_dev.food where food_id=$1::uuid",
            food_id,
        )
        return value is True

    async def create_item(
        self,
        *,
        meal_plan_id: str,
        meal_label: str,
        sort_order: int,
        my_food_id: str | None,
        food_id: str | None,
        qty_g: float | None,
        serving_id: str | None,
        qty_servings: float | None,
        notes: str | None,
    ) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            insert into {self._schema}.meal_plan_item
              (meal_plan_id, meal_label, sort_order, my_food_id, food_id,
               qty_g, my_food_serving_id, qty_servings, notes)
            values
              ($1::uuid, $2, $3, $4::uuid, $5::uuid, $6, $7::uuid, $8, $9)
            returning meal_plan_item_id, meal_plan_id, meal_label, sort_order,
                      my_food_id, food_id, qty_g, my_food_serving_id, qty_servings, notes,
                      created_at, updated_at
            """,
            meal_plan_id,
            meal_label,
            sort_order,
            my_food_id,
            food_id,
            qty_g,
            serving_id,
            qty_servings,
            notes,
        )

    async def update_item(
        self,
        *,
        meal_plan_id: str,
        item_id: str,
        meal_label: str,
        qty_g: float | None,
        raw_serving_id: str | None,
        qty_servings: float | None,
        use_grams: bool,
        use_serving: bool,
        authorize_item: ItemAuthorizer,
        parse_serving_id: IdentifierParser,
    ) -> Any:
        async with self._connection.transaction():
            item = await self._connection.fetchrow(
                f"""
                select i.my_food_id, i.food_id, p.owner_user_id
                from {self._schema}.meal_plan_item i
                join {self._schema}.meal_plan p on p.meal_plan_id=i.meal_plan_id
                where i.meal_plan_id=$1::uuid
                  and i.meal_plan_item_id=$2::uuid
                """,
                meal_plan_id,
                item_id,
            )
            if not item:
                raise MealPlansRepositoryError("item_not_found")
            authorize_item(item)

            serving_id = None
            if use_serving:
                serving_id = parse_serving_id(str(raw_serving_id))
                serving_ok = await self._connection.fetchval(
                    f"""
                    select 1
                    from {self._schema}.my_food_serving
                    where my_food_serving_id=$1::uuid
                      and my_food_id=$2::uuid
                      and is_active
                    """,
                    serving_id,
                    item["my_food_id"],
                )
                if not serving_ok:
                    raise MealPlansRepositoryError("serving_not_found")

            return await self._connection.fetchrow(
                f"""
                update {self._schema}.meal_plan_item
                set meal_label=$3,
                    qty_g=$4,
                    my_food_serving_id=$5::uuid,
                    qty_servings=$6,
                    updated_at=now()
                where meal_plan_id=$1::uuid
                  and meal_plan_item_id=$2::uuid
                returning meal_plan_item_id, meal_plan_id, meal_label, sort_order,
                          my_food_id, food_id, qty_g, my_food_serving_id, qty_servings,
                          notes, created_at, updated_at
                """,
                meal_plan_id,
                item_id,
                meal_label,
                qty_g if use_grams else None,
                serving_id,
                qty_servings if use_serving else None,
            )

    async def plan_owner(self, *, meal_plan_id: str) -> Any | None:
        return await self._connection.fetchval(
            f"select owner_user_id from {self._schema}.meal_plan where meal_plan_id=$1::uuid",
            meal_plan_id,
        )

    async def delete_item(self, *, meal_plan_id: str, item_id: str) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            delete from {self._schema}.meal_plan_item
            where meal_plan_id=$1::uuid
              and meal_plan_item_id=$2::uuid
            returning meal_plan_item_id
            """,
            meal_plan_id,
            item_id,
        )

    async def list_items(self, *, meal_plan_id: str) -> tuple[Any, ...]:
        rows = await self._connection.fetch(
            f"""
            select
              i.meal_plan_item_id, i.meal_plan_id, i.meal_label, i.sort_order,
              i.my_food_id, i.food_id, i.qty_g, i.my_food_serving_id, i.qty_servings, i.notes,
              s.name as serving_name,
              s.grams as serving_grams,
              coalesce(i.qty_g, s.grams * i.qty_servings) as qty_g_resolved,
              coalesce(m.display_name, f.display_name) as display_name,
              coalesce(m.brand, f.brand) as brand,
              coalesce(m.kcal, f.kcal) as kcal,
              coalesce(m.protein_g, f.protein_g) as protein_g,
              coalesce(m.carbs_g, f.carbs_g) as carbs_g,
              coalesce(m.fat_g, f.fat_g) as fat_g,
              i.created_at, i.updated_at
            from {self._schema}.meal_plan_item i
            left join {self._schema}.my_food m on m.my_food_id = i.my_food_id
            left join {self._schema}.my_food_serving s
              on s.my_food_serving_id = i.my_food_serving_id
             and s.my_food_id = i.my_food_id
            left join catalog_dev.food f on f.food_id = i.food_id
            where i.meal_plan_id = $1::uuid
            order by i.meal_label, i.sort_order, i.created_at
            """,
            meal_plan_id,
        )
        return tuple(rows)


@asynccontextmanager
async def lifeswitch_meal_plans_repository(
    request: Request,
    *,
    connection_factory: ConnectionFactory = connect_lifeswitch,
    nutrition_schema: str | None = None,
) -> AsyncIterator[PostgresLifeSwitchMealPlansRepository]:
    resolved_schema = resolve_nutrition_schema(nutrition_schema)
    connection = await connection_factory(request)
    try:
        yield PostgresLifeSwitchMealPlansRepository(
            connection,
            nutrition_schema=resolved_schema,
        )
    finally:
        await connection.close()
