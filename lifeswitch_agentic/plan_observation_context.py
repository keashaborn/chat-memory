from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Protocol
from zoneinfo import ZoneInfo

import asyncpg


NUTRITION_WINDOW_DAYS = 21
MEASUREMENT_WINDOW_DAYS = 90
TRAINING_WINDOW_DAYS = 28
CONDITIONING_WINDOW_DAYS = 21


@dataclass(frozen=True, slots=True)
class ObservationPermissions:
    nutrition: bool
    training: bool
    measurements: bool


class PlanObservationContextRepository(Protocol):
    async def summarize(
        self,
        conn: asyncpg.Connection,
        *,
        owner_user_id: uuid.UUID,
        owner_timezone: str,
        document: Mapping[str, Any],
        permissions: ObservationPermissions,
    ) -> Mapping[str, Any]: ...


_NUMBER = re.compile(r"(?<![A-Za-z])([0-9][0-9,]*(?:\.[0-9]+)?)")
_RANGE_SEPARATOR = re.compile(r"(?:\s(?:to|through)\s|\s*[-–—]\s*)", re.IGNORECASE)


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    text = str(value).strip().replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(float(value), digits)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _parse_numeric_target(
    value: Any,
    *,
    source_path: str,
    minimum: float,
    maximum: float,
) -> dict[str, Any]:
    if isinstance(value, Mapping):
        lower = _float(value.get("lower", value.get("minimum", value.get("min"))))
        upper = _float(value.get("upper", value.get("maximum", value.get("max"))))
        if lower is not None and upper is not None:
            low, high = sorted((lower, upper))
            if minimum <= low <= high <= maximum:
                return {
                    "status": "available",
                    "kind": "range",
                    "lower": _round(low),
                    "upper": _round(high),
                    "source_path": source_path,
                }
        point = _float(value.get("value", value.get("target")))
        if point is not None and minimum <= point <= maximum:
            return {
                "status": "available",
                "kind": "point",
                "value": _round(point),
                "source_path": source_path,
            }
        return {"status": "unavailable", "reason": "target_not_parseable"}

    point = _float(value)
    if point is not None and minimum <= point <= maximum:
        return {
            "status": "available",
            "kind": "point",
            "value": _round(point),
            "source_path": source_path,
        }

    text = str(value or "").strip()
    if not text:
        return {"status": "unavailable", "reason": "target_missing"}
    numbers = [float(item.replace(",", "")) for item in _NUMBER.findall(text)]
    if len(numbers) >= 2 and _RANGE_SEPARATOR.search(text):
        low, high = sorted(numbers[:2])
        if minimum <= low <= high <= maximum:
            return {
                "status": "available",
                "kind": "range",
                "lower": _round(low),
                "upper": _round(high),
                "source_path": source_path,
            }
    if len(numbers) == 1 and minimum <= numbers[0] <= maximum:
        return {
            "status": "available",
            "kind": "point",
            "value": _round(numbers[0]),
            "source_path": source_path,
        }
    return {"status": "unavailable", "reason": "target_not_parseable"}


def _section_target(
    document: Mapping[str, Any],
    *,
    section_name: str,
    candidate_keys: tuple[str, ...],
    minimum: float,
    maximum: float,
) -> dict[str, Any]:
    section = document.get(section_name)
    if not isinstance(section, Mapping):
        return {"status": "unavailable", "reason": "target_missing"}
    for key in candidate_keys:
        if key in section:
            return _parse_numeric_target(
                section[key],
                source_path=f"/{section_name}/{key}",
                minimum=minimum,
                maximum=maximum,
            )
    return {"status": "unavailable", "reason": "target_missing"}


