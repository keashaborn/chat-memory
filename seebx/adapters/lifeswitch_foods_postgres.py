from __future__ import annotations

"""PostgreSQL effect boundary for LifeSwitch foods, servings, and overrides."""

import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from starlette.requests import Request

from seebx.adapters.lifeswitch_postgres import connect_lifeswitch


_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")


def _resolve_schema(environment_name: str, default: str, value: str | None) -> str:
    candidate = str(os.getenv(environment_name, default) if value is None else value).strip()
    if _IDENTIFIER.fullmatch(candidate) is None:
        raise RuntimeError(f"invalid {environment_name}")
    return candidate


def resolve_nutrition_schema(value: str | None = None) -> str:
    return _resolve_schema("LIFESWITCH_NUTRITION_SCHEMA", "lifeswitch_nutrition", value)


def resolve_catalog_schema(value: str | None = None) -> str:
    return _resolve_schema("CATALOG_SCHEMA", "catalog_dev", value)


class FoodsRepositoryError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


MY_FOOD_RETURN_COLUMNS = """
  f.my_food_id, f.owner_user_id, f.display_name, f.source_display_name,
  f.brand, f.variant, f.source_type, f.source_food_id, f.source, f.source_id,
  f.barcode, f.basis, f.kcal, f.protein_g, f.carbs_g, f.fat_g,
  f.fiber_g, f.sugar_g, f.sodium_mg, f.nutrient_source,
  f.nutrient_source_detail, f.nutrient_updated_at, f.preferred_mode,
  f.preferred_quantity, f.preferred_serving_id,
  ps.name as preferred_serving_name, ps.grams as preferred_serving_grams,
  f.is_verified, f.is_active, f.created_at, f.updated_at
"""


