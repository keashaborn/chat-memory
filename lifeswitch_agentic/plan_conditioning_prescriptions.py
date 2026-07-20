from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

import asyncpg

from .plan_domain import PlanDocumentV1, PlanDomainError


LINKED_CONDITIONING_FIELD = "linked_conditioning"
MAX_LINKED_CONDITIONING = 20


def _uuid(value: Any) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise PlanDomainError(
            "invalid_section",
            "each linked conditioning plan must contain a valid "
            "my_conditioning_prescription_id",
        ) from error


def _frequency(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise PlanDomainError(
            "invalid_section",
            "linked conditioning sessions_per_week must be a number from 0 through 21",
        )
    numeric = float(value)
    if not 0 <= numeric <= 21:
        raise PlanDomainError(
            "invalid_section",
            "linked conditioning sessions_per_week must be a number from 0 through 21",
        )
    return int(numeric) if numeric.is_integer() else numeric


def _notes(value: Any) -> str:
    cleaned = str(value or "").strip()
    if len(cleaned) > 500:
        raise PlanDomainError(
            "field_too_long",
            "linked conditioning schedule_notes must not exceed 500 characters",
        )
    return cleaned


def _dose_config(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _optional_number(value: Any) -> int | float | None:
    if value is None:
        return None
    numeric = float(value)
    return int(numeric) if numeric.is_integer() else numeric


def _snapshot_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    updated_at = row.get("updated_at")
    return {
        "name": str(row.get("name") or "Unnamed conditioning plan"),
        "category": str(row.get("category") or ""),
        "modality": str(row.get("modality") or ""),
        "purpose": str(row.get("purpose") or ""),
        "target_duration_min": _optional_number(row.get("target_duration_min")),
        "target_frequency_per_week": _optional_number(
            row.get("target_frequency_per_week")
        ),
        "target_intensity": str(row.get("target_intensity") or ""),
        "preferred_timing": str(row.get("preferred_timing") or ""),
        "recovery_constraints": str(row.get("recovery_constraints") or ""),
        "notes": str(row.get("notes") or ""),
        "dose_type": str(row.get("dose_type") or "open"),
        "dose_config": _dose_config(row.get("dose_config")),
        "library_slug": str(row.get("library_slug") or ""),
        "library_name": str(row.get("library_name") or ""),
        "prescription_updated_at": (
            updated_at.isoformat() if updated_at is not None else None
        ),
    }


@dataclass(frozen=True, slots=True)
class PlanConditioningPrescriptionService:
    schema: str = "lifeswitch_training"

    async def list_options(
        self,
        conn: asyncpg.Connection,
        *,
        owner_user_id: uuid.UUID,
    ) -> list[dict[str, Any]]:
        rows = await conn.fetch(
            f"""
            select
              p.my_conditioning_prescription_id,
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
              p.updated_at,
              l.slug as library_slug,
              l.name as library_name
            from {self.schema}.my_conditioning_prescription p
            left join {self.schema}.conditioning_library l
              on l.conditioning_library_id = p.conditioning_library_id
            where p.owner_user_id = $1::uuid
              and p.is_active = true
            order by lower(p.name), p.my_conditioning_prescription_id
            """,
            owner_user_id,
        )
        return [
            {
                "my_conditioning_prescription_id": str(
                    row["my_conditioning_prescription_id"]
                ),
                **_snapshot_from_row(row),
            }
            for row in rows
        ]

    async def materialize_document(
        self,
        conn: asyncpg.Connection,
        *,
        owner_user_id: uuid.UUID,
        document: PlanDocumentV1,
    ) -> PlanDocumentV1:
        raw = document.to_dict()
        conditioning = dict(raw.get("conditioning_targets") or {})
        linked = conditioning.get(LINKED_CONDITIONING_FIELD)
        if linked is None:
            return document
        if not isinstance(linked, list):
            raise PlanDomainError(
                "invalid_section",
                "conditioning_targets.linked_conditioning must be a list",
            )
        if len(linked) > MAX_LINKED_CONDITIONING:
            raise PlanDomainError(
                "invalid_section",
                f"no more than {MAX_LINKED_CONDITIONING} conditioning plans may be linked",
            )

        selected: list[tuple[uuid.UUID, int | float, str]] = []
        seen: set[uuid.UUID] = set()
        for item in linked:
            if not isinstance(item, Mapping):
                raise PlanDomainError(
                    "invalid_section",
                    "each linked conditioning plan must be a JSON object",
                )
            prescription_id = _uuid(item.get("my_conditioning_prescription_id"))
            if prescription_id in seen:
                raise PlanDomainError(
                    "invalid_section",
                    "a conditioning plan may be linked only once",
                )
            seen.add(prescription_id)
            selected.append(
                (
                    prescription_id,
                    _frequency(item.get("sessions_per_week", 1)),
                    _notes(item.get("schedule_notes")),
                )
            )

        snapshots: list[dict[str, Any]] = []
        for prescription_id, sessions_per_week, schedule_notes in selected:
            snapshot = await self._snapshot(
                conn,
                owner_user_id=owner_user_id,
                prescription_id=prescription_id,
            )
            snapshots.append(
                {
                    "my_conditioning_prescription_id": str(prescription_id),
                    "sessions_per_week": sessions_per_week,
                    "schedule_notes": schedule_notes,
                    "prescription_snapshot": snapshot,
                }
            )

        conditioning[LINKED_CONDITIONING_FIELD] = snapshots
        raw["conditioning_targets"] = conditioning
        return PlanDocumentV1.from_mapping(raw)

    async def _snapshot(
        self,
        conn: asyncpg.Connection,
        *,
        owner_user_id: uuid.UUID,
        prescription_id: uuid.UUID,
    ) -> dict[str, Any]:
        row = await conn.fetchrow(
            f"""
            select
              p.my_conditioning_prescription_id,
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
              p.updated_at,
              l.slug as library_slug,
              l.name as library_name
            from {self.schema}.my_conditioning_prescription p
            left join {self.schema}.conditioning_library l
              on l.conditioning_library_id = p.conditioning_library_id
            where p.my_conditioning_prescription_id = $1::uuid
              and p.owner_user_id = $2::uuid
              and p.is_active = true
            """,
            prescription_id,
            owner_user_id,
        )
        if row is None:
            raise PlanDomainError(
                "entity_out_of_scope",
                "a selected conditioning plan is unavailable",
            )
        return _snapshot_from_row(row)