def _calorie_target_config(document: Mapping[str, Any]) -> dict[str, Any]:
    section = document.get("nutrition_targets")
    if not isinstance(section, Mapping):
        section = {}
    raw = section.get("calorie_target")
    if isinstance(raw, Mapping) and any(
        key in raw for key in ("nominal_kcal", "daily_range_kcal", "rolling_average_kcal")
    ):
        nominal = _parse_numeric_target(
            raw.get("nominal_kcal"),
            source_path="/nutrition_targets/calorie_target/nominal_kcal",
            minimum=500,
            maximum=10000,
        )
        daily_range = _parse_numeric_target(
            raw.get("daily_range_kcal"),
            source_path="/nutrition_targets/calorie_target/daily_range_kcal",
            minimum=500,
            maximum=10000,
        )
        rolling_raw = raw.get("rolling_average_kcal")
        rolling_average = _parse_numeric_target(
            rolling_raw,
            source_path="/nutrition_targets/calorie_target/rolling_average_kcal",
            minimum=500,
            maximum=10000,
        )
        window_days = 0
        if isinstance(rolling_raw, Mapping):
            parsed_window = _float(rolling_raw.get("window_days"))
            if parsed_window is not None and 2 <= parsed_window <= 28:
                window_days = int(parsed_window)
        rolling_average["window_days"] = window_days or None
        return {
            "status": (
                "available"
                if any(item.get("status") == "available" for item in (nominal, daily_range, rolling_average))
                else "unavailable"
            ),
            "format": "structured",
            "nominal": nominal,
            "daily_range": daily_range,
            "rolling_average": rolling_average,
        }

    legacy = _section_target(
        document,
        section_name="nutrition_targets",
        candidate_keys=("calorie_target", "calorie_range", "calories", "target_kcal"),
        minimum=500,
        maximum=10000,
    )
    if legacy.get("kind") == "range":
        nominal = {"status": "unavailable", "reason": "target_missing"}
        daily_range = legacy
    else:
        nominal = legacy
        daily_range = {"status": "unavailable", "reason": "range_missing"}
    return {
        "status": legacy.get("status", "unavailable"),
        "format": "legacy",
        "nominal": nominal,
        "daily_range": daily_range,
        "rolling_average": {
            "status": "unavailable",
            "reason": "rolling_average_rule_missing",
            "window_days": None,
        },
    }


def _protein_target_config(document: Mapping[str, Any]) -> dict[str, Any]:
    section = document.get("nutrition_targets")
    if not isinstance(section, Mapping):
        section = {}
    raw = section.get("protein_target")
    if isinstance(raw, Mapping) and "minimum_g" in raw:
        minimum = _parse_numeric_target(
            raw.get("minimum_g"),
            source_path="/nutrition_targets/protein_target/minimum_g",
            minimum=10,
            maximum=1000,
        )
        weekly_raw = raw.get("weekly_adherence")
        weekly: dict[str, Any] = {
            "status": "unavailable",
            "reason": "weekly_protein_rule_missing",
        }
        if isinstance(weekly_raw, Mapping):
            mode = str(weekly_raw.get("mode") or "").strip()
            window = _float(weekly_raw.get("window_days"))
            required = _float(weekly_raw.get("required_hit_days"))
            if (
                mode == "days_hit"
                and window is not None
                and required is not None
                and 2 <= window <= 28
                and 1 <= required <= window
            ):
                weekly = {
                    "status": "available",
                    "mode": mode,
                    "window_days": int(window),
                    "required_hit_days": int(required),
                    "source_path": "/nutrition_targets/protein_target/weekly_adherence",
                }
        return {
            "status": minimum.get("status", "unavailable"),
            "format": "structured",
            "minimum": minimum,
            "weekly_adherence": weekly,
        }

    minimum = _section_target(
        document,
        section_name="nutrition_targets",
        candidate_keys=("protein_grams_minimum", "protein_minimum_g", "protein_g", "protein"),
        minimum=10,
        maximum=1000,
    )
    return {
        "status": minimum.get("status", "unavailable"),
        "format": "legacy",
        "minimum": minimum,
        "weekly_adherence": {
            "status": "unavailable",
            "reason": "weekly_protein_rule_missing",
        },
    }


