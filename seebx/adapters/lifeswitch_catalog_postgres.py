from __future__ import annotations

"""Read-only PostgreSQL boundary for the shared LifeSwitch catalog."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from seebx.adapters.lifeswitch_postgres import resolve_lifeswitch_postgres_dsn


ConnectionFactory = Callable[[], Awaitable[Any]]

EXERCISE_SEARCH_SQL = """
select exercise_id, display_name, kind, modality, score, matched_text,
       matched_source, brand_name, model_name
from catalog_dev.search_exercises($1::text, $2::int, $3::text)
"""

EXERCISE_BROWSE_SQL = """
with selected_families as (
  select
    f.exercise_family_id,
    f.slug,
    f.display_name,
    f.kind,
    f.movement_group,
    f.movement_pattern,
    coalesce(
      array(
        select canonical_m.display_name
        from catalog_dev.exercise_family_member canonical_fm
        join catalog_dev.exercise canonical_e
          on canonical_e.exercise_id=canonical_fm.exercise_id
         and canonical_e.is_active=true
         and canonical_e.is_public=true
        join catalog_dev.exercise_muscle canonical_em
          on canonical_em.exercise_id=canonical_e.exercise_id
         and canonical_em.role='primary'
        join catalog_dev.muscle canonical_m
          on canonical_m.muscle_slug=canonical_em.muscle_slug
        where canonical_fm.exercise_family_id=f.exercise_family_id
          and canonical_fm.is_active=true
        group by canonical_m.muscle_slug, canonical_m.display_name
        order by
          max(canonical_em.weight) desc nulls last,
          lower(canonical_m.display_name),
          canonical_m.muscle_slug
      ),
      array[]::text[]
    ) as primary_muscles,
    f.description,
    f.sort_order
  from catalog_dev.exercise_family f
  where f.is_active=true
    and f.kind=$3
    and ($2='' or f.movement_group=$2)
    and (
      $1=''
      or lower(f.display_name) like ('%' || lower($1) || '%')
      or exists (
        select 1
        from catalog_dev.exercise_family_member qfm
        join catalog_dev.exercise qe
          on qe.exercise_id=qfm.exercise_id
        where qfm.exercise_family_id=f.exercise_family_id
          and qfm.is_active=true
          and qe.is_active=true
          and qe.is_public=true
          and lower(qe.display_name) like ('%' || lower($1) || '%')
      )
    )
  order by f.sort_order asc, lower(f.display_name) asc
  limit $4
)
select
  f.exercise_family_id,
  f.slug as family_slug,
  f.display_name as family_name,
  f.kind,
  f.movement_group,
  f.movement_pattern,
  f.primary_muscles as family_primary_muscles,
  f.description,
  f.sort_order as family_sort_order,
  fm.exercise_family_member_id,
  fm.variant_label,
  fm.is_default,
  fm.sort_order as variant_sort_order,
  e.exercise_id,
  e.slug as exercise_slug,
  e.display_name,
  e.modality,
  coalesce(
    array(
      select variant_m.display_name
      from catalog_dev.exercise_muscle variant_em
      join catalog_dev.muscle variant_m
        on variant_m.muscle_slug=variant_em.muscle_slug
      where variant_em.exercise_id=e.exercise_id
        and variant_em.role='primary'
      order by
        variant_em.weight desc nulls last,
        lower(variant_m.display_name),
        variant_m.muscle_slug
    ),
    array[]::text[]
  ) as primary_muscles,
  e.equipment_required,
  e.unilateral
from selected_families f
join catalog_dev.exercise_family_member fm
  on fm.exercise_family_id=f.exercise_family_id
 and fm.is_active=true
join catalog_dev.exercise e
  on e.exercise_id=fm.exercise_id
 and e.is_active=true
 and e.is_public=true
order by
  f.sort_order asc,
  lower(f.display_name) asc,
  fm.is_default desc,
  fm.sort_order asc,
  lower(e.display_name) asc
"""

MUSCLE_CATALOG_SQL = """
select
  m.muscle_slug,
  m.display_name,
  m.parent_slug,
  m.region,
  coalesce(
    array(
      select ma.alias
      from catalog_dev.muscle_alias ma
      where ma.muscle_slug=m.muscle_slug
        and ma.locale=$3
      order by lower(ma.alias), ma.alias
    ),
    array[]::text[]
  ) as aliases