class PostgresLifeSwitchFoodsRepository:
    """Own all retained food, serving, and override SQL effects."""

    def __init__(
        self,
        connection: Any,
        *,
        nutrition_schema: str,
        catalog_schema: str,
    ) -> None:
        self._connection = connection
        self._schema = resolve_nutrition_schema(nutrition_schema)
        self._catalog_schema = resolve_catalog_schema(catalog_schema)

    @asynccontextmanager
    async def transaction(
        self, *, translate_unique_violation: bool = False
    ) -> AsyncIterator[None]:
        try:
            async with self._connection.transaction():
                yield
        except asyncpg.UniqueViolationError as error:
            if translate_unique_violation:
                raise FoodsRepositoryError("serving_name_conflict") from error
            raise

    async def find_active_usda_food(
        self, *, owner_user_id: str, source_id: str, variant: str | None
    ) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            select my_food_id, nutrient_source
            from {self._schema}.my_food
            where owner_user_id=$1::uuid
              and source_type='usda'
              and source_id=$2
              and coalesce(variant,'')=coalesce($3,'')
              and is_active
            limit 1
            """,
            owner_user_id,
            source_id,
            variant,
        )

    async def upsert_usda_food(
        self,
        *,
        owner_user_id: str,
        display_name: str,
        brand: str | None,
        variant: str | None,
        source_id: str,
        barcode: str | None,
        kcal: float | None,
        protein_g: float | None,
        carbs_g: float | None,
        fat_g: float | None,
        fiber_g: float | None,
        sugar_g: float | None,
        sodium_mg: float | None,
    ) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            insert into {self._schema}.my_food as current
              (owner_user_id, display_name, source_display_name, brand, variant,
               source_type, source_food_id, source, source_id, barcode,
               basis, kcal, protein_g, carbs_g, fat_g, fiber_g, sugar_g, sodium_mg,
               nutrient_source, nutrient_updated_at, is_verified, is_active)
            values
              ($1::uuid, $2, $2, $3, $4,
               'usda', null, 'usda_fdc', $5, $6,
               'per_100g', $7, $8, $9, $10, $11, $12, $13,
               'usda', now(), true, true)
            on conflict (owner_user_id, source_type, source_id, coalesce(variant,''))
            where is_active
            do update set
              source_display_name = excluded.source_display_name,
              brand = excluded.brand,
              barcode = excluded.barcode,
              basis = excluded.basis,
              kcal = case when current.nutrient_source='usda' then excluded.kcal else current.kcal end,
              protein_g = case when current.nutrient_source='usda' then excluded.protein_g else current.protein_g end,
              carbs_g = case when current.nutrient_source='usda' then excluded.carbs_g else current.carbs_g end,
              fat_g = case when current.nutrient_source='usda' then excluded.fat_g else current.fat_g end,
              fiber_g = case when current.nutrient_source='usda' then excluded.fiber_g else current.fiber_g end,
              sugar_g = case when current.nutrient_source='usda' then excluded.sugar_g else current.sugar_g end,
              sodium_mg = case when current.nutrient_source='usda' then excluded.sodium_mg else current.sodium_mg end,
              nutrient_updated_at = case when current.nutrient_source='usda' then now() else current.nutrient_updated_at end,
              is_verified = case when current.nutrient_source='usda' then true else current.is_verified end,
              is_active = true,
              updated_at = now()
            returning my_food_id
            """,
            owner_user_id,
            display_name,
            brand,
            variant,
            source_id,
            barcode,
            kcal,
            protein_g,
            carbs_g,
            fat_g,
            fiber_g,
            sugar_g,
            sodium_mg,
        )

    async def find_serving_by_name(self, *, my_food_id: Any, name: str) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            select my_food_serving_id, source_type
            from {self._schema}.my_food_serving
            where my_food_id=$1::uuid and lower(name)=lower($2)
            limit 1
            """,
            my_food_id,
            name,
        )

    async def has_active_default_serving(self, *, my_food_id: Any) -> bool:
        value = await self._connection.fetchval(
            f"select 1 from {self._schema}.my_food_serving where my_food_id=$1::uuid and is_default and is_active",
            my_food_id,
        )
        return bool(value)

    async def clear_default_servings(self, *, my_food_id: Any) -> None:
        await self._connection.execute(
            f"update {self._schema}.my_food_serving set is_default=false, updated_at=now() where my_food_id=$1::uuid and is_default",
            my_food_id,
        )

    async def update_imported_serving(
        self, *, serving_id: Any, grams: float, source_label: str, is_default: bool
    ) -> None:
        await self._connection.execute(
            f"""
            update {self._schema}.my_food_serving
            set grams=case when source_type in ('usda','legacy') then $2 else grams end,
                source_type=case when source_type in ('usda','legacy') then 'usda' else source_type end,
                source_label=coalesce(source_label, $3),
                is_active=true,
                is_default=case when $4::bool then true else is_default end,
                updated_at=now()
            where my_food_serving_id=$1::uuid
            """,
            serving_id,
            grams,
            source_label,
            is_default,
        )

    async def create_imported_serving(
        self, *, my_food_id: Any, name: str, grams: float, is_default: bool
    ) -> Any | None:
        return await self._connection.fetchval(
            f"""
            insert into {self._schema}.my_food_serving
              (my_food_id, name, grams, is_default, source_type, source_label, is_active)
            values ($1::uuid, $2, $3, $4, 'usda', $2, true)
            returning my_food_serving_id
            """,
            my_food_id,
            name,
            grams,
            is_default,
        )

    async def set_preferred_serving(self, *, my_food_id: Any, serving_id: Any) -> None:
        await self._connection.execute(
            f"""
            update {self._schema}.my_food
            set preferred_mode='serving', preferred_quantity=1,
                preferred_serving_id=$2::uuid, updated_at=now()
            where my_food_id=$1::uuid
            """,
            my_food_id,
            serving_id,
        )

    async def get_food_with_preferred(self, *, my_food_id: Any) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            select {MY_FOOD_RETURN_COLUMNS}
            from {self._schema}.my_food f
            left join {self._schema}.my_food_serving ps
              on ps.my_food_serving_id=f.preferred_serving_id
             and ps.my_food_id=f.my_food_id
            where f.my_food_id=$1::uuid
            """,
            my_food_id,
        )

    async def list_foods(
        self, *, owner_user_id: str, query: str | None, include_inactive: bool
    ) -> tuple[Any, ...]:
        where = "f.owner_user_id = $1::uuid"
        arguments: list[object] = [owner_user_id]
        if not include_inactive:
            where += " and f.is_active"
        if query:
            where += " and (f.display_name ilike $2 or coalesce(f.brand,'') ilike $2 or coalesce(f.variant,'') ilike $2)"
            arguments.append(f"%{query}%")
        rows = await self._connection.fetch(
            f"""
            select {MY_FOOD_RETURN_COLUMNS}
            from {self._schema}.my_food f
            left join {self._schema}.my_food_serving ps
              on ps.my_food_serving_id = f.preferred_serving_id
             and ps.my_food_id = f.my_food_id
            where {where}
            order by lower(f.display_name), lower(coalesce(f.brand,'')), lower(coalesce(f.variant,''))
            """,
            *arguments,
        )
        return tuple(rows)

    async def get_public_catalog_food(self, *, food_id: str) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            select food_id, display_name, brand, barcode, source, source_id, basis,
                   kcal, protein_g, carbs_g, fat_g, fiber_g, sugar_g, sodium_mg
            from {self._catalog_schema}.food
            where food_id = $1::uuid and is_public and is_active
            limit 1
            """,
            food_id,
        )

    async def upsert_catalog_food(
        self,
        *,
        owner_user_id: str,
        display_name: str,
        source_display_name: str,
        brand: str | None,
        variant: str | None,
        source_food_id: str,
        source: Any,
        source_id: str,
        barcode: Any,
        basis: Any,
        kcal: Any,
        protein_g: Any,
        carbs_g: Any,
        fat_g: Any,
        fiber_g: Any,
        sugar_g: Any,
        sodium_mg: Any,
    ) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            insert into {self._schema}.my_food as current
              (owner_user_id, display_name, source_display_name, brand, variant,
               source_type, source_food_id, source, source_id, barcode,
               basis, kcal, protein_g, carbs_g, fat_g, fiber_g, sugar_g, sodium_mg,
               nutrient_source, nutrient_updated_at, is_verified, is_active)
            values
              ($1::uuid, $2, $3, $4, $5,
               'catalog', $6::uuid, $7, $8, $9,
               $10, $11, $12, $13, $14, $15, $16, $17,
               'catalog', now(), true, true)
            on conflict (owner_user_id, source_type, source_id, coalesce(variant,''))
            where is_active
            do update set
              source_display_name = excluded.source_display_name,
              brand = excluded.brand,
              barcode = excluded.barcode,
              basis = excluded.basis,
              kcal = case when current.nutrient_source='catalog' then excluded.kcal else current.kcal end,
              protein_g = case when current.nutrient_source='catalog' then excluded.protein_g else current.protein_g end,
              carbs_g = case when current.nutrient_source='catalog' then excluded.carbs_g else current.carbs_g end,
              fat_g = case when current.nutrient_source='catalog' then excluded.fat_g else current.fat_g end,
              fiber_g = case when current.nutrient_source='catalog' then excluded.fiber_g else current.fiber_g end,
              sugar_g = case when current.nutrient_source='catalog' then excluded.sugar_g else current.sugar_g end,
              sodium_mg = case when current.nutrient_source='catalog' then excluded.sodium_mg else current.sodium_mg end,
              nutrient_updated_at = case when current.nutrient_source='catalog' then now() else current.nutrient_updated_at end,
              is_active = true,
              updated_at = now()
            returning my_food_id
            """,
            owner_user_id,
            display_name,
            source_display_name,
            brand,
            variant,
            source_food_id,
            source,
            source_id,
            barcode,
            basis,
            kcal,
            protein_g,
            carbs_g,
            fat_g,
            fiber_g,
            sugar_g,
            sodium_mg,
        )

    async def lock_food(self, *, my_food_id: str) -> Any | None:
        return await self._connection.fetchrow(
            f"select * from {self._schema}.my_food where my_food_id=$1::uuid for update",
            my_food_id,
        )

    async def active_serving_belongs_to_food(self, *, serving_id: str, my_food_id: str) -> bool:
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

    async def update_food_record(self, *, my_food_id: str, values: tuple[Any, ...]) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            update {self._schema}.my_food
            set display_name=$2,
                brand=$3,
                variant=$4,
                barcode=$5,
                kcal=$6,
                protein_g=$7,
                carbs_g=$8,
                fat_g=$9,
                fiber_g=$10,
                sugar_g=$11,
                sodium_mg=$12,
                nutrient_source=$13,
                nutrient_source_detail=$14,
                nutrient_updated_at=case when $15::bool then now() else nutrient_updated_at end,
                preferred_mode=$16,
                preferred_quantity=$17,
                preferred_serving_id=$18::uuid,
                is_verified=$19,
                updated_at=now()
            where my_food_id=$1::uuid
            returning my_food_id
            """,
            my_food_id,
            *values,
        )

    async def food_owner(self, *, my_food_id: str) -> Any | None:
        return await self._connection.fetchval(
            f"select owner_user_id from {self._schema}.my_food where my_food_id=$1::uuid",
            my_food_id,
        )

    async def deactivate_food(self, *, my_food_id: str) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            update {self._schema}.my_food
            set is_active=false, updated_at=now()
            where my_food_id=$1::uuid
            returning my_food_id, owner_user_id, display_name, brand, variant, source_type, source_id, is_active, updated_at
            """,
            my_food_id,
        )

    async def food_owner_and_active(self, *, my_food_id: str) -> Any | None:
        return await self._connection.fetchrow(
            f"select owner_user_id, is_active from {self._schema}.my_food where my_food_id=$1::uuid",
            my_food_id,
        )

    async def list_active_servings(self, *, my_food_id: str) -> tuple[Any, ...]:
        rows = await self._connection.fetch(
            f"""
            select my_food_serving_id, my_food_id, name, grams, is_default,
                   source_type, source_label, is_active, created_at, updated_at
            from {self._schema}.my_food_serving
            where my_food_id = $1::uuid and is_active
            order by is_default desc, lower(name), grams
            """,
            my_food_id,
        )
        return tuple(rows)

    async def find_serving_record_by_name(self, *, my_food_id: str, name: str) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            select my_food_serving_id, my_food_id, name, grams, is_default,
                   source_type, source_label, is_active, created_at, updated_at
            from {self._schema}.my_food_serving
            where my_food_id=$1::uuid
              and lower(name)=lower($2)
            order by updated_at desc nulls last, created_at desc
            limit 1
            """,
            my_food_id,
            name,
        )

    async def update_manual_serving(
        self, *, serving_id: Any, name: str, grams: float
    ) -> None:
        await self._connection.execute(
            f"""
            update {self._schema}.my_food_serving
            set name=$2,
                grams=$3,
                source_type='manual',
                source_label=coalesce(source_label, name),
                is_active=true,
                updated_at=now()
            where my_food_serving_id=$1::uuid
            """,
            serving_id,
            name,
            grams,
        )

    async def set_default_serving(self, *, serving_id: Any) -> None:
        await self._connection.execute(
            f"""
            update {self._schema}.my_food_serving
            set is_default=true, updated_at=now()
            where my_food_serving_id=$1::uuid
            """,
            serving_id,
        )

    async def get_serving(self, *, serving_id: Any) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            select my_food_serving_id, my_food_id, name, grams, is_default,
                   source_type, source_label, is_active, created_at, updated_at
            from {self._schema}.my_food_serving
            where my_food_serving_id=$1::uuid
            """,
            serving_id,
        )

    async def create_manual_serving(
        self, *, my_food_id: str, name: str, grams: float, is_default: bool
    ) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            insert into {self._schema}.my_food_serving
              (my_food_id, name, grams, is_default, source_type, source_label, is_active)
            values ($1::uuid, $2, $3, $4::bool, 'manual', $2, true)
            returning my_food_serving_id, my_food_id, name, grams, is_default,
                      source_type, source_label, is_active, created_at, updated_at
            """,
            my_food_id,
            name,
            grams,
            is_default,
        )

    async def lock_serving(self, *, serving_id: str, my_food_id: str) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            select s.*, f.owner_user_id, f.preferred_serving_id
            from {self._schema}.my_food_serving s
            join {self._schema}.my_food f on f.my_food_id=s.my_food_id
            where s.my_food_serving_id=$1::uuid
              and s.my_food_id=$2::uuid
            for update of s, f
            """,
            serving_id,
            my_food_id,
        )

    async def update_serving_record(
        self,
        *,
        serving_id: str,
        my_food_id: str,
        name: str,
        grams: float,
        is_active: bool,
        manually_changed: bool,
    ) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            update {self._schema}.my_food_serving
            set name=$3,
                grams=$4,
                is_active=$5,
                source_type=case when $6::bool then 'manual' else source_type end,
                source_label=coalesce(source_label, name),
                is_default=case when $5::bool then is_default else false end,
                updated_at=now()
            where my_food_serving_id=$1::uuid
              and my_food_id=$2::uuid
            returning my_food_serving_id, my_food_id, name, grams, is_default,
                      source_type, source_label, is_active, created_at, updated_at
            """,
            serving_id,
            my_food_id,
            name,
            grams,
            is_active,
            manually_changed,
        )

    async def clear_other_default_servings(
        self, *, my_food_id: str, serving_id: str
    ) -> None:
        await self._connection.execute(
            f"update {self._schema}.my_food_serving set is_default=false, updated_at=now() where my_food_id=$1::uuid and my_food_serving_id<>$2::uuid and is_default",
            my_food_id,
            serving_id,
        )

    async def set_grams_preference(
        self, *, my_food_id: str, preferred_quantity: float
    ) -> None:
        await self._connection.execute(
            f"""
            update {self._schema}.my_food
            set preferred_mode='grams', preferred_quantity=$2,
                preferred_serving_id=null, updated_at=now()
            where my_food_id=$1::uuid
            """,
            my_food_id,
            preferred_quantity,
        )

    async def clear_serving_default(self, *, serving_id: str) -> None:
        await self._connection.execute(
            f"update {self._schema}.my_food_serving set is_default=false, updated_at=now() where my_food_serving_id=$1::uuid",
            serving_id,
        )

    async def list_overrides(self, *, owner_user_id: str) -> tuple[Any, ...]:
        rows = await self._connection.fetch(
            f"""
            select owner_user_id, my_food_id, alias, default_grams, sort_order, created_at, updated_at
            from {self._schema}.my_food_override
            where owner_user_id = $1::uuid
            order by sort_order asc, updated_at desc nulls last, created_at desc
            """,
            owner_user_id,
        )
        return tuple(rows)

    async def food_is_active(self, *, my_food_id: str) -> bool:
        value = await self._connection.fetchval(
            f"select is_active from {self._schema}.my_food where my_food_id=$1::uuid",
            my_food_id,
        )
        return value is True

    async def upsert_override(
        self,
        *,
        owner_user_id: str,
        my_food_id: str,
        alias: str | None,
        default_grams: float | None,
        sort_order: int,
    ) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            insert into {self._schema}.my_food_override
              (owner_user_id, my_food_id, alias, default_grams, sort_order)
            values
              ($1::uuid, $2::uuid, $3, $4, $5)
            on conflict (owner_user_id, my_food_id) do update
              set
                alias = coalesce(excluded.alias, {self._schema}.my_food_override.alias),
                default_grams = coalesce(excluded.default_grams, {self._schema}.my_food_override.default_grams),
                sort_order = excluded.sort_order,
                updated_at = now()
            returning owner_user_id, my_food_id, alias, default_grams, sort_order, created_at, updated_at
            """,
            owner_user_id,
            my_food_id,
            alias,
            default_grams,
            sort_order,
        )

    async def delete_override(self, *, owner_user_id: str, my_food_id: str) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            delete from {self._schema}.my_food_override
            where owner_user_id=$1::uuid and my_food_id=$2::uuid
            returning owner_user_id, my_food_id
            """,
            owner_user_id,
            my_food_id,
        )


@asynccontextmanager
async def lifeswitch_foods_repository(
    request: Request,
    *,
    connection_factory=connect_lifeswitch,
    nutrition_schema: str | None = None,
    catalog_schema: str | None = None,
) -> AsyncIterator[PostgresLifeSwitchFoodsRepository]:
    connection = await connection_factory(request)
    try:
        yield PostgresLifeSwitchFoodsRepository(
            connection,
            nutrition_schema=resolve_nutrition_schema(nutrition_schema),
            catalog_schema=resolve_catalog_schema(catalog_schema),
        )
    finally:
        await connection.close()