def _boolean_rule(document: Mapping[str, Any], key: str) -> bool | None:
    section = document.get("nutrition_targets")
    if not isinstance(section, Mapping):
        return None
    raw = section.get("adherence_rule")
    if not isinstance(raw, Mapping) or not isinstance(raw.get(key), bool):
        return None
    return bool(raw[key])


def _data_sufficiency(observations: int, *, limited: int, sufficient: int) -> str:
    if observations >= sufficient:
        return "sufficient"
    if observations >= limited:
        return "limited"
    return "insufficient"


def _window(as_of: dt.date, days: int) -> tuple[dt.date, dt.date]:
    return as_of - dt.timedelta(days=days - 1), as_of


def _weekly_values(
    observations: Mapping[dt.date, float],
    *,
    as_of: dt.date,
) -> dict[str, Any]:
    current_start = as_of - dt.timedelta(days=6)
    prior_start = as_of - dt.timedelta(days=13)
    prior_end = as_of - dt.timedelta(days=7)
    current = [value for day, value in observations.items() if current_start <= day <= as_of]
    prior = [value for day, value in observations.items() if prior_start <= day <= prior_end]
    current_mean = _mean(current)
    prior_mean = _mean(prior)
    return {
        "current_7_day_average": _round(current_mean, 2),
        "current_7_day_observation_days": len(current),
        "prior_7_day_average": _round(prior_mean, 2),
        "prior_7_day_observation_days": len(prior),
        "change_current_minus_prior": (
            _round(current_mean - prior_mean, 2)
            if current_mean is not None and prior_mean is not None
            else None
        ),
        "data_sufficiency": (
            "sufficient"
            if len(current) >= 3 and len(prior) >= 3
            else "limited"
            if current and prior
            else "insufficient"
        ),
    }


def _change_summary(observations: Mapping[dt.date, float], *, unit: str) -> dict[str, Any]:
    if not observations:
        return {
            "observation_days": 0,
            "latest": None,
            "change_latest_minus_earliest": None,
            "unit": unit,
            "data_sufficiency": "insufficient",
        }
    ordered = sorted(observations.items())
    first_day, first_value = ordered[0]
    latest_day, latest_value = ordered[-1]
    span_days = (latest_day - first_day).days
    return {
        "observation_days": len(ordered),
        "earliest_date": first_day.isoformat(),
        "latest_date": latest_day.isoformat(),
        "latest": _round(latest_value, 2),
        "change_latest_minus_earliest": (
            _round(latest_value - first_value, 2) if len(ordered) >= 2 else None
        ),
        "unit": unit,
        "data_sufficiency": (
            "sufficient"
            if len(ordered) >= 3 and span_days >= 28
            else "limited"
            if len(ordered) >= 2
            else "insufficient"
        ),
    }


def _average_by_date(values: list[tuple[dt.date, float]]) -> dict[dt.date, float]:
    grouped: dict[dt.date, list[float]] = {}
    for day, value in values:
        grouped.setdefault(day, []).append(value)
    return {day: sum(items) / len(items) for day, items in grouped.items()}


def _permission_unavailable(scope: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "reason": "permission_not_granted",
        "required_permission": scope,
    }