from catalog_dev.muscle m
where (
    $1=''
    or lower(m.display_name) like ('%' || lower($1) || '%')
    or lower(m.muscle_slug) like ('%' || lower($1) || '%')
    or exists (
      select 1
      from catalog_dev.muscle_alias search_alias
      where search_alias.muscle_slug=m.muscle_slug
        and search_alias.locale=$3
        and lower(search_alias.alias) like ('%' || lower($1) || '%')
    )
  )
  and ($2='' or lower(coalesce(m.region, ''))=lower($2))
order by lower(m.display_name), m.muscle_slug
limit $4
"""

EXERCISE_MUSCLES_SQL = """
select
  e.exercise_id,
  e.slug as exercise_slug,
  e.display_name as exercise_display_name,
  em.muscle_slug,
  m.display_name as muscle_display_name,
  m.parent_slug,
  m.region,
  em.role,
  em.weight,
  coalesce(
    array(
      select ma.alias
      from catalog_dev.muscle_alias ma
      where ma.muscle_slug=em.muscle_slug
        and ma.locale=$2
      order by lower(ma.alias), ma.alias
    ),
    array[]::text[]
  ) as aliases
from catalog_dev.exercise e
left join catalog_dev.exercise_muscle em
  on em.exercise_id=e.exercise_id
left join catalog_dev.muscle m
  on m.muscle_slug=em.muscle_slug
where e.exercise_id=$1
  and e.is_active=true
  and e.is_public=true
order by
  case em.role when 'primary' then 0 when 'secondary' then 1 when 'stabilizer' then 2 else 3 end,
  em.weight desc nulls last,
  lower(m.display_name) nulls last,
  em.muscle_slug nulls last
"""


async def connect_lifeswitch_catalog() -> asyncpg.Connection:
    """Open the canonical isolated LifeSwitch database at call time."""
    return await asyncpg.connect(resolve_lifeswitch_postgres_dsn())


class PostgresLifeSwitchCatalogReader:
    """Query the shared catalog without accepting mutation authority."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    async def search_exercises(
        self,
        query: str,
        limit: int,
        locale: str,
    ) -> list[Any]:
        async with self._connection.transaction(readonly=True):
            rows = await self._connection.fetch(
                EXERCISE_SEARCH_SQL,
                query,
                limit,
                locale,
            )
        return list(rows)

    async def browse_exercises(
        self,
        query: str,
        movement_group: str,
        kind: str,
        limit: int,
    ) -> list[Any]:
        async with self._connection.transaction(readonly=True):
            rows = await self._connection.fetch(
                EXERCISE_BROWSE_SQL,
                query,
                movement_group,
                kind,
                limit,
            )
        return list(rows)

    async def list_muscles(
        self,
        query: str,
        region: str,
        locale: str,
        limit: int,
    ) -> list[Any]:
        async with self._connection.transaction(readonly=True):
            rows = await self._connection.fetch(
                MUSCLE_CATALOG_SQL,
                query,
                region,
                locale,
                limit,
            )
        return list(rows)

    async def read_exercise_muscles(
        self,
        exercise_id: Any,
        locale: str,
    ) -> list[Any]:
        async with self._connection.transaction(readonly=True):
            rows = await self._connection.fetch(
                EXERCISE_MUSCLES_SQL,
                exercise_id,
                locale,
            )
        return list(rows)


@asynccontextmanager
async def lifeswitch_catalog_reader(
    *,
    connection_factory: ConnectionFactory = connect_lifeswitch_catalog,
) -> AsyncIterator[PostgresLifeSwitchCatalogReader]:
    connection = await connection_factory()
    try:
        yield PostgresLifeSwitchCatalogReader(connection)
    finally:
        await connection.close()


__all__ = [
    "EXERCISE_MUSCLES_SQL",
    "MUSCLE_CATALOG_SQL",
    "PostgresLifeSwitchCatalogReader",
    "connect_lifeswitch_catalog",
    "lifeswitch_catalog_reader",
]
