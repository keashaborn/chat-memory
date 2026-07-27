from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import asyncpg

from .command_idempotency import begin_command, command_fingerprint, complete_command
from .identifiers import uuid7
from .plan_domain import PlanDomainError


SCHEMA = "lifeswitch_agentic"
REASON_CODES = frozenset({"surgery_recovery", "illness", "injury", "other"})
IdFactory = Callable[[], uuid.UUID]


@dataclass(frozen=True, slots=True)
class RecoveryPeriod:
    starts_on: dt.date
    ends_on: dt.date

    @classmethod
    def from_values(
        cls,
        *,
        starts_on: dt.date | None,
        ends_on: dt.date | None,
        field: str,
    ) -> RecoveryPeriod | None:
        if starts_on is None and ends_on is None:
            return None
        if starts_on is None or ends_on is None:
            raise PlanDomainError(
                "invalid_recovery_period",
                f"{field} requires both starts_on and ends_on",
            )
        if ends_on < starts_on:
            raise PlanDomainError(
                "invalid_recovery_period",
                f"{field}.ends_on cannot be before starts_on",
            )
        if (ends_on - starts_on).days > 365:
            raise PlanDomainError(
                "invalid_recovery_period",
                f"{field} cannot exceed 366 days",
            )
        return cls(starts_on=starts_on, ends_on=ends_on)


@dataclass(frozen=True, slots=True)
class RecoveryAdjustmentInput:
    reason_code: str
    note: str
    nutrition_period: RecoveryPeriod | None
    strength_period: RecoveryPeriod | None

    @classmethod
    def create(
        cls,
        *,
        reason_code: str,
        note: str,
        nutrition_starts_on: dt.date | None,
        nutrition_ends_on: dt.date | None,
        strength_starts_on: dt.date | None,
        strength_ends_on: dt.date | None,
    ) -> RecoveryAdjustmentInput:
        cleaned_reason = str(reason_code or "").strip()
        if cleaned_reason not in REASON_CODES:
            raise PlanDomainError(
                "invalid_recovery_reason",
                "reason_code must be surgery_recovery|illness|injury|other",
            )
        cleaned_note = str(note or "").strip()
        if len(cleaned_note) > 1000:
            raise PlanDomainError(
                "field_too_long",
                "recovery adjustment note exceeds 1000 characters",
            )
        nutrition = RecoveryPeriod.from_values(
            starts_on=nutrition_starts_on,
            ends_on=nutrition_ends_on,
            field="nutrition_period",
        )
        strength = RecoveryPeriod.from_values(
            starts_on=strength_starts_on,
            ends_on=strength_ends_on,
            field="strength_period",
        )
        if nutrition is None and strength is None:
            raise PlanDomainError(
                "recovery_scope_required",
                "select nutrition, strength, or both",
            )
        return cls(
            reason_code=cleaned_reason,
            note=cleaned_note,
            nutrition_period=nutrition,
            strength_period=strength,
        )


@dataclass(frozen=True, slots=True)
class RecoveryAdjustmentRecord:
    adjustment_id: uuid.UUID
    owner_user_id: uuid.UUID
    reason_code: str
    note: str
    nutrition_period: RecoveryPeriod | None
    strength_period: RecoveryPeriod | None
    created_by_actor_user_id: uuid.UUID
    created_at: dt.datetime
    stopped_on: dt.date | None
    stopped_by_actor_user_id: uuid.UUID | None
    stopped_at: dt.datetime | None

    @staticmethod
    def _effective_period(
        period: RecoveryPeriod | None,
        stopped_on: dt.date | None,
    ) -> RecoveryPeriod | None:
        if period is None:
            return None
        effective_end = period.ends_on
        if stopped_on is not None:
            effective_end = min(effective_end, stopped_on - dt.timedelta(days=1))
        if effective_end < period.starts_on:
            return None
        return RecoveryPeriod(starts_on=period.starts_on, ends_on=effective_end)

    def to_dict(self, *, include_private_note: bool) -> dict[str, Any]:
        def period_dict(period: RecoveryPeriod | None) -> dict[str, str] | None:
            return (
                {
                    "starts_on": period.starts_on.isoformat(),
                    "ends_on": period.ends_on.isoformat(),
                }
                if period
                else None
            )

        return {
            "adjustment_id": str(self.adjustment_id),
            "reason_code": self.reason_code,
            "note": self.note if include_private_note else "",
            "nutrition_period": period_dict(
                self._effective_period(self.nutrition_period, self.stopped_on)
            ),
            "strength_period": period_dict(
                self._effective_period(self.strength_period, self.stopped_on)
            ),
            "configured_nutrition_period": period_dict(self.nutrition_period),
            "configured_strength_period": period_dict(self.strength_period),
            "created_at": self.created_at.isoformat(),
            "stopped_on": self.stopped_on.isoformat() if self.stopped_on else None,
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
        }


