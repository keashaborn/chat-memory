from __future__ import annotations

"""PostgreSQL effect boundary for LifeSwitch meal templates."""

import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Protocol

from starlette.requests import Request

from seebx.adapters.lifeswitch_postgres import connect_lifeswitch


ConnectionFactory = Callable[[Request], Awaitable[Any]]
OwnerAuthorizer = Callable[[str], None]
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


class MealsRepositoryError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class LifeSwitchMealsRepository(Protocol):
    async def list_meals(self, *, owner_user_id: str, include_inactive: bool) -> tuple[Any, ...]: ...
    async def create_meal(self, *, owner_user_id: str, name: str, meal_type: str) -> Any | None: ...
    async def deactivate_meal(self, *, meal_id: str, owner_user_id: str) -> Any | None: ...
    async def active_meal_owner(self, *, meal_id: str) -> Any | None: ...
    async def food_is_active(self, *, my_food_id: str, owner_user_id: str) -> bool: ...
    async def serving_is_active(self, *, serving_id: str, my_food_id: str) -> bool: ...
    async def create_item(self, **values: Any) -> Any | None: ...
    async def update_item(self, **values: Any) -> Any: ...
    async def meal_owner(self, *, meal_id: str) -> Any | None: ...
    async def list_items(self, *, meal_id: str) -> tuple[Any, ...]: ...
    async def delete_item(self, *, meal_item_id: str, meal_id: str, owner_user_id: str) -> Any | None: ...