class CanonicalPlanObservationContextRepository:
    async def summarize(
        self,
        conn: asyncpg.Connection,
        *,
        owner_user_id: uuid.UUID,
        owner_timezone: str,
        document: Mapping[str, Any],
        permissions: ObservationPermissions,
    ) -> Mapping[str, Any]:
        as_of = dt.datetime.now(ZoneInfo(owner_timezone)).date()
        nutrition = (
            await self._nutrition(conn, owner_user_id, document, as_of)
            if permissions.nutrition
            else _permission_unavailable("nutrition:view")
        )
        measurements = (
            await self._measurements(conn, owner_user_id, as_of)
            if permissions.measurements
            else _permission_unavailable("measurements:view")
        )
        if permissions.training:
            training = await self._training(conn, owner_user_id, document, as_of)
            conditioning = await self._conditioning(conn, owner_user_id, as_of)
        else:
            training = _permission_unavailable("training:view")
            conditioning = _permission_unavailable("training:view")

        return {
            "schema_version": 1,
            "as_of_local_date": as_of.isoformat(),
            "nutrition": nutrition,
            "measurements": measurements,
            "training": training,
            "conditioning": conditioning,
            "activity": {
                "status": "unavailable",
                "steps": {
                    "status": "not_collected",
                    "reason": "no_canonical_steps_observation_store",
                },
            },
            "recovery": {
                "status": "unavailable",
                "reason": "no_canonical_recovery_observation_store",
            },
            "source_provenance": {
                "nutrition": [
                    "lifeswitch_nutrition.nutrition_day",
                    "lifeswitch_nutrition.nutrition_entry",
                    "lifeswitch_nutrition.my_food",
                    "lifeswitch_nutrition.meal_item",
                ],
                "measurements": ["public.lifeswitch_measurement_entries"],
                "training": [
                    "lifeswitch_training.training_session_current_v",
                    "lifeswitch_training.training_set_log",
                ],
                "conditioning": [
                    "lifeswitch_training.conditioning_session_current_v"
                ],
            },
            "writes_performed": False,
        }

    async def _nutrition(
        self,
        conn: asyncpg.Connection,
        owner_user_id: uuid.UUID,
        document: Mapping[str, Any],
        as_of: dt.date,
    ) -> dict[str, Any]:
        start, end = _window(as_of, NUTRITION_WINDOW_DAYS)
        rows = await conn.fetch(
            """
            /* lifeswitch_plan_context:nutrition_daily_totals */
            select
              nd.day,
              count(e.nutrition_entry_id)::int as entry_count,
              coalesce(sum(
                case
                  when e.my_food_id is not null then
                    f.kcal * coalesce(e.qty_g, serving.grams * e.qty_servings) / 100.0
                  else meal_total.kcal
                end
              ), 0)::float as kcal,
              coalesce(sum(
                case
                  when e.my_food_id is not null then
                    f.protein_g * coalesce(e.qty_g, serving.grams * e.qty_servings) / 100.0
                  else meal_total.protein_g
                end
              ), 0)::float as protein_g
            from lifeswitch_nutrition.nutrition_day nd
            left join lifeswitch_nutrition.nutrition_entry e
              on e.nutrition_day_id = nd.nutrition_day_id
            left join lifeswitch_nutrition.my_food f
              on f.my_food_id = e.my_food_id
            left join lifeswitch_nutrition.my_food_serving serving
              on serving.my_food_serving_id = e.my_food_serving_id
             and serving.my_food_id = e.my_food_id
            left join lateral (
              select
                sum(food.kcal * coalesce(item.qty_g, item_serving.grams * item.qty_servings) / 100.0) as kcal,
                sum(food.protein_g * coalesce(item.qty_g, item_serving.grams * item.qty_servings) / 100.0) as protein_g
              from lifeswitch_nutrition.meal_item item
              join lifeswitch_nutrition.my_food food on food.my_food_id = item.my_food_id
              left join lifeswitch_nutrition.my_food_serving item_serving
                on item_serving.my_food_serving_id = item.my_food_serving_id
               and item_serving.my_food_id = item.my_food_id
              where item.meal_id = e.meal_id
            ) meal_total on true
            where nd.owner_user_id = $1
              and nd.day between $2 and $3
            group by nd.day
            order by nd.day
            """,
            owner_user_id,
            start,
            end,
        )
        observed = [row for row in rows if int(row["entry_count"] or 0) > 0]
        calories = {row["day"]: float(row["kcal"] or 0) for row in observed}
        protein = {row["day"]: float(row["protein_g"] or 0) for row in observed}
        calorie_target = _calorie_target_config(document)
        protein_target = _protein_target_config(document)
        daily_calorie_target = calorie_target["daily_range"]
        protein_minimum = protein_target["minimum"]

        calorie_adherence: dict[str, Any]
        if daily_calorie_target.get("kind") == "range":
            lower = float(daily_calorie_target["lower"])
            upper = float(daily_calorie_target["upper"])
            hits = sum(lower <= value <= upper for value in calories.values())
            calorie_adherence = {
                "status": "evaluable",
                "days_within_range": hits,
                "observed_days": len(calories),
                "percent_of_observed_days": _round(100 * hits / len(calories), 1) if calories else None,
            }
        elif calorie_target["nominal"].get("kind") == "point":
            calorie_adherence = {
                "status": "not_evaluable",
                "reason": "point_target_has_no_acceptable_range",
            }
        else:
            calorie_adherence = {
                "status": "not_evaluable",
                "reason": "calorie_target_unavailable",
            }

        protein_adherence: dict[str, Any]
        if protein_minimum.get("kind") == "range":
            lower = float(protein_minimum["lower"])
            upper = float(protein_minimum["upper"])
            hits = sum(lower <= value <= upper for value in protein.values())
            rule = "within_range"
        elif protein_minimum.get("kind") == "point":
            lower = float(protein_minimum["value"])
            hits = sum(value >= lower for value in protein.values())
            rule = "at_or_above_single_protein_target"
        else:
            hits = 0
            rule = "target_unavailable"
        protein_adherence = (
            {
                "status": "evaluable",
                "rule": rule,
                "days_meeting_target": hits,
                "observed_days": len(protein),
                "percent_of_observed_days": _round(100 * hits / len(protein), 1) if protein else None,
            }
            if protein_minimum.get("status") == "available"
            else {"status": "not_evaluable", "reason": "protein_target_unavailable"}
        )

        daily_combined_required = _boolean_rule(
            document, "daily_requires_both_calorie_and_protein"
        )
        if (
            daily_combined_required is True
            and calorie_adherence.get("status") == "evaluable"
            and protein_adherence.get("status") == "evaluable"
        ):
            calorie_lower = float(daily_calorie_target["lower"])
            calorie_upper = float(daily_calorie_target["upper"])
            protein_lower = float(
                protein_minimum.get("lower", protein_minimum.get("value"))
            )
            common_days = set(calories) & set(protein)
            combined_hits = sum(
                calorie_lower <= calories[day] <= calorie_upper
                and protein[day] >= protein_lower
                for day in common_days
            )
            daily_combined = {
                "status": "evaluable",
                "rule": "calorie_range_and_protein_minimum",
                "days_meeting_both": combined_hits,
                "observed_days": len(common_days),
                "percent_of_observed_days": (
                    _round(100 * combined_hits / len(common_days), 1) if common_days else None
                ),
            }
        elif daily_combined_required is True:
            daily_combined = {
                "status": "not_evaluable",
                "reason": "daily_component_rule_unavailable",
            }
        else:
            daily_combined = {
                "status": "not_configured",
                "reason": "combined_daily_rule_not_required",
            }

        rolling_rule = calorie_target["rolling_average"]
        protein_weekly_rule = protein_target["weekly_adherence"]
        weekly_combined_required = _boolean_rule(
            document, "weekly_requires_both_calorie_and_protein"
        )
        weekly_window = rolling_rule.get("window_days")
        same_window = (
            rolling_rule.get("status") == "available"
            and protein_weekly_rule.get("status") == "available"
            and weekly_window == protein_weekly_rule.get("window_days")
        )
        if same_window and weekly_window:
            period_start = as_of - dt.timedelta(days=int(weekly_window) - 1)
            period_days = [
                period_start + dt.timedelta(days=offset)
                for offset in range(int(weekly_window))
            ]
            logged_period_days = [day for day in period_days if day in calories and day in protein]
            full_window = len(logged_period_days) == int(weekly_window)
            calorie_average = _mean([calories[day] for day in logged_period_days])
            calorie_week_hit = (
                full_window
                and calorie_average is not None
                and float(rolling_rule["lower"]) <= calorie_average <= float(rolling_rule["upper"])
            )
            protein_hit_days = sum(
                protein[day] >= float(protein_minimum.get("lower", protein_minimum.get("value")))
                for day in logged_period_days
            )
            protein_week_hit = (
                full_window
                and protein_hit_days >= int(protein_weekly_rule["required_hit_days"])
            )
            weekly_evaluation = {
                "status": "evaluable" if full_window else "insufficient_data",
                "window_days": int(weekly_window),
                "logged_days": len(logged_period_days),
                "calorie_average": _round(calorie_average, 1),
                "calorie_average_within_range": calorie_week_hit if full_window else None,
                "protein_days_meeting_minimum": protein_hit_days,
                "protein_required_hit_days": int(protein_weekly_rule["required_hit_days"]),
                "protein_days_hit_rule_met": protein_week_hit if full_window else None,
                "combined_rule_required": weekly_combined_required,
                "combined_rule_met": (
                    calorie_week_hit and protein_week_hit
                    if full_window and weekly_combined_required is True
                    else None
                ),
            }
        else:
            weekly_evaluation = {
                "status": "not_evaluable",
                "reason": (
                    "weekly_rule_windows_do_not_match"
                    if rolling_rule.get("status") == "available"
                    and protein_weekly_rule.get("status") == "available"
                    else "weekly_rules_not_fully_configured"
                ),
            }

        return {
            "status": "available",
            "window": {"start": start.isoformat(), "end": end.isoformat(), "calendar_days": NUTRITION_WINDOW_DAYS},
            "logged_days": len(observed),
            "missing_log_days": NUTRITION_WINDOW_DAYS - len(observed),
            "data_sufficiency": _data_sufficiency(len(observed), limited=7, sufficient=14),
            "calories": {
                "target": calorie_target,
                "average_on_logged_days": _round(_mean(list(calories.values())), 1),
                "recent_week_comparison": _weekly_values(calories, as_of=as_of),
                "adherence": calorie_adherence,
            },
            "protein": {
                "target": protein_target,
                "average_on_logged_days": _round(_mean(list(protein.values())), 1),
                "recent_week_comparison": _weekly_values(protein, as_of=as_of),
                "adherence": protein_adherence,
            },
            "combined_daily_adherence": daily_combined,
            "weekly_evaluation": weekly_evaluation,
        }

    async def _measurements(
        self,
        conn: asyncpg.Connection,
        owner_user_id: uuid.UUID,
        as_of: dt.date,
    ) -> dict[str, Any]:
        start, end = _window(as_of, MEASUREMENT_WINDOW_DAYS)
        rows = await conn.fetch(
            """
            /* lifeswitch_plan_context:measurements */
            select local_date, weight_value, weight_unit, waist_value,
                   body_fat_percent, measurement_unit
            from public.lifeswitch_measurement_entries
            where owner_user_id = $1
              and is_active = true
              and local_date between $2 and $3
            order by local_date
            """,
            str(owner_user_id),
            start,
            end,
        )
        weight_values: list[tuple[dt.date, float]] = []
        waist_values: list[tuple[dt.date, float]] = []
        body_fat_values: list[tuple[dt.date, float]] = []
        unsupported_weight_units = 0
        unsupported_measurement_units = 0
        for row in rows:
            day = row["local_date"]
            weight = _float(row["weight_value"])
            if weight is not None:
                unit = str(row["weight_unit"] or "").strip().lower()
                if unit in {"lb", "lbs", "pound", "pounds"}:
                    weight_values.append((day, weight))
                elif unit in {"kg", "kgs", "kilogram", "kilograms"}:
                    weight_values.append((day, weight * 2.2046226218))
                else:
                    unsupported_weight_units += 1
            waist = _float(row["waist_value"])
            if waist is not None:
                unit = str(row["measurement_unit"] or "").strip().lower()
                if unit in {"in", "inch", "inches"}:
                    waist_values.append((day, waist))
                elif unit in {"cm", "centimeter", "centimeters"}:
                    waist_values.append((day, waist / 2.54))
                else:
                    unsupported_measurement_units += 1
            body_fat = _float(row["body_fat_percent"])
            if body_fat is not None:
                body_fat_values.append((day, body_fat))

        weight_by_day = _average_by_date(weight_values)
        waist_by_day = _average_by_date(waist_values)
        body_fat_by_day = _average_by_date(body_fat_values)
        return {
            "status": "available",
            "window": {"start": start.isoformat(), "end": end.isoformat(), "calendar_days": MEASUREMENT_WINDOW_DAYS},
            "weight": {
                "observation_days": len(weight_by_day),
                "latest_date": max(weight_by_day).isoformat() if weight_by_day else None,
                "weekly_average_trend_lb": _weekly_values(weight_by_day, as_of=as_of),
                "unsupported_unit_observations": unsupported_weight_units,
            },
            "waist": {
                **_change_summary(waist_by_day, unit="in"),
                "unsupported_unit_observations": unsupported_measurement_units,
            },
            "body_fat_percent": _change_summary(body_fat_by_day, unit="percent"),
        }

    async def _training(
        self,
        conn: asyncpg.Connection,
        owner_user_id: uuid.UUID,
        document: Mapping[str, Any],
        as_of: dt.date,
    ) -> dict[str, Any]:
        start, end = _window(as_of, TRAINING_WINDOW_DAYS)
        rows = await conn.fetch(
            """
            /* lifeswitch_plan_context:resistance_sessions */
            select s.day,
                   count(l.training_set_log_id)::int as active_set_count,
                   count(distinct l.exercise_id)::int as exercise_count,
                   count(l.training_set_log_id) filter (
                     where role_resolution.effective_role = 'strength'
                   )::int as strength_set_count,
                   count(distinct l.exercise_id) filter (
                     where role_resolution.effective_role = 'strength'
                   )::int as strength_exercise_count,
                   count(l.training_set_log_id) filter (
                     where role_resolution.effective_role = 'rehab'
                   )::int as rehab_set_count,
                   count(distinct l.exercise_id) filter (
                     where role_resolution.effective_role = 'rehab'
                   )::int as rehab_exercise_count,
                   count(l.training_set_log_id) filter (
                     where role_resolution.effective_role = 'unknown'
                   )::int as unknown_role_set_count,
                   count(distinct l.exercise_id) filter (
                     where role_resolution.effective_role = 'unknown'
                   )::int as unknown_role_exercise_count
            from lifeswitch_training.training_session_current_v s
            join lifeswitch_training.training_set_log l
              on l.training_session_id = s.training_session_id
             and l.owner_user_id = s.owner_user_id
             and l.is_active = true
            join lifeswitch_training.training_set_effective_role_v1 role_resolution
              on role_resolution.training_set_log_id = l.training_set_log_id
             and role_resolution.training_session_id = l.training_session_id
             and role_resolution.owner_user_id = l.owner_user_id
            where s.owner_user_id = $1
              and s.finished_at is not null
              and s.day between $2 and $3
            group by s.training_session_id, s.day
            order by s.day
            """,
            owner_user_id,
            start,
            end,
        )
        recent_start = as_of - dt.timedelta(days=6)
        prior_start = as_of - dt.timedelta(days=13)
        prior_end = as_of - dt.timedelta(days=7)
        target = _section_target(
            document,
            section_name="training_targets",
            candidate_keys=("strength_sessions_per_week", "workouts_per_week", "sessions_per_week"),
            minimum=0,
            maximum=14,
        )
        strength_rows = [row for row in rows if int(row["strength_set_count"] or 0) > 0]
        rehab_rows = [row for row in rows if int(row["rehab_set_count"] or 0) > 0]
        strength_recent = sum(recent_start <= row["day"] <= as_of for row in strength_rows)
        strength_prior = sum(prior_start <= row["day"] <= prior_end for row in strength_rows)
        if target.get("kind") == "point":
            planned = float(target["value"])
            strength_adherence = {
                "status": "evaluable",
                "rule": "sessions_at_or_above_target",
                "sessions_last_7_days": strength_recent,
                "planned_sessions_per_week": planned,
                "target_met": strength_recent >= planned,
                "rehab_exclusion_supported": True,
            }
        elif target.get("kind") == "range":
            lower = float(target["lower"])
            upper = float(target["upper"])
            strength_adherence = {
                "status": "evaluable",
                "rule": "sessions_within_target_range",
                "sessions_last_7_days": strength_recent,
                "planned_sessions_range": {"lower": lower, "upper": upper},
                "target_met": lower <= strength_recent <= upper,
                "rehab_exclusion_supported": True,
            }
        else:
            strength_adherence = {
                "status": "not_evaluable",
                "reason": "strength_session_target_unavailable",
                "rehab_exclusion_supported": True,
            }
        return {
            "status": "available",
            "window": {"start": start.isoformat(), "end": end.isoformat(), "calendar_days": TRAINING_WINDOW_DAYS},
            "all_logged_resistance_sessions": len(rows),
            "all_logged_active_sets": sum(int(row["active_set_count"] or 0) for row in rows),
            "sessions_last_7_days": sum(recent_start <= row["day"] <= as_of for row in rows),
            "sessions_prior_7_days": sum(prior_start <= row["day"] <= prior_end for row in rows),
            "strength_sessions": len(strength_rows),
            "strength_active_sets": sum(int(row["strength_set_count"] or 0) for row in rows),
            "strength_sessions_last_7_days": strength_recent,
            "strength_sessions_prior_7_days": strength_prior,
            "rehab_sessions": len(rehab_rows),
            "rehab_active_sets": sum(int(row["rehab_set_count"] or 0) for row in rows),
            "unknown_role_active_sets": sum(
                int(row["unknown_role_set_count"] or 0) for row in rows
            ),
            "unknown_role_exercises": sum(
                int(row["unknown_role_exercise_count"] or 0) for row in rows
            ),
            "effective_role_contract": "training_set_effective_role_v1",
            "effective_role_active_sets": {
                "strength": sum(int(row["strength_set_count"] or 0) for row in rows),
                "rehab": sum(int(row["rehab_set_count"] or 0) for row in rows),
                "unknown": sum(
                    int(row["unknown_role_set_count"] or 0) for row in rows
                ),
            },
            "rehab_only_sessions": sum(
                int(row["rehab_set_count"] or 0) > 0
                and int(row["strength_set_count"] or 0) == 0
                for row in rows
            ),
            "mixed_strength_rehab_sessions": sum(
                int(row["rehab_set_count"] or 0) > 0
                and int(row["strength_set_count"] or 0) > 0
                for row in rows
            ),
            "planned_sessions_per_week": target,
            "strength_adherence": strength_adherence,
            "progression": {
                "status": "not_computed",
                "reason": "progression_metric_is_not_implemented_yet",
            },
            "data_sufficiency": _data_sufficiency(len(strength_rows), limited=2, sufficient=6),
        }

    async def _conditioning(
        self,
        conn: asyncpg.Connection,
        owner_user_id: uuid.UUID,
        as_of: dt.date,
    ) -> dict[str, Any]:
        start, end = _window(as_of, CONDITIONING_WINDOW_DAYS)
        rows = await conn.fetch(
            """
            /* lifeswitch_plan_context:conditioning_sessions */
            select day, duration_min
            from lifeswitch_training.conditioning_session_current_v
            where owner_user_id = $1
              and day between $2 and $3
            order by day
            """,
            owner_user_id,
            start,
            end,
        )
        recent_start = as_of - dt.timedelta(days=6)
        prior_start = as_of - dt.timedelta(days=13)
        prior_end = as_of - dt.timedelta(days=7)
        return {
            "status": "available",
            "window": {"start": start.isoformat(), "end": end.isoformat(), "calendar_days": CONDITIONING_WINDOW_DAYS},
            "session_count": len(rows),
            "duration_minutes": _round(sum(float(row["duration_min"] or 0) for row in rows), 1),
            "sessions_last_7_days": sum(recent_start <= row["day"] <= as_of for row in rows),
            "sessions_prior_7_days": sum(prior_start <= row["day"] <= prior_end for row in rows),
            "data_sufficiency": _data_sufficiency(len(rows), limited=2, sufficient=6),
        }
