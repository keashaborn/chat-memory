from __future__ import annotations

"""PostgreSQL effect boundary for LifeSwitch Conditioning."""

import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from starlette.requests import Request

from seebx.adapters.lifeswitch_postgres import connect_lifeswitch
from seebx.adapters.lifeswitch_training_writes_postgres import (
    correct_conditioning_session as write_conditioning_correction,
    create_conditioning_session as write_conditioning_session,
    set_transaction_actor,
    void_conditioning_session as write_conditioning_void,
)


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


class PostgresLifeSwitchTrainingConditioningRepository:
    """Own all ten retained Conditioning SQL effects and three transactions."""

    def __init__(self, connection: Any, *, training_schema: str) -> None:
        self._connection = connection
        self._schema = resolve_training_schema(training_schema)

    async def list_library(self, *, include_inactive: bool) -> tuple[Any, ...]:
        where_active = "" if include_inactive else "where is_active=true"
        rows = await self._connection.fetch(
            f"""
            select
              conditioning_library_id, slug, name, category, modality,
              purpose, default_duration_min, default_frequency_per_week,
              default_intensity, interference_risk, joint_stress, equipment,
              progression_notes, contraindication_notes,
              sort_order, is_active, created_at, updated_at
            from {self._schema}.conditioning_library
            {where_active}
            order by sort_order asc, lower(name) asc
            """
        )
        return tuple(rows)

    async def list_prescriptions(
        self, *, owner_user_id: str, include_inactive: bool
    ) -> tuple[Any, ...]:
        where_active = "" if include_inactive else "and p.is_active=true"
        rows = await self._connection.fetch(
            f"""
            select
              p.my_conditioning_prescription_id,
              p.owner_user_id,
              p.conditioning_library_id,
              p.name,
              p.category,
              p.modality,
              p.purpose,
              p.target_duration_min,
              p.target_frequency_per_week,
              p.target_intensity,
              p.preferred_timing,
              p.recovery_constraints,
              p.notes,
              p.dose_type,
              p.dose_config,
              p.is_active,
              p.created_at,
              p.updated_at,
              l.slug as library_slug,
              l.name as library_name
            from {self._schema}.my_conditioning_prescription p
            left join {self._schema}.conditioning_library l
              on l.conditioning_library_id=p.conditioning_library_id
            where p.owner_user_id=$1::uuid
              {where_active}
            order by p.updated_at desc, lower(p.name) asc
            """,
            owner_user_id,
        )
        return tuple(rows)

    async def get_prescription_owner(self, *, prescription_id: str) -> Any | None:
        return await self._connection.fetchval(
            f"""
            select owner_user_id
            from {self._schema}.my_conditioning_prescription
            where my_conditioning_prescription_id=$1::uuid
            """,
            prescription_id,
        )

    async def upsert_prescription(
        self,
        *,
        prescription_id: str | None,
        owner_user_id: str,
        conditioning_library_id: str | None,
        name: str,
        category: str,
        modality: str,
        purpose: str,
        target_duration_min: int,
        target_frequency_per_week: float,
        target_intensity: str,
        preferred_timing: str,
        recovery_constraints: str,
        notes: str,
        dose_type: str,
        dose_config_json: str,
    ) -> Any | None:
        if prescription_id:
            return await self._connection.fetchrow(
                f"""
                insert into {self._schema}.my_conditioning_prescription
                  (
                    my_conditioning_prescription_id,
                    owner_user_id,
                    conditioning_library_id,
                    name,
                    category,
                    modality,
                    purpose,
                    target_duration_min,
                    target_frequency_per_week,
                    target_intensity,
                    preferred_timing,
                    recovery_constraints,
                    notes,
                    dose_type,
                    dose_config,
                    is_active
                  )
                values
                  (
                    $1::uuid,
                    $2::uuid,
                    $3::uuid,
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
                    $15::jsonb,
                    true
                  )
                on conflict (my_conditioning_prescription_id) do update
                  set conditioning_library_id=excluded.conditioning_library_id,
                      name=excluded.name,
                      category=excluded.category,
                      modality=excluded.modality,
                      purpose=excluded.purpose,
                      target_duration_min=excluded.target_duration_min,
                      target_frequency_per_week=excluded.target_frequency_per_week,
                      target_intensity=excluded.target_intensity,
                      preferred_timing=excluded.preferred_timing,
                      recovery_constraints=excluded.recovery_constraints,
                      notes=excluded.notes,
                      dose_type=excluded.dose_type,
                      dose_config=excluded.dose_config,
                      is_active=true,
                      updated_at=now()
                returning
                  my_conditioning_prescription_id,
                  owner_user_id,
                  conditioning_library_id,
                  name,
                  category,
                  modality,
                  purpose,
                  target_duration_min,
                  target_frequency_per_week,
                  target_intensity,
                  preferred_timing,
                  recovery_constraints,
                  notes,
                  dose_type,
                  dose_config,
                  is_active,
                  created_at,
                  updated_at
                """,
                prescription_id,
                owner_user_id,
                conditioning_library_id,
                name,
                category,
                modality,
                purpose,
                target_duration_min,
                target_frequency_per_week,
                target_intensity,
                preferred_timing,
                recovery_constraints,
                notes,
                dose_type,
                dose_config_json,
            )
        return await self._connection.fetchrow(
            f"""
            insert into {self._schema}.my_conditioning_prescription
              (
                owner_user_id,
                conditioning_library_id,
                name,
                category,
                modality,
                purpose,
                target_duration_min,
                target_frequency_per_week,
                target_intensity,
                preferred_timing,
                recovery_constraints,
                notes,
                dose_type,
                dose_config,
                is_active
              )
            values
              (
                $1::uuid,
                $2::uuid,
                $3,
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
                $14::jsonb,
                true
              )
            on conflict (owner_user_id, name) do update
              set conditioning_library_id=excluded.conditioning_library_id,
                  category=excluded.category,
                  modality=excluded.modality,
                  purpose=excluded.purpose,
                  target_duration_min=excluded.target_duration_min,
                  target_frequency_per_week=excluded.target_frequency_per_week,
                  target_intensity=excluded.target_intensity,
                  preferred_timing=excluded.preferred_timing,
                  recovery_constraints=excluded.recovery_constraints,
                  notes=excluded.notes,
                  dose_type=excluded.dose_type,
                  dose_config=excluded.dose_config,
                  is_active=true,
                  updated_at=now()
            returning
              my_conditioning_prescription_id,
              owner_user_id,
              conditioning_library_id,
              name,
              category,
              modality,
              purpose,
              target_duration_min,
              target_frequency_per_week,
              target_intensity,
              preferred_timing,
              recovery_constraints,
              notes,
              dose_type,
              dose_config,
              is_active,
              created_at,
              updated_at
            """,
            owner_user_id,
            conditioning_library_id,
            name,
            category,
            modality,
            purpose,
            target_duration_min,
            target_frequency_per_week,
            target_intensity,
            preferred_timing,
            recovery_constraints,
            notes,
            dose_type,
            dose_config_json,
        )

    async def deactivate_prescription(
        self, *, prescription_id: str, owner_user_id: str
    ) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            update {self._schema}.my_conditioning_prescription
               set is_active=false, updated_at=now()
             where my_conditioning_prescription_id=$1::uuid
               and owner_user_id=$2::uuid
            returning my_conditioning_prescription_id, owner_user_id, is_active, updated_at
            """,
            prescription_id,
            owner_user_id,
        )

    async def create_session(
        self, *, owner_user_id: str, intent: dict[str, Any], idempotency_key: str
    ) -> Any | None:
        async with self._connection.transaction():
            await set_transaction_actor(self._connection, actor_user_id=owner_user_id)
            session_id = await write_conditioning_session(
                self._connection,
                intent=intent,
                idempotency_key=idempotency_key,
            )
            return await self._connection.fetchrow(
                f"""
                select
                  conditioning_session_log_id, owner_user_id,
                  my_conditioning_prescription_id, day, name, category,
                  modality, duration_min, intensity, distance,
                  distance_value, distance_unit, heart_rate_avg,
                  recovery_impact, notes, dose_type, dose_config,
                  is_active, created_at, updated_at
                from {self._schema}.conditioning_session_log
                where conditioning_session_log_id=$1::uuid
                  and owner_user_id=$2::uuid
                """,
                session_id,
                owner_user_id,
            )

    async def list_sessions(
        self,
        *,
        owner_user_id: str,
        delegated: bool,
        day: Any | None,
        include_inactive: bool,
        limit: int,
    ) -> tuple[Any, ...]:
        session_source = (
            "conditioning_session_log"
            if include_inactive
            else "conditioning_session_current_v"
        )
        where = ["c.owner_user_id=$1::uuid"]
        args: list[Any] = [owner_user_id, delegated]
        if day:
            args.append(day)
            where.append(f"c.day=${len(args)}::date")
        if not include_inactive:
            where.append("c.is_active=true")
        rows = await self._connection.fetch(
            f"""
            select
              c.conditioning_session_log_id,
              c.owner_user_id,
              c.my_conditioning_prescription_id,
              c.day,
              c.name,
              c.category,
              c.modality,
              c.duration_min,
              c.intensity,
              c.distance,
              c.distance_value,
              c.distance_unit,
              c.heart_rate_avg,
              c.recovery_impact,
              c.notes,
              c.dose_type,
              c.dose_config,
              c.is_active,
              c.created_at,
              c.updated_at,
              $1::uuid as _target_user_id,
              $2::boolean as _delegated_view,
              p.name as prescription_name
            from {self._schema}.{session_source} c
            left join {self._schema}.my_conditioning_prescription p
              on p.my_conditioning_prescription_id=c.my_conditioning_prescription_id
            where {' and '.join(where)}
            order by c.day desc, c.created_at desc
            limit {int(limit)}
            """,
            *args,
        )
        return tuple(rows)

    async def get_session(
        self, *, conditioning_session_log_id: str, owner_user_id: str
    ) -> Any | None:
        return await self._connection.fetchrow(
            f"""
            select
              conditioning_session_log_id,
              owner_user_id,
              my_conditioning_prescription_id,
              day,
              name,
              category,
              modality,
              duration_min,
              intensity,
              distance,
              distance_value,
              distance_unit,
              heart_rate_avg,
              recovery_impact,
              notes,
              dose_type,
              dose_config,
              is_active,
              created_at,
              updated_at
            from {self._schema}.conditioning_session_current_v
            where conditioning_session_log_id=$1::uuid
              and owner_user_id=$2::uuid
            """,
            conditioning_session_log_id,
            owner_user_id,
        )

    async def void_session(
        self,
        *,
        conditioning_session_log_id: str,
        owner_user_id: str,
        reason: str,
    ) -> Any:
        async with self._connection.transaction():
            await set_transaction_actor(self._connection, actor_user_id=owner_user_id)
            return await write_conditioning_void(
                self._connection,
                conditioning_session_log_id=conditioning_session_log_id,
                reason=reason,
            )

    async def correct_session(
        self,
        *,
        conditioning_session_log_id: str,
        owner_user_id: str,
        intent: dict[str, Any],
        idempotency_key: str,
    ) -> Any | None:
        async with self._connection.transaction():
            await set_transaction_actor(self._connection, actor_user_id=owner_user_id)
            replacement_id = await write_conditioning_correction(
                self._connection,
                conditioning_session_log_id=conditioning_session_log_id,
                intent=intent,
                idempotency_key=idempotency_key,
            )
            return await self._connection.fetchrow(
                f"""
                select
                  conditioning_session_log_id, owner_user_id,
                  my_conditioning_prescription_id, day, name, category,
                  modality, duration_min, intensity, distance,
                  distance_value, distance_unit, heart_rate_avg,
                  recovery_impact, notes, dose_type, dose_config,
                  supersedes_conditioning_session_id, is_active,
                  created_at, updated_at
                from {self._schema}.conditioning_session_current_v
                where conditioning_session_log_id=$1::uuid
                  and owner_user_id=$2::uuid
                """,
                replacement_id,
                owner_user_id,
            )


@asynccontextmanager
async def lifeswitch_training_conditioning_repository(
    request: Request,
    *,
    connection_factory: ConnectionFactory = connect_lifeswitch,
    training_schema: str | None = None,
) -> AsyncIterator[PostgresLifeSwitchTrainingConditioningRepository]:
    connection = await connection_factory(request)
    try:
        yield PostgresLifeSwitchTrainingConditioningRepository(
            connection,
            training_schema=resolve_training_schema(training_schema),
        )
    finally:
        await connection.close()
