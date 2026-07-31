from __future__ import annotations

"""Read-only PostgreSQL projections for LifeSwitch chat context."""

import datetime as dt
import json
from typing import Any, Mapping
from uuid import UUID

import asyncpg

from lifeswitch_agentic.plan_observation_context import (
    CanonicalPlanObservationContextRepository,
    ObservationPermissions,
)
from rag_engine.lifeswitch_domain_provider_v1 import LifeSwitchReadResultV1


PLAN_AGENTIC_SOURCES = (
    "lifeswitch_agentic.plan_owner_state",
    "lifeswitch_agentic.plan_versions",
)
PLAN_LEGACY_SOURCES = ("lifeswitch_plan.plan_profile",)
NUTRITION_SOURCES = (
    "lifeswitch_nutrition.nutrition_day",
    "lifeswitch_nutrition.nutrition_entry",
    "lifeswitch_nutrition.my_food",
    "lifeswitch_nutrition.my_food_serving",
    "lifeswitch_nutrition.meal_item",
)
TRAINING_SOURCES = (
    "lifeswitch_training.training_session_current_v",
    "lifeswitch_training.training_set_log",
)
TRAINING_EFFECTIVE_ROLE_SOURCES = (
    *TRAINING_SOURCES,
    "lifeswitch_training.training_set_effective_role_v1",
)
CONDITIONING_SOURCES = (
    "lifeswitch_training.conditioning_session_current_v",
)
MEASUREMENT_SOURCES = ("public.lifeswitch_measurement_entries",)

def _json_object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise ValueError("stored LifeSwitch JSON must be an object")
    return dict(value)


def _number(value: Any) -> float:
    return round(float(value or 0), 1)