def _period_from_row(
    row: Mapping[str, Any],
    *,
    start_field: str,
    end_field: str,
) -> RecoveryPeriod | None:
    starts_on = row[start_field]
    ends_on = row[end_field]
    if starts_on is None or ends_on is None:
        return None
    return RecoveryPeriod(starts_on=starts_on, ends_on=ends_on)


def record_from_row(row: Mapping[str, Any]) -> RecoveryAdjustmentRecord:
    return RecoveryAdjustmentRecord(
        adjustment_id=row["id"],
        owner_user_id=row["owner_user_id"],
        reason_code=str(row["reason_code"]),
        note=str(row["note"] or ""),
        nutrition_period=_period_from_row(
            row,
            start_field="nutrition_starts_on",
            end_field="nutrition_ends_on",
        ),
        strength_period=_period_from_row(
            row,
            start_field="strength_starts_on",
            end_field="strength_ends_on",
        ),
        created_by_actor_user_id=row["created_by_actor_user_id"],
        created_at=row["created_at"],
        stopped_on=row["stopped_on"],
        stopped_by_actor_user_id=row["stopped_by_actor_user_id"],
        stopped_at=row["stopped_at"],
    )


class RecoveryAdjustmentRepository:
    def __init__(self, *, id_factory: IdFactory = uuid7) -> None:
        self._id_factory = id_factory

    async def list_for_range(
        self,
        conn: asyncpg.Connection,
        *,
        owner_user_id: uuid.UUID,
        starts_on: dt.date,
        ends_on: dt.date,
    ) -> tuple[RecoveryAdjustmentRecord, ...]:
        rows = await conn.fetch(
            f"""
            select *
            from {SCHEMA}.recovery_adjustments
            where owner_user_id = $1
              and (
                (
                  nutrition_starts_on is not null
                  and nutrition_starts_on <= $3
                  and least(
                    nutrition_ends_on,
                    coalesce(stopped_on - 1, nutrition_ends_on)
                  ) >= $2
                )
                or
                (
                  strength_starts_on is not null
                  and strength_starts_on <= $3
                  and least(
                    strength_ends_on,
                    coalesce(stopped_on - 1, strength_ends_on)
                  ) >= $2
                )
              )
            order by created_at desc, id desc
            limit 100
            """,
            owner_user_id,
            starts_on,
            ends_on,
        )
        return tuple(record_from_row(row) for row in rows)

    async def _overlap(
        self,
        conn: asyncpg.Connection,
        *,
        owner_user_id: uuid.UUID,
        period: RecoveryPeriod,
        domain: str,
    ) -> bool:
        if domain not in {"nutrition", "strength"}:
            raise RuntimeError("unsupported recovery adjustment domain")
        prefix = "nutrition" if domain == "nutrition" else "strength"
        return bool(
            await conn.fetchval(
                f"""
                select exists (
                  select 1
                  from {SCHEMA}.recovery_adjustments
                  where owner_user_id = $1
                    and {prefix}_starts_on is not null
                    and {prefix}_starts_on <= $3
                    and least(
                      {prefix}_ends_on,
                      coalesce(stopped_on - 1, {prefix}_ends_on)
                    ) >= $2
                )
                """,
                owner_user_id,
                period.starts_on,
                period.ends_on,
            )
        )

    async def create(
        self,
        conn: asyncpg.Connection,
        *,
        owner_user_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        adjustment: RecoveryAdjustmentInput,
        idempotency_key: str,
    ) -> RecoveryAdjustmentRecord:
        command_name = "plan.recovery_adjustment.create.v1"
        request_payload = {
            "reason_code": adjustment.reason_code,
            "note": adjustment.note,
            "nutrition_period": (
                {
                    "starts_on": adjustment.nutrition_period.starts_on,
                    "ends_on": adjustment.nutrition_period.ends_on,
                }
                if adjustment.nutrition_period
                else None
            ),
            "strength_period": (
                {
                    "starts_on": adjustment.strength_period.starts_on,
                    "ends_on": adjustment.strength_period.ends_on,
                }
                if adjustment.strength_period
                else None
            ),
        }
        request_sha256 = command_fingerprint(request_payload)

        async with conn.transaction():
            replay = await begin_command(
                conn,
                owner_user_id=owner_user_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
            )
            if replay is not None:
                row = await conn.fetchrow(
                    f"""
                    select *
                    from {SCHEMA}.recovery_adjustments
                    where owner_user_id = $1 and id = $2
                    """,
                    owner_user_id,
                    uuid.UUID(str(replay["adjustment_id"])),
                )
                if row is None:
                    raise RuntimeError("idempotent recovery adjustment disappeared")
                return record_from_row(row)

            await conn.execute(
                "select pg_advisory_xact_lock(hashtextextended($1::text, 0))",
                str(owner_user_id),
            )
            for domain, period in (
                ("nutrition", adjustment.nutrition_period),
                ("strength", adjustment.strength_period),
            ):
                if period and await self._overlap(
                    conn,
                    owner_user_id=owner_user_id,
                    period=period,
                    domain=domain,
                ):
                    raise PlanDomainError(
                        "recovery_adjustment_overlap",
                        f"an existing {domain} recovery adjustment overlaps these dates",
                    )

            adjustment_id = self._id_factory()
            row = await conn.fetchrow(
                f"""
                insert into {SCHEMA}.recovery_adjustments (
                  id, owner_user_id, reason_code, note,
                  nutrition_starts_on, nutrition_ends_on,
                  strength_starts_on, strength_ends_on,
                  created_by_actor_user_id
                )
                values ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                returning *
                """,
                adjustment_id,
                owner_user_id,
                adjustment.reason_code,
                adjustment.note,
                (
                    adjustment.nutrition_period.starts_on
                    if adjustment.nutrition_period
                    else None
                ),
                (
                    adjustment.nutrition_period.ends_on
                    if adjustment.nutrition_period
                    else None
                ),
                (
                    adjustment.strength_period.starts_on
                    if adjustment.strength_period
                    else None
                ),
                (
                    adjustment.strength_period.ends_on
                    if adjustment.strength_period
                    else None
                ),
                actor_user_id,
            )
            if row is None:
                raise RuntimeError("recovery adjustment insert failed")
            result = record_from_row(row)
            await complete_command(
                conn,
                owner_user_id=owner_user_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                response={"adjustment_id": str(result.adjustment_id)},
            )
            return result

    async def stop(
        self,
        conn: asyncpg.Connection,
        *,
        owner_user_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        adjustment_id: uuid.UUID,
        stopped_on: dt.date,
        idempotency_key: str,
    ) -> RecoveryAdjustmentRecord:
        command_name = "plan.recovery_adjustment.stop.v1"
        request_sha256 = command_fingerprint(
            {
                "adjustment_id": adjustment_id,
                "stopped_on": stopped_on,
            }
        )
        async with conn.transaction():
            replay = await begin_command(
                conn,
                owner_user_id=owner_user_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
            )
            if replay is not None:
                row = await conn.fetchrow(
                    f"""
                    select *
                    from {SCHEMA}.recovery_adjustments
                    where owner_user_id = $1 and id = $2
                    """,
                    owner_user_id,
                    adjustment_id,
                )
                if row is None:
                    raise PlanDomainError(
                        "entity_out_of_scope",
                        "recovery adjustment is unavailable",
                    )
                return record_from_row(row)

            row = await conn.fetchrow(
                f"""
                select *
                from {SCHEMA}.recovery_adjustments
                where owner_user_id = $1 and id = $2
                for update
                """,
                owner_user_id,
                adjustment_id,
            )
            if row is None:
                raise PlanDomainError(
                    "entity_out_of_scope",
                    "recovery adjustment is unavailable",
                )
            if row["stopped_at"] is None:
                row = await conn.fetchrow(
                    f"""
                    update {SCHEMA}.recovery_adjustments
                    set stopped_on = $3,
                        stopped_by_actor_user_id = $4,
                        stopped_at = now()
                    where owner_user_id = $1 and id = $2
                    returning *
                    """,
                    owner_user_id,
                    adjustment_id,
                    stopped_on,
                    actor_user_id,
                )
                if row is None:
                    raise RuntimeError("recovery adjustment stop failed")
            result = record_from_row(row)
            await complete_command(
                conn,
                owner_user_id=owner_user_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                response={"adjustment_id": str(result.adjustment_id)},
            )
            return result
