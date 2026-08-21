from __future__ import annotations

"""PostgreSQL effect boundary for LifeSwitch measurement records."""

import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from starlette.requests import Request

from seebx.adapters.lifeswitch_postgres import connect_lifeswitch


ConnectionFactory = Callable[[Request], Awaitable[Any]]
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")


def resolve_people_schema(value: str | None = None) -> str:
    """Resolve one SQL identifier without admitting executable SQL text."""
    schema = (
        os.getenv("LIFESWITCH_PEOPLE_SCHEMA", "lifeswitch_people")
        if value is None
        else value
    ).strip()
    if _IDENTIFIER.fullmatch(schema) is None:
        raise RuntimeError("invalid LIFESWITCH_PEOPLE_SCHEMA")
    return schema


@dataclass(frozen=True)
class MeasurementEntryWrite:
    owner_user_id: str
    local_date: date
    measured_at: datetime | None
    weight_value: float | None
    weight_unit: str
    waist_value: float | None
    abdomen_value: float | None
    neck_value: float | None
    chest_value: float | None
    hip_value: float | None
    left_arm_value: float | None
    right_arm_value: float | None
    left_thigh_value: float | None
    right_thigh_value: float | None
    left_calf_value: float | None
    right_calf_value: float | None
    body_fat_percent: float | None
    body_fat_method: str | None
    measurement_unit: str
    source: str
    entry_kind: str
    notes: str
    skinfolds_json: str | None
    scan_json: str | None


