from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Mapping

import asyncpg

from .plan_domain import PlanDocumentV1, PlanDomainError


LINKED_WORKOUTS_FIELD = "linked_workouts"
MAX_LINKED_WORKOUTS = 20


def _role(strength_count: int, rehab_count: int) -> str:
    if strength_count and rehab_count:
        return "mixed"
    if rehab_count:
        return "rehab"
    return "strength"


def _uuid(value: Any) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise PlanDomainError(
            "invalid_section",
            "each linked workout must contain a valid workout_template_id",
        ) from error


def _frequency(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 14:
        raise PlanDomainError(
            "invalid_section",
            "linked workout sessions_per_week must be an integer from 0 through 14",
        )
    return value


def _notes(value: Any) -> str:
    cleaned = str(value or "").strip()
    if len(cleaned) > 500:
        raise PlanDomainError(
            "field_too_long",
            "linked workout schedule_notes must not exceed 500 characters",
        )
    return cleaned


@dataclass(frozen=True, slots=True)
class PlanWorkoutTemplateService:
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
              wt.workout_template_id,
              wt.name,
              wt.notes,
              wt.updated_at,
              count(wte.workout_template_exercise_id)::int as exercise_count,
              count(wte.workout_template_exercise_id) filter (
                where coalesce(me.exercise_role, 'strength') = 'strength'
              )::int as strength_exercise_count,
              count(wte.workout_template_exercise_id) filter (
                where coalesce(me.exercise_role, 'strength') = 'rehab'
              )::int as rehab_exercise_count
            from {self.schema}.workout_template wt
            left join {self.schema}.workout_template_exercise wte
              on wte.workout_template_id = wt.workout_template_id
            left join {self.schema}.my_exercise me
              on me.owner_user_id = wt.owner_user_id
             and me.exercise_id = wte.exercise_id
            where wt.owner_user_id = $1::uuid
              and wt.is_active = true
            group by
              wt.workout_template_id, wt.name, wt.notes, wt.updated_at
            order by lower(wt.name), wt.workout_template_id
            """,
            owner_user_id,
        )
        exercise_rows = await conn.fetch(
            f"""
            select
              wte.workout_template_id,
              wte.display_name_snapshot,
              wte.exercise_id,
              wte.sort_order,
              wte.planned_sets,
              wte.default_weight,
              wte.default_reps,
              coalesce(me.exercise_role, 'strength') as exercise_role
            from {self.schema}.workout_template_exercise wte
            join {self.schema}.workout_template wt
              on wt.workout_template_id = wte.workout_template_id
            left join {self.schema}.my_exercise me
              on me.owner_user_id = wt.owner_user_id
             and me.exercise_id = wte.exercise_id
            where wt.owner_user_id = $1::uuid
              and wt.is_active = true
            order by wte.workout_template_id, wte.sort_order, wte.created_at
            """,
            owner_user_id,
        )
        exercises_by_template: dict[str, list[dict[str, Any]]] = {}
        for exercise in exercise_rows:
            key = str(exercise["workout_template_id"])
            exercises_by_template.setdefault(key, []).append(
                {
                    "name": str(
                        exercise["display_name_snapshot"] or exercise["exercise_id"]
                    ),
                    "role": str(exercise["exercise_role"] or "strength"),
                    "planned_sets": int(exercise["planned_sets"] or 0),
                    "default_weight": float(exercise["default_weight"] or 0),
                    "default_reps": int(exercise["default_reps"] or 0),
                }
            )
        options: list[dict[str, Any]] = []
        for row in rows:
            strength_count = int(row["strength_exercise_count"] or 0)
            rehab_count = int(row["rehab_exercise_count"] or 0)
            options.append(
                {
                    "workout_template_id": str(row["workout_template_id"]),
                    "name": str(row["name"] or "Unnamed workout"),
                    "notes": str(row["notes"] or ""),
                    "template_updated_at": row["updated_at"].isoformat(),
                    "exercise_count": int(row["exercise_count"] or 0),
                    "strength_exercise_count": strength_count,
                    "rehab_exercise_count": rehab_count,
                    "role": _role(strength_count, rehab_count),
                    "exercises": exercises_by_template.get(
                        str(row["workout_template_id"]), []
                    ),
                }
            )
        return options

    async def materialize_document(
        self,
        conn: asyncpg.Connection,
        *,
        owner_user_id: uuid.UUID,
        document: PlanDocumentV1,
    ) -> PlanDocumentV1:
        raw = document.to_dict()
        training = dict(raw.get("training_targets") or {})
        linked = training.get(LINKED_WORKOUTS_FIELD)
        if linked is None:
            return document
        if not isinstance(linked, list):
            raise PlanDomainError(
                "invalid_section",
                "training_targets.linked_workouts must be a list",
            )
        if len(linked) > MAX_LINKED_WORKOUTS:
            raise PlanDomainError(
                "invalid_section",
                f"no more than {MAX_LINKED_WORKOUTS} workout templates may be linked",
            )

        selected: list[tuple[uuid.UUID, int, str]] = []
        seen: set[uuid.UUID] = set()
        for item in linked:
            if not isinstance(item, Mapping):
                raise PlanDomainError(
                    "invalid_section",
                    "each linked workout must be a JSON object",
                )
            template_id = _uuid(item.get("workout_template_id"))
            if template_id in seen:
                raise PlanDomainError(
                    "invalid_section",
                    "a workout template may be linked only once",
                )
            seen.add(template_id)
            selected.append(
                (
                    template_id,
                    _frequency(item.get("sessions_per_week", 1)),
                    _notes(item.get("schedule_notes")),
                )
            )

        snapshots: list[dict[str, Any]] = []
        for template_id, sessions_per_week, schedule_notes in selected:
            snapshot = await self._snapshot(
                conn,
                owner_user_id=owner_user_id,
                workout_template_id=template_id,
            )
            snapshots.append(
                {
                    "workout_template_id": str(template_id),
                    "sessions_per_week": sessions_per_week,
                    "schedule_notes": schedule_notes,
                    "prescription_snapshot": snapshot,
                }
            )

        training[LINKED_WORKOUTS_FIELD] = snapshots
        raw["training_targets"] = training
        return PlanDocumentV1.from_mapping(raw)

    async def _snapshot(
        self,
        conn: asyncpg.Connection,
        *,
        owner_user_id: uuid.UUID,
        workout_template_id: uuid.UUID,
    ) -> dict[str, Any]:
        template = await conn.fetchrow(
            f"""
            select workout_template_id, name, notes, updated_at
            from {self.schema}.workout_template
            where workout_template_id = $1::uuid
              and owner_user_id = $2::uuid
              and is_active = true
            """,
            workout_template_id,
            owner_user_id,
        )
        if template is None:
            raise PlanDomainError(
                "entity_out_of_scope",
                "a selected workout template is unavailable",
            )

        exercise_rows = await conn.fetch(
            f"""
            select
              wte.workout_template_exercise_id,
              wte.exercise_id,
              wte.display_name_snapshot,
              wte.sort_order,
              wte.set_type,
              wte.planned_sets,
              wte.default_weight,
              wte.default_reps,
              wte.flags,
              coalesce(me.exercise_role, 'strength') as exercise_role
            from {self.schema}.workout_template_exercise wte
            join {self.schema}.workout_template wt
              on wt.workout_template_id = wte.workout_template_id
            left join {self.schema}.my_exercise me
              on me.owner_user_id = wt.owner_user_id
             and me.exercise_id = wte.exercise_id
            where wte.workout_template_id = $1::uuid
              and wt.owner_user_id = $2::uuid
              and wt.is_active = true
            order by wte.sort_order, wte.created_at, wte.workout_template_exercise_id
            """,
            workout_template_id,
            owner_user_id,
        )
        exercise_ids = [row["workout_template_exercise_id"] for row in exercise_rows]
        segment_rows = (
            await conn.fetch(
                f"""
                select
                  workout_template_exercise_id,
                  segment_index,
                  label,
                  default_weight,
                  default_reps
                from {self.schema}.workout_template_exercise_segment
                where workout_template_exercise_id = any($1::uuid[])
                order by workout_template_exercise_id, segment_index
                """,
                exercise_ids,
            )
            if exercise_ids
            else []
        )
        segments: dict[str, list[dict[str, Any]]] = {}
        for row in segment_rows:
            segments.setdefault(str(row["workout_template_exercise_id"]), []).append(
                {
                    "segment_index": int(row["segment_index"]),
                    "label": str(row["label"] or ""),
                    "default_weight": float(row["default_weight"] or 0),
                    "default_reps": int(row["default_reps"] or 0),
                }
            )

        exercises: list[dict[str, Any]] = []
        strength_count = 0
        rehab_count = 0
        for row in exercise_rows:
            role = str(row["exercise_role"] or "strength")
            if role == "rehab":
                rehab_count += 1
            else:
                role = "strength"
                strength_count += 1
            exercise_key = str(row["workout_template_exercise_id"])
            exercises.append(
                {
                    "workout_template_exercise_id": exercise_key,
                    "exercise_id": str(row["exercise_id"]),
                    "name": str(row["display_name_snapshot"] or row["exercise_id"]),
                    "role": role,
                    "sort_order": int(row["sort_order"] or 0),
                    "set_type": str(row["set_type"] or "straight"),
                    "planned_sets": int(row["planned_sets"] or 0),
                    "default_weight": float(row["default_weight"] or 0),
                    "default_reps": int(row["default_reps"] or 0),
                    "flags": str(row["flags"] or ""),
                    "segments": segments.get(exercise_key, []),
                }
            )

        return {
            "name": str(template["name"] or "Unnamed workout"),
            "notes": str(template["notes"] or ""),
            "template_updated_at": template["updated_at"].isoformat(),
            "role": _role(strength_count, rehab_count),
            "exercise_count": len(exercises),
            "strength_exercise_count": strength_count,
            "rehab_exercise_count": rehab_count,
            "exercises": exercises,
        }