class PostgresLifeSwitchMealsRepository:
    """Own all retained meal-template SQL and its one transaction."""

    def __init__(self, connection: Any, *, nutrition_schema: str) -> None:
        self._connection = connection
        self._schema = resolve_nutrition_schema(nutrition_schema)

    async def list_meals(self, *, owner_user_id: str, include_inactive: bool) -> tuple[Any, ...]:
        where = "owner_user_id=$1::uuid"
        if not include_inactive:
            where += " and is_active"
        rows = await self._connection.fetch(
            f"""
            select meal_id, owner_user_id, name, meal_type, is_active, created_at, updated_at
            from {self._schema}.meal
            where {where}
            order by lower(meal_type), lower(name)
            """,
            owner_user_id,
        )
        return tuple(rows)

    async def create_meal(self, *, owner_user_id: str, name: str, meal_type: str) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            insert into {self._schema}.meal (owner_user_id, name, meal_type)
            values ($1::uuid, $2, $3)
            on conflict (owner_user_id, name) do update
              set meal_type=excluded.meal_type,
                  is_active=true,
                  updated_at=now()
            returning meal_id, owner_user_id, name, meal_type, is_active, created_at, updated_at
            """,
            owner_user_id,
            name,
            meal_type,
        )

    async def deactivate_meal(self, *, meal_id: str, owner_user_id: str) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            update {self._schema}.meal
               set is_active=false, updated_at=now()
             where meal_id=$1::uuid
               and owner_user_id=$2::uuid
            returning meal_id, owner_user_id, name, meal_type, is_active, created_at, updated_at
            """,
            meal_id,
            owner_user_id,
        )

    async def active_meal_owner(self, *, meal_id: str) -> Any | None:
        return await self._connection.fetchval(
            f"select owner_user_id from {self._schema}.meal where meal_id=$1::uuid and is_active",
            meal_id,
        )

    async def food_is_active(self, *, my_food_id: str, owner_user_id: str) -> bool:
        value = await self._connection.fetchval(
            f"select is_active from {self._schema}.my_food where my_food_id=$1::uuid and owner_user_id=$2::uuid",
            my_food_id,
            owner_user_id,
        )
        return value is True

    async def serving_is_active(self, *, serving_id: str, my_food_id: str) -> bool:
        value = await self._connection.fetchval(
            f"select 1 from {self._schema}.my_food_serving where my_food_serving_id=$1::uuid and my_food_id=$2::uuid and is_active",
            serving_id,
            my_food_id,
        )
        return value == 1

    async def create_item(
        self,
        *,
        meal_id: str,
        my_food_id: str,
        qty_g: float | None,
        serving_id: str | None,
        qty_servings: float | None,
        sort_order: int,
        notes: str | None,
    ) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            insert into {self._schema}.meal_item
              (meal_id, my_food_id, qty_g, my_food_serving_id, qty_servings, sort_order, notes)
            values
              ($1::uuid, $2::uuid, $3, $4::uuid, $5, $6, $7)
            returning meal_item_id, meal_id, my_food_id,
                      qty_g, my_food_serving_id, qty_servings,
                      sort_order, notes, created_at, updated_at
            """,
            meal_id,
            my_food_id,
            qty_g,
            serving_id,
            qty_servings,
            sort_order,
            notes,
        )

    async def update_item(
        self,
        *,
        meal_id: str,
        meal_item_id: str,
        qty_g: float | None,
        serving_id: str | None,
        qty_servings: float | None,
        authorize_owner: OwnerAuthorizer,
    ) -> Any:
        async with self._connection.transaction():
            current = await self._connection.fetchrow(
                f"""
                select i.my_food_id, m.owner_user_id
                from {self._schema}.meal_item i
                join {self._schema}.meal m on m.meal_id=i.meal_id
                where i.meal_item_id=$1::uuid
                  and i.meal_id=$2::uuid
                  and m.is_active=true
                """,
                meal_item_id,
                meal_id,
            )
            if not current:
                raise MealsRepositoryError("meal_item_not_found_or_inactive")
            authorize_owner(str(current["owner_user_id"]))

            if serving_id:
                owns = await self._connection.fetchval(
                    f"""
                    select 1
                    from {self._schema}.my_food_serving
                    where my_food_serving_id=$1::uuid
                      and my_food_id=$2::uuid
                      and is_active=true
                    """,
                    serving_id,
                    current["my_food_id"],
                )
                if owns != 1:
                    raise MealsRepositoryError("serving_not_found_for_item")

            return await self._connection.fetchrow(
                f"""
                update {self._schema}.meal_item
                set qty_g=$3,
                    my_food_serving_id=$4::uuid,
                    qty_servings=$5,
                    updated_at=now()
                where meal_item_id=$1::uuid
                  and meal_id=$2::uuid
                returning meal_item_id, meal_id, my_food_id,
                          qty_g, my_food_serving_id, qty_servings,
                          sort_order, notes, created_at, updated_at
                """,
                meal_item_id,
                meal_id,
                qty_g,
                serving_id,
                qty_servings,
            )

    async def meal_owner(self, *, meal_id: str) -> Any | None:
        return await self._connection.fetchval(
            f"select owner_user_id from {self._schema}.meal where meal_id=$1::uuid",
            meal_id,
        )

    async def list_items(self, *, meal_id: str) -> tuple[Any, ...]:
        rows = await self._connection.fetch(
            f"""
            select
              i.meal_item_id, i.meal_id, i.my_food_id,
              i.qty_g, i.my_food_serving_id, i.qty_servings,
              coalesce(i.qty_g, (s.grams * i.qty_servings)) as qty_g_resolved,
              s.name as serving_name, s.grams as serving_grams,
              i.sort_order, i.notes,

              f.display_name, f.brand, f.variant,
              f.kcal, f.protein_g, f.carbs_g, f.fat_g,

              i.created_at, i.updated_at
            from {self._schema}.meal_item i
            join {self._schema}.my_food f on f.my_food_id = i.my_food_id
            left join {self._schema}.my_food_serving s on s.my_food_serving_id = i.my_food_serving_id
            where i.meal_id = $1::uuid
            order by i.sort_order, i.created_at
            """,
            meal_id,
        )
        return tuple(rows)

    async def delete_item(
        self,
        *,
        meal_item_id: str,
        meal_id: str,
        owner_user_id: str,
    ) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            delete from {self._schema}.meal_item i
            using {self._schema}.meal m
            where i.meal_item_id = $1::uuid
              and i.meal_id = $2::uuid
              and m.meal_id = i.meal_id
              and m.owner_user_id = $3::uuid
            returning
              i.meal_item_id, i.meal_id, i.my_food_id,
              i.qty_g, i.my_food_serving_id, i.qty_servings,
              i.sort_order, i.notes, i.created_at, i.updated_at
            """,
            meal_item_id,
            meal_id,
            owner_user_id,
        )


@asynccontextmanager
async def lifeswitch_meals_repository(
    request: Request,
    *,
    connection_factory: ConnectionFactory = connect_lifeswitch,
    nutrition_schema: str | None = None,
) -> AsyncIterator[PostgresLifeSwitchMealsRepository]:
    resolved_schema = resolve_nutrition_schema(nutrition_schema)
    connection = await connection_factory(request)
    try:
        yield PostgresLifeSwitchMealsRepository(
            connection,
            nutrition_schema=resolved_schema,
        )
    finally:
        await connection.close()