class PostgresLifeSwitchMeasurementsRepository:
    """Own the four retained measurement queries and connection-local effects."""

    def __init__(self, connection: Any, *, people_schema: str) -> None:
        self._connection = connection
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

    async def list_entries(
        self,
        *,
        owner_user_id: str,
        limit: int,
        include_inactive: bool,
    ) -> list[Any]:
        where_active = "" if include_inactive else "and is_active=true"
        rows = await self._connection.fetch(
            f"""
            select
              measurement_entry_id,
              owner_user_id,
              local_date,
              measured_at,

              weight_value,
              weight_unit,

              waist_value,
              abdomen_value,
              neck_value,
              chest_value,
              hip_value,

              left_arm_value,
              right_arm_value,
              left_thigh_value,
              right_thigh_value,
              left_calf_value,
              right_calf_value,

              body_fat_percent,
              body_fat_method,

              measurement_unit,
              source,
              entry_kind,
              notes,
              skinfolds_json,
              scan_json,

              is_active,
              created_at,
              updated_at
            from public.lifeswitch_measurement_entries
            where owner_user_id=$1
              {where_active}
            order by local_date desc, created_at desc
            limit $2
            """,
            owner_user_id,
            limit,
        )
        return list(rows)

    async def create_entry(self, value: MeasurementEntryWrite) -> Any:
        return await self._connection.fetchrow(
            """
            insert into public.lifeswitch_measurement_entries (
              owner_user_id,
              local_date,
              measured_at,

              weight_value,
              weight_unit,

              waist_value,
              abdomen_value,
              neck_value,
              chest_value,
              hip_value,

              left_arm_value,
              right_arm_value,
              left_thigh_value,
              right_thigh_value,
              left_calf_value,
              right_calf_value,

              body_fat_percent,
              body_fat_method,

              measurement_unit,
              source,
              entry_kind,
              notes,
              skinfolds_json,
              scan_json,

              is_active
            )
            values (
              $1,
              $2::date,
              $3::timestamptz,

              $4,
              $5,

              $6,
              $7,
              $8,
              $9,
              $10,

              $11,
              $12,
              $13,
              $14,
              $15,
              $16,

              $17,
              $18,

              $19,
              $20,
              $21,
              $22,
              $23::jsonb,
              $24::jsonb,

              true
            )
            on conflict (owner_user_id, local_date, source, entry_kind)
              where is_active=true
            do update set
              measured_at=coalesce(excluded.measured_at, public.lifeswitch_measurement_entries.measured_at),

              weight_value=coalesce(excluded.weight_value, public.lifeswitch_measurement_entries.weight_value),
              weight_unit=excluded.weight_unit,

              waist_value=coalesce(excluded.waist_value, public.lifeswitch_measurement_entries.waist_value),
              abdomen_value=coalesce(excluded.abdomen_value, public.lifeswitch_measurement_entries.abdomen_value),
              neck_value=coalesce(excluded.neck_value, public.lifeswitch_measurement_entries.neck_value),
              chest_value=coalesce(excluded.chest_value, public.lifeswitch_measurement_entries.chest_value),
              hip_value=coalesce(excluded.hip_value, public.lifeswitch_measurement_entries.hip_value),

              left_arm_value=coalesce(excluded.left_arm_value, public.lifeswitch_measurement_entries.left_arm_value),
              right_arm_value=coalesce(excluded.right_arm_value, public.lifeswitch_measurement_entries.right_arm_value),
              left_thigh_value=coalesce(excluded.left_thigh_value, public.lifeswitch_measurement_entries.left_thigh_value),
              right_thigh_value=coalesce(excluded.right_thigh_value, public.lifeswitch_measurement_entries.right_thigh_value),
              left_calf_value=coalesce(excluded.left_calf_value, public.lifeswitch_measurement_entries.left_calf_value),
              right_calf_value=coalesce(excluded.right_calf_value, public.lifeswitch_measurement_entries.right_calf_value),

              body_fat_percent=coalesce(excluded.body_fat_percent, public.lifeswitch_measurement_entries.body_fat_percent),
              body_fat_method=coalesce(excluded.body_fat_method, public.lifeswitch_measurement_entries.body_fat_method),

              measurement_unit=excluded.measurement_unit,
              notes=case when excluded.notes <> '' then excluded.notes else public.lifeswitch_measurement_entries.notes end,
              skinfolds_json=coalesce(excluded.skinfolds_json, public.lifeswitch_measurement_entries.skinfolds_json),
              scan_json=coalesce(excluded.scan_json, public.lifeswitch_measurement_entries.scan_json),

              updated_at=now()
            returning
              measurement_entry_id,
              owner_user_id,
              local_date,
              measured_at,

              weight_value,
              weight_unit,

              waist_value,
              abdomen_value,
              neck_value,
              chest_value,
              hip_value,

              left_arm_value,
              right_arm_value,
              left_thigh_value,
              right_thigh_value,
              left_calf_value,
              right_calf_value,

              body_fat_percent,
              body_fat_method,

              measurement_unit,
              source,
              entry_kind,
              notes,
              skinfolds_json,
              scan_json,

              is_active,
              created_at,
              updated_at
            """,
            value.owner_user_id,
            value.local_date,
            value.measured_at,
            value.weight_value,
            value.weight_unit,
            value.waist_value,
            value.abdomen_value,
            value.neck_value,
            value.chest_value,
            value.hip_value,
            value.left_arm_value,
            value.right_arm_value,
            value.left_thigh_value,
            value.right_thigh_value,
            value.left_calf_value,
            value.right_calf_value,
            value.body_fat_percent,
            value.body_fat_method,
            value.measurement_unit,
            value.source,
            value.entry_kind,
            value.notes,
            value.skinfolds_json,
            value.scan_json,
        )

    async def deactivate_entry(
        self,
        *,
        measurement_entry_id: str,
        owner_user_id: str,
    ) -> Any | None:
        return await self._connection.fetchrow(
            """
            update public.lifeswitch_measurement_entries
               set is_active=false,
                   updated_at=now()
             where measurement_entry_id=$1::uuid
               and owner_user_id=$2
               and is_active=true
            returning
              measurement_entry_id,
              owner_user_id,
              local_date,
              entry_kind,
              source,
              is_active,
              created_at,
              updated_at
            """,
            measurement_entry_id,
            owner_user_id,
        )


@asynccontextmanager
async def lifeswitch_measurements_repository(
    request: Request,
    *,
    connection_factory: ConnectionFactory = connect_lifeswitch,
) -> AsyncIterator[PostgresLifeSwitchMeasurementsRepository]:
    people_schema = resolve_people_schema()
    connection = await connection_factory(request)
    try:
        yield PostgresLifeSwitchMeasurementsRepository(
            connection,
            people_schema=people_schema,
        )
    finally:
        await connection.close()


__all__ = [
    "MeasurementEntryWrite",
    "PostgresLifeSwitchMeasurementsRepository",
    "lifeswitch_measurements_repository",
    "resolve_people_schema",
]