def _average(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def _compact_plan(document: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in (
        "phase",
        "phase_label",
        "primary_goal",
        "start_date",
        "review_date",
        "review_cadence",
    ):
        value = document.get(field)
        if value not in (None, "", {}):
            result[field] = value[:500] if isinstance(value, str) else value

    target_keys = {
        "nutrition_targets": (
            "calories",
            "target_kcal",
            "kcal",
            "calorie_target",
            "calorie_range",
            "protein_g",
            "target_protein_g",
            "protein",
            "protein_target",
            "protein_grams_minimum",
            "protein_minimum_g",
            "carbs_g",
            "fat_g",
        ),
        "training_targets": (
            "workouts_per_week",
            "strength_sessions_per_week",
            "sessions_per_week",
        ),
        "conditioning_targets": (
            "sessions_per_week",
            "minutes_per_week",
            "duration_min",
        ),
        "activity_targets": ("steps", "steps_per_day"),
        "recovery_targets": ("sleep_hours", "rest_days"),
    }
    for section, keys in target_keys.items():
        source = document.get(section)
        if not isinstance(source, Mapping):
            continue
        selected = {key: source[key] for key in keys if key in source}
        if selected:
            result[section] = selected
    return result


def _select_mapping(value: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {key: value[key] for key in keys if key in value}


def _compact_status_plan(document: Mapping[str, Any]) -> dict[str, Any]:
    plan = _compact_plan(document)
    goal = plan.get("primary_goal")
    if isinstance(goal, str):
        plan["primary_goal"] = goal[:240]
    for field in ("start_date", "review_date", "review_cadence", "training_targets"):
        plan.pop(field, None)
    return plan


def _compact_overall_observations(
    observations: Mapping[str, Any],
    *,
    macro_averages: Mapping[str, Any],
) -> dict[str, Any]:
    nutrition = observations.get("nutrition", {})
    calories = nutrition.get("calories", {}) if isinstance(nutrition, Mapping) else {}
    protein = nutrition.get("protein", {}) if isinstance(nutrition, Mapping) else {}
    training = observations.get("training", {})
    conditioning = observations.get("conditioning", {})
    measurements = observations.get("measurements", {})
    weight = measurements.get("weight", {}) if isinstance(measurements, Mapping) else {}
    return {
        "nutrition": {
            **_select_mapping(
                nutrition,
                ("status", "window", "logged_days", "missing_log_days", "data_sufficiency"),
            ),
            "macro_averages_on_logged_days": dict(macro_averages),
            "calorie_adherence": calories.get("adherence") if isinstance(calories, Mapping) else None,
            "protein_adherence": protein.get("adherence") if isinstance(protein, Mapping) else None,
            "combined_daily_adherence": nutrition.get("combined_daily_adherence") if isinstance(nutrition, Mapping) else None,
        },
        "training": _select_mapping(
            training,
            (
                "status",
                "window",
                "sessions_last_7_days",
                "sessions_prior_7_days",
                "strength_sessions_last_7_days",
                "strength_sessions_prior_7_days",
                "strength_active_sets",
                "rehab_sessions",
                "planned_sessions_per_week",
                "strength_adherence",
                "data_sufficiency",
            ),
        ),
        "conditioning": _select_mapping(
            conditioning,
            (
                "status",
                "window",
                "sessions_last_7_days",
                "sessions_prior_7_days",
                "duration_minutes",
                "data_sufficiency",
            ),
        ),
        "measurements": {
            **_select_mapping(measurements, ("status", "window")),
            "weight": _select_mapping(
                weight,
                ("observation_days", "latest_date", "weekly_average_trend_lb"),
            ),
        },
    }


class _LifeSwitchGatewayObservationConnectionV1:
    """Expose only typed gateway reads to the canonical observation projector."""

    def __init__(self, conn: asyncpg.Connection, *, context_id: UUID) -> None:
        self._conn = conn
        self._context_id = context_id

    async def fetch(self, query: str, *args: Any):
        if len(args) != 3:
            raise ValueError("LifeSwitch observation query arguments are invalid")
        start_date, end_date = args[1], args[2]
        if "lifeswitch_plan_context:nutrition_daily_totals" in query:
            function = "read_nutrition_daily_v1"
        elif "lifeswitch_plan_context:measurements" in query:
            function = "read_measurement_observations_v1"
        elif "lifeswitch_plan_context:resistance_sessions" in query:
            function = "read_resistance_sessions_v1"
        elif "lifeswitch_plan_context:conditioning_sessions" in query:
            function = "read_conditioning_sessions_v1"
        else:
            raise ValueError("unapproved LifeSwitch observation query")
        return await self._conn.fetch(
            f"select * from lifeswitch_chat.{function}($1,$2,$3)",
            self._context_id,
            start_date,
            end_date,
        )


class PostgresLifeSwitchDomainReaderV1:
    def __init__(
        self,
        conn: asyncpg.Connection,
        *,
        context_id: UUID,
        observation_repository: CanonicalPlanObservationContextRepository | None = None,
    ) -> None:
        self._conn = conn
        self._context_id = context_id
        self._observation_conn = _LifeSwitchGatewayObservationConnectionV1(
            conn,
            context_id=context_id,
        )
        self._observations = (
            observation_repository or CanonicalPlanObservationContextRepository()
        )

    async def _resolve_plan(
        self,
        owner_user_id: UUID,
    ) -> tuple[str, dict[str, Any], tuple[str, ...]]:
        row = await self._conn.fetchrow(
            "select * from lifeswitch_chat.read_plan_v1($1)",
            self._context_id,
        )
        if row is None:
            return "unavailable", {}, ()
        source = str(row["plan_source"])
        if source not in {"agentic_active", "legacy_fallback"}:
            raise ValueError("LifeSwitch plan gateway returned an invalid source")
        relations = PLAN_AGENTIC_SOURCES if source == "agentic_active" else PLAN_LEGACY_SOURCES
        return source, _json_object(row["document"]), relations

    async def _nutrition_rows(
        self,
        *,
        owner_user_id: UUID,
        start_date: dt.date,
        end_date: dt.date,
    ) -> list[asyncpg.Record]:
        return await self._conn.fetch(
            "select * from lifeswitch_chat.read_nutrition_daily_v1($1,$2,$3)",
            self._context_id,
            start_date,
            end_date,
        )

    @staticmethod
    def _nutrition_payload(
        rows: list[asyncpg.Record],
        *,
        start_date: dt.date,
        end_date: dt.date,
        targets: Mapping[str, Any],
        include_daily: bool,
        compact_daily: bool = False,
    ) -> tuple[int, dict[str, Any]]:
        observed = [row for row in rows if int(row["entry_count"] or 0) > 0]
        daily = [
            {
                "date": row["day"].isoformat(),
                "calories": _number(row["kcal"]),
                "protein_g": _number(row["protein_g"]),
                "carbs_g": _number(row["carbs_g"]),
                "fat_g": _number(row["fat_g"]),
            }
            for row in observed
        ]
        single_day = start_date == end_date
        payload: dict[str, Any] = {}
        if not single_day:
            payload["logged_days"] = len(observed)
            payload["calendar_days"] = (end_date - start_date).days + 1
            payload["averages_on_logged_days"] = {
                "calories": _average([item["calories"] for item in daily]),
                "protein_g": _average([item["protein_g"] for item in daily]),
                "carbs_g": _average([item["carbs_g"] for item in daily]),
                "fat_g": _average([item["fat_g"] for item in daily]),
            }
        if targets:
            payload["plan_targets"] = dict(targets)
        if include_daily and compact_daily:
            daily_columns = (
                "date",
                "calories",
                "protein_g",
                "carbs_g",
                "fat_g",
            )
            payload["daily_columns"] = list(daily_columns)
            payload["daily_rows"] = [
                [item[column] for column in daily_columns]
                for item in daily
            ]
        elif include_daily:
            payload["daily"] = daily
        return len(observed), payload

    async def read_plan(
        self, *, owner_user_id: UUID, owner_timezone: str
    ) -> LifeSwitchReadResultV1:
        source, document, relations = await self._resolve_plan(owner_user_id)
        if not document:
            return LifeSwitchReadResultV1(
                status="EMPTY",
                plan_source="unavailable",
                record_count=0,
                source_relations=(),
            )
        return LifeSwitchReadResultV1(
            status="AVAILABLE",
            plan_source=source,
            record_count=1,
            source_relations=relations,
            payload=_compact_plan(document),
        )

    async def read_overall_status(
        self, *, owner_user_id: UUID, owner_timezone: str
    ) -> LifeSwitchReadResultV1:
        source, document, plan_relations = await self._resolve_plan(owner_user_id)
        observations = await self._observations.summarize(
            self._observation_conn,
            owner_user_id=owner_user_id,
            owner_timezone=owner_timezone,
            document=document,
            permissions=ObservationPermissions(True, True, True),
        )
        as_of = dt.date.fromisoformat(str(observations["as_of_local_date"]))
        nutrition_start = as_of - dt.timedelta(days=20)
        macro_rows = await self._nutrition_rows(
            owner_user_id=owner_user_id,
            start_date=nutrition_start,
            end_date=as_of,
        )
        macro_count, macro_payload = self._nutrition_payload(
            macro_rows,
            start_date=nutrition_start,
            end_date=as_of,
            targets=document.get("nutrition_targets", {}),
            include_daily=False,
        )
        payload = {
            "plan": _compact_status_plan(document) if document else {"status": "unavailable"},
            **_compact_overall_observations(
                observations,
                macro_averages=macro_payload["averages_on_logged_days"],
            ),
        }
        training_count = int(observations["training"].get("all_logged_resistance_sessions", 0))
        conditioning_count = int(observations["conditioning"].get("session_count", 0))
        measurement_count = int(
            observations["measurements"].get("weight", {}).get("observation_days", 0)
        )
        relations = (
            *plan_relations,
            *NUTRITION_SOURCES,
            *TRAINING_EFFECTIVE_ROLE_SOURCES,
            *CONDITIONING_SOURCES,
            *MEASUREMENT_SOURCES,
        )
        return LifeSwitchReadResultV1(
            status="AVAILABLE",
            plan_source=source,
            record_count=macro_count + training_count + conditioning_count + measurement_count,
            source_relations=tuple(dict.fromkeys(relations)),
            payload=payload,
        )

    async def read_nutrition_day(
        self,
        *,
        owner_user_id: UUID,
        owner_timezone: str,
        day: dt.date,
    ) -> LifeSwitchReadResultV1:
        source, document, plan_relations = await self._resolve_plan(owner_user_id)
        rows = await self._nutrition_rows(
            owner_user_id=owner_user_id,
            start_date=day,
            end_date=day,
        )
        count, payload = self._nutrition_payload(
            rows,
            start_date=day,
            end_date=day,
            targets=document.get("nutrition_targets", {}),
            include_daily=True,
        )
        return LifeSwitchReadResultV1(
            status="AVAILABLE" if count else "EMPTY",
            plan_source=source,
            record_count=count,
            source_relations=tuple(dict.fromkeys((*plan_relations, *NUTRITION_SOURCES))),
            payload=payload if count else {},
        )

    async def read_nutrition_range(
        self,
        *,
        owner_user_id: UUID,
        owner_timezone: str,
        start_date: dt.date,
        end_date: dt.date,
    ) -> LifeSwitchReadResultV1:
        source, document, plan_relations = await self._resolve_plan(owner_user_id)
        rows = await self._nutrition_rows(
            owner_user_id=owner_user_id,
            start_date=start_date,
            end_date=end_date,
        )
        count, payload = self._nutrition_payload(
            rows,
            start_date=start_date,
            end_date=end_date,
            targets=document.get("nutrition_targets", {}),
            include_daily=True,
            compact_daily=True,
        )
        return LifeSwitchReadResultV1(
            status="AVAILABLE" if count else "EMPTY",
            plan_source=source,
            record_count=count,
            source_relations=tuple(dict.fromkeys((*plan_relations, *NUTRITION_SOURCES))),
            payload=payload if count else {},
        )

    async def read_training_summary(
        self,
        *,
        owner_user_id: UUID,
        owner_timezone: str,
        start_date: dt.date,
        end_date: dt.date,
    ) -> LifeSwitchReadResultV1:
        source, document, plan_relations = await self._resolve_plan(owner_user_id)
        observations = await self._observations.summarize(
            self._observation_conn,
            owner_user_id=owner_user_id,
            owner_timezone=owner_timezone,
            document=document,
            permissions=ObservationPermissions(False, True, False),
        )
        training = observations["training"]
        conditioning = observations["conditioning"]
        count = int(training.get("all_logged_resistance_sessions", 0)) + int(
            conditioning.get("session_count", 0)
        )
        return LifeSwitchReadResultV1(
            status="AVAILABLE" if count else "EMPTY",
            plan_source=source,
            record_count=count,
            source_relations=tuple(
                dict.fromkeys(
                    (
                        *plan_relations,
                        *TRAINING_EFFECTIVE_ROLE_SOURCES,
                        *CONDITIONING_SOURCES,
                    )
                )
            ),
            payload=(
                {
                    "training": training,
                    "conditioning": conditioning,
                    "plan_targets": document.get("training_targets", {}),
                }
                if count
                else {}
            ),
        )

    async def read_training_session(
        self,
        *,
        owner_user_id: UUID,
        owner_timezone: str,
        day: dt.date,
    ) -> LifeSwitchReadResultV1:
        rows = await self._conn.fetch(
            "select * from lifeswitch_chat.read_training_day_v1($1,$2)",
            self._context_id,
            day,
        )
        conditioning_rows = await self._conn.fetch(
            "select * from lifeswitch_chat.read_conditioning_sessions_v1($1,$2,$2)",
            self._context_id,
            day,
        )
        payload = {
            "date": day.isoformat(),
            "resistance_sessions": [
                {
                    "name": row["name"],
                    "sets": int(row["set_count"]),
                    "exercises": int(row["exercise_count"]),
                    "volume": _number(row["total_volume"]),
                }
                for row in rows
            ],
            "conditioning_sessions": [
                {
                    "name": row["name"],
                    "category": row["category"],
                    "modality": row["modality"],
                    "duration_min": _number(row["duration_min"]),
                    "intensity": row["intensity"],
                    "distance": _number(row["distance_value"]),
                    "distance_unit": row["distance_unit"],
                    "heart_rate_avg": _number(row["heart_rate_avg"]),
                    "recovery_impact": row["recovery_impact"],
                }
                for row in conditioning_rows
            ],
        }
        count = len(rows) + len(conditioning_rows)
        return LifeSwitchReadResultV1(
            status="AVAILABLE" if count else "EMPTY",
            plan_source="not_requested",
            record_count=count,
            source_relations=tuple(dict.fromkeys((*TRAINING_SOURCES, *CONDITIONING_SOURCES))),
            payload=payload if count else {},
        )

    async def read_exercise_progression(
        self,
        *,
        owner_user_id: UUID,
        owner_timezone: str,
        start_date: dt.date,
        end_date: dt.date,
        subject: str,
    ) -> LifeSwitchReadResultV1:
        rows = await self._conn.fetch(
            "select * from lifeswitch_chat.read_exercise_progression_v1($1,$2,$3,$4)",
            self._context_id,
            start_date,
            end_date,
            subject,
        )
        payload = {
            "exercise_query": subject,
            "observations": [
                {
                    "date": row["day"].isoformat(),
                    "exercise": row["exercise_name"],
                    "sets": int(row["set_count"]),
                    "reps": int(row["total_reps"]),
                    "max_load": _number(row["max_load"]),
                    "volume": _number(row["total_volume"]),
                    "load_unit": row["load_unit"],
                }
                for row in rows
            ],
        }
        return LifeSwitchReadResultV1(
            status="AVAILABLE" if rows else "EMPTY",
            plan_source="not_requested",
            record_count=len(rows),
            source_relations=TRAINING_EFFECTIVE_ROLE_SOURCES,
            payload=payload if rows else {},
        )

    async def read_exercise_frequency(
        self,
        *,
        owner_user_id: UUID,
        owner_timezone: str,
        start_date: dt.date,
        end_date: dt.date,
    ) -> LifeSwitchReadResultV1:
        rows = await self._conn.fetch(
            "select * from lifeswitch_chat.read_exercise_frequency_v1($1,$2,$3)",
            self._context_id,
            start_date,
            end_date,
        )
        columns = (
            "exercise_name",
            "effective_role",
            "set_count",
            "session_count",
            "first_day",
            "last_day",
            "role_conflict",
        )
        payload = {
            "columns": list(columns),
            "rows": [
                [
                    row["exercise_name"],
                    row["effective_role"],
                    int(row["set_count"]),
                    int(row["session_count"]),
                    row["first_day"].isoformat(),
                    row["last_day"].isoformat(),
                    bool(row["role_conflict"]),
                ]
                for row in rows
            ],
        }
        return LifeSwitchReadResultV1(
            status="AVAILABLE" if rows else "EMPTY",
            plan_source="not_requested",
            record_count=len(rows),
            source_relations=TRAINING_EFFECTIVE_ROLE_SOURCES,
            payload=payload if rows else {},
        )

    async def read_lifting_progression_summary(
        self,
        *,
        owner_user_id: UUID,
        owner_timezone: str,
        start_date: dt.date,
        end_date: dt.date,
    ) -> LifeSwitchReadResultV1:
        source, document, plan_relations = await self._resolve_plan(owner_user_id)
        rows = await self._conn.fetch(
            "select * from lifeswitch_chat.read_lifting_progression_summary_v2($1,$2,$3)",
            self._context_id,
            start_date,
            end_date,
        )
        columns = (
            "exercise_name",
            "exposure_count",
            "set_count",
            "first_day",
            "last_day",
            "first_set_count",
            "latest_set_count",
            "first_max_load",
            "latest_max_load",
            "first_total_reps",
            "latest_total_reps",
            "first_total_volume",
            "latest_total_volume",
            "first_average_load",
            "latest_average_load",
            "first_load_unit",
            "latest_load_unit",
        )
        payload = {
            "plan_targets": document.get("training_targets", {}),
            "comparison_policy": {
                "basis": "first_latest_exposure",
                "same_sets_same_unit": "comparable",
                "different_sets": "normalize_per_set",
                "raw_totals_when_sets_differ": "work_only",
                "different_units": "not_comparable",
                "mixed_metrics": "use_average_load",
            },
            "columns": list(columns),
            "rows": [
                [
                    row["exercise_name"],
                    int(row["exposure_count"]),
                    int(row["set_count"]),
                    row["first_day"].isoformat(),
                    row["last_day"].isoformat(),
                    int(row["first_set_count"]),
                    int(row["latest_set_count"]),
                    _number(row["first_max_load"]),
                    _number(row["latest_max_load"]),
                    int(row["first_total_reps"]),
                    int(row["latest_total_reps"]),
                    _number(row["first_total_volume"]),
                    _number(row["latest_total_volume"]),
                    _number(row["first_average_load"]),
                    _number(row["latest_average_load"]),
                    row["first_load_unit"],
                    row["latest_load_unit"],
                ]
                for row in rows
            ],
        }
        return LifeSwitchReadResultV1(
            status="AVAILABLE" if rows else "EMPTY",
            plan_source=source,
            record_count=len(rows),
            source_relations=tuple(
                dict.fromkeys((*plan_relations, *TRAINING_EFFECTIVE_ROLE_SOURCES))
            ),
            payload=payload if rows else {},
        )

    async def read_measurements_summary(
        self,
        *,
        owner_user_id: UUID,
        owner_timezone: str,
        start_date: dt.date,
        end_date: dt.date,
    ) -> LifeSwitchReadResultV1:
        observations = await self._observations.summarize(
            self._observation_conn,
            owner_user_id=owner_user_id,
            owner_timezone=owner_timezone,
            document={},
            permissions=ObservationPermissions(False, False, True),
        )
        measurements = observations["measurements"]
        count = int(measurements.get("weight", {}).get("observation_days", 0))
        return LifeSwitchReadResultV1(
            status="AVAILABLE" if count else "EMPTY",
            plan_source="not_requested",
            record_count=count,
            source_relations=MEASUREMENT_SOURCES,
            payload=measurements if count else {},
        )


__all__ = [
    "PostgresLifeSwitchDomainReaderV1",
]
