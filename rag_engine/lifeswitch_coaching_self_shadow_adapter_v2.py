from __future__ import annotations

"""Fail-closed owner-only V1 to V2 projection adapter.

This module is intentionally shadow-only. It performs no reads, writes, prompt
rendering, persistence, network calls, or runtime registration. The caller must
provide a previously authenticated V1 request/envelope and a server-created V2
authorization snapshot.
"""

import datetime as dt
import re
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rag_engine.lifeswitch_coaching_contracts_v2 import (
    AuthorizationSnapshotV2,
    CoachingCapabilitiesV2,
    ProjectionAbsoluteWindowV1,
    ProjectionCalculationV1,
    ProjectionEnvelopeV1,
    ProjectionFieldStateV1,
    ProjectionId,
    ProjectionProvenanceV1,
    ProjectionResultValueV1,
    ProjectionSourceRefV1,
    canonical_json_bytes,
    canonical_sha256,
)
from rag_engine.lifeswitch_domain_context_v1 import (
    LIFESWITCH_CONTEXT_ENVELOPE_CONTRACT,
    LifeSwitchContextSectionV1,
    LifeSwitchDomainContextEnvelopeV1,
    TrustedLifeSwitchContextRequestV1,
)


SELF_S1_SHADOW_ADAPTER_CONTRACT = "lifeswitch_self_s1_shadow_adapter_v1"
SELF_S1_SHADOW_FIELD_POLICY = "lifeswitch.sensitivity_field_policy@1"
SELF_S1_SHADOW_GATEWAY_ID = "lifeswitch_domain_context_v1_shadow_adapter"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class LifeSwitchSelfShadowAdapterError(ValueError):
    """A source failed a shadow-adapter trust, schema, or policy boundary."""


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
        validate_default=True,
    )


SELF_S1_SHADOW_ADAPTER_MAP_V1: dict[str, dict[str, Any]] = {
    "current_plan": {
        "target_projection_id": "plan.current.v1",
        "required_scopes": ("plan:view",),
        "mode": "transform",
        "allowed_payload_fields": (
            "phase",
            "phase_label",
            "primary_goal",
            "start_date",
            "review_date",
            "review_cadence",
            "nutrition_targets",
            "training_targets",
            "conditioning_targets",
            "activity_targets",
            "recovery_targets",
        ),
    },
    "nutrition_day": {
        "target_projection_id": "nutrition.range.v1",
        "required_scopes": ("nutrition:view",),
        "mode": "transform",
        "allowed_payload_fields": ("plan_targets", "daily"),
    },
    "nutrition_range": {
        "target_projection_id": "nutrition.range.v1",
        "required_scopes": ("nutrition:view",),
        "mode": "transform",
        "allowed_payload_fields": (
            "logged_days",
            "calendar_days",
            "averages_on_logged_days",
            "plan_targets",
            "daily_columns",
            "daily_rows",
        ),
    },
    "exercise_frequency": {
        "target_projection_id": "training.exercise_frequency.v1",
        "required_scopes": ("training:view",),
        "mode": "transform",
        "allowed_payload_fields": ("columns", "rows"),
    },
    "exercise_progression": {
        "target_projection_id": "training.exercise_progression.v1",
        "required_scopes": ("training:view",),
        "mode": "transform",
        "allowed_payload_fields": ("exercise_query", "observations"),
    },
    "training_session": {
        "target_projection_id": "conditioning.sessions_by_day.v1",
        "required_scopes": ("training:view",),
        "mode": "conditioning_only",
        "allowed_payload_fields": (
            "date",
            "resistance_sessions",
            "conditioning_sessions",
        ),
    },
    "training_range": {
        "target_projection_id": "training.sessions_by_day.v1",
        "required_scopes": ("training:view",),
        "mode": "transform",
        "allowed_payload_fields": ("columns", "rows"),
    },
    "measurements_summary": {
        "target_projection_id": "measurements.core_summary.v1",
        "required_scopes": ("measurements:view",),
        "mode": "decision_blocked",
        "decision_block": "D03_MEASUREMENT_COLLISIONS",
        "allowed_payload_fields": (),
    },
}

_EXCLUDED_V1_PROJECTIONS = frozenset(
    {
        "plan_adherence",
        "training_summary",
        "lifting_progression_summary",
        "daily_status_range",
    }
)

_PLAN_TARGET_FIELDS = {
    "nutrition_targets": {
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
    },
    "training_targets": {
        "workouts_per_week",
        "strength_sessions_per_week",
        "sessions_per_week",
    },
    "conditioning_targets": {
        "sessions_per_week",
        "minutes_per_week",
        "duration_min",
    },
    "activity_targets": {"steps", "steps_per_day"},
    "recovery_targets": {"sleep_hours", "rest_days"},
}

_PLAN_AGENTIC_RELATIONS = {
    "lifeswitch_agentic.plan_owner_state",
    "lifeswitch_agentic.plan_versions",
}
_NUTRITION_RELATIONS = {
    *_PLAN_AGENTIC_RELATIONS,
    "lifeswitch_nutrition.nutrition_day",
    "lifeswitch_nutrition.nutrition_entry",
    "lifeswitch_nutrition.my_food",
    "lifeswitch_nutrition.my_food_serving",
    "lifeswitch_nutrition.meal_item",
}
_TRAINING_RELATIONS = {
    "lifeswitch_training.training_session_current_v",
    "lifeswitch_training.training_set_log",
    "lifeswitch_training.training_set_effective_role_v1",
}
_TRAINING_DAY_RELATIONS = {
    "lifeswitch_training.training_session_current_v",
    "lifeswitch_training.training_set_log",
    "lifeswitch_training.conditioning_session_current_v",
}
_MEASUREMENT_RELATIONS = {"public.lifeswitch_measurement_entries"}

_ALLOWED_RELATIONS = {
    "current_plan": _PLAN_AGENTIC_RELATIONS,
    "nutrition_day": _NUTRITION_RELATIONS,
    "nutrition_range": _NUTRITION_RELATIONS,
    "exercise_frequency": _TRAINING_RELATIONS,
    "exercise_progression": _TRAINING_RELATIONS,
    "training_session": _TRAINING_DAY_RELATIONS,
    "training_range": {
        *_TRAINING_RELATIONS,
        "lifeswitch_training.conditioning_session_current_v",
    },
    "measurements_summary": _MEASUREMENT_RELATIONS,
}


class LifeSwitchSelfShadowAdaptationV1(_StrictFrozenModel):
    contract_version: Literal[SELF_S1_SHADOW_ADAPTER_CONTRACT] = (
        SELF_S1_SHADOW_ADAPTER_CONTRACT
    )
    source_contract_version: Literal[LIFESWITCH_CONTEXT_ENVELOPE_CONTRACT] = (
        LIFESWITCH_CONTEXT_ENVELOPE_CONTRACT
    )
    source_envelope_sha256: str
    source_conversation_snapshot_sha256: str
    context_bridge_sha256: str
    authorization_snapshot_sha256: str
    source_projection: str = Field(min_length=1, max_length=80)
    target_projection_id: ProjectionId
    serialized_projection_bytes: int = Field(ge=0, le=32_768)
    capabilities: CoachingCapabilitiesV2 = Field(default_factory=CoachingCapabilitiesV2)
    result: ProjectionResultValueV1
    adaptation_sha256: str

    @field_validator(
        "source_envelope_sha256",
        "source_conversation_snapshot_sha256",
        "context_bridge_sha256",
        "authorization_snapshot_sha256",
        "adaptation_sha256",
    )
    @classmethod
    def valid_sha256(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("invalid SHA-256")
        return value

    @model_validator(mode="after")
    def exact_result_and_hash(self) -> "LifeSwitchSelfShadowAdaptationV1":
        if self.result.projection_id != self.target_projection_id:
            raise ValueError("target projection differs from adapted result")
        expected_bytes = (
            self.result.serialized_bytes
            if isinstance(self.result, ProjectionEnvelopeV1)
            else 0
        )
        if self.serialized_projection_bytes != expected_bytes:
            raise ValueError("adapted projection byte count mismatch")
        payload = self.model_dump(mode="json", exclude={"adaptation_sha256"})
        if self.adaptation_sha256 != canonical_sha256(payload):
            raise ValueError("adaptation hash mismatch")
        return self


def _fail(message: str) -> None:
    raise LifeSwitchSelfShadowAdapterError(message)


def _exact_keys(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        _fail(f"{label} contains unapproved fields")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(f"{label} must be an array")
    return value


def _string(value: Any, label: str, *, maximum: int = 240) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        _fail(f"{label} must be a bounded non-empty string")
    return value.strip()


def _number(value: Any, label: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{label} must be numeric")
    return value


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(f"{label} must be a non-negative integer")
    return value


def _date(value: Any, label: str) -> str:
    text = _string(value, label, maximum=10)
    try:
        dt.date.fromisoformat(text)
    except ValueError as exc:
        raise LifeSwitchSelfShadowAdapterError(f"{label} must be an ISO date") from exc
    return text


def _json_scalar_or_array(value: Any, label: str) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        canonical_json_bytes(value)
        return value
    if isinstance(value, list):
        result = [_json_scalar_or_array(item, label) for item in value]
        canonical_json_bytes(result)
        return result
    _fail(f"{label} must contain only typed scalar values")


def _window(
    request: TrustedLifeSwitchContextRequestV1,
    envelope: LifeSwitchDomainContextEnvelopeV1,
    section: LifeSwitchContextSectionV1,
) -> ProjectionAbsoluteWindowV1:
    if section.window is None:
        start = end = envelope.as_of_local_date.isoformat()
    else:
        start = section.window.start_date.isoformat()
        end = section.window.end_date.isoformat()
    return ProjectionAbsoluteWindowV1(
        start_local_date=start,
        end_local_date=end,
        timezone=request.owner_timezone,
    )


def _source_ref(section: LifeSwitchContextSectionV1) -> ProjectionSourceRefV1:
    return ProjectionSourceRefV1(
        source_id=f"{LIFESWITCH_CONTEXT_ENVELOPE_CONTRACT}.{section.projection}",
        gateway_id=SELF_S1_SHADOW_GATEWAY_ID,
        gateway_version=1,
        field_policy_version=1,
    )


def _provenance(
    envelope: LifeSwitchDomainContextEnvelopeV1,
    authorization: AuthorizationSnapshotV2,
    section: LifeSwitchContextSectionV1,
) -> ProjectionProvenanceV1:
    return ProjectionProvenanceV1(
        source_refs=(_source_ref(section),),
        snapshot_id=authorization.binding.context_snapshot_id,
        retrieved_at_utc=envelope.generated_at.astimezone(dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    )


def _state(field: str, state: str, decision_block: str | None = None):
    return ProjectionFieldStateV1(
        field=field,
        state=state,
        decision_block=decision_block,
    )


def _plan(payload: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[Any, ...], str]:
    allowed = set(SELF_S1_SHADOW_ADAPTER_MAP_V1["current_plan"]["allowed_payload_fields"])
    _exact_keys(payload, allowed, "current plan")
    data: dict[str, Any] = {}
    metadata: dict[str, Any] = {}
    for key in ("phase", "phase_label", "start_date", "review_date", "review_cadence"):
        if key in payload and payload[key] not in (None, ""):
            metadata[key] = _json_scalar_or_array(payload[key], f"plan {key}")
    if metadata:
        data["activation_and_effective_metadata"] = metadata
    if payload.get("primary_goal") not in (None, ""):
        data["routine_goal_direction"] = _string(
            payload["primary_goal"], "plan primary goal", maximum=500
        )
    sections: dict[str, Any] = {}
    for section, allowed_fields in _PLAN_TARGET_FIELDS.items():
        if section not in payload:
            continue
        values = _mapping(payload[section], section)
        _exact_keys(values, allowed_fields, section)
        sections[section] = {
            key: _json_scalar_or_array(value, f"{section}.{key}")
            for key, value in values.items()
        }
    if sections:
        data["requested_target_sections"] = sections
    if not data:
        _fail("current plan contains no approved fields")
    states = tuple(
        _state(field, "present" if field in data else "missing")
        for field in (
            "activation_and_effective_metadata",
            "routine_goal_direction",
            "requested_target_sections",
        )
    )
    status = "available" if all(item.state == "present" for item in states) else "partial"
    return data, states, status


def _nutrition_rows(
    payload: Mapping[str, Any], section: LifeSwitchContextSectionV1
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if section.projection == "nutrition_day":
        daily = _list(payload.get("daily"), "nutrition daily")
        for index, raw in enumerate(daily):
            item = _mapping(raw, f"nutrition daily row {index}")
            _exact_keys(
                item,
                {"date", "calories", "protein_g", "carbs_g", "fat_g"},
                f"nutrition daily row {index}",
            )
            result.append(dict(item))
    else:
        columns = _list(payload.get("daily_columns"), "nutrition daily columns")
        expected = ["date", "calories", "protein_g", "carbs_g", "fat_g"]
        if columns != expected:
            _fail("nutrition daily columns differ from the approved V1 shape")
        for index, raw in enumerate(_list(payload.get("daily_rows"), "nutrition daily rows")):
            row = _list(raw, f"nutrition daily row {index}")
            if len(row) != len(expected):
                _fail("nutrition daily row width is invalid")
            result.append(dict(zip(expected, row, strict=True)))
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(result):
        local_date = _date(row["date"], f"nutrition row {index} date")
        if local_date in seen:
            _fail("nutrition range contains a duplicate date")
        seen.add(local_date)
        normalized.append(
            {
                "local_date": local_date,
                "day_state": "present",
                "calories": _number(row["calories"], "nutrition calories"),
                "protein_g": _number(row["protein_g"], "nutrition protein"),
                "carbohydrate_g": _number(row["carbs_g"], "nutrition carbohydrate"),
                "fat_g": _number(row["fat_g"], "nutrition fat"),
            }
        )
    return sorted(normalized, key=lambda item: item["local_date"])


def _nutrition(
    payload: Mapping[str, Any],
    section: LifeSwitchContextSectionV1,
    projection_window: ProjectionAbsoluteWindowV1,
) -> tuple[dict[str, Any], tuple[Any, ...], str]:
    allowed = set(SELF_S1_SHADOW_ADAPTER_MAP_V1[section.projection]["allowed_payload_fields"])
    _exact_keys(payload, allowed, "nutrition range")
    if "plan_targets" in payload:
        targets = _mapping(payload["plan_targets"], "nutrition plan targets")
        _exact_keys(targets, _PLAN_TARGET_FIELDS["nutrition_targets"], "nutrition plan targets")
    if "averages_on_logged_days" in payload:
        averages = _mapping(payload["averages_on_logged_days"], "nutrition averages")
        _exact_keys(
            averages,
            {"calories", "protein_g", "carbs_g", "fat_g"},
            "nutrition averages",
        )
        for key, value in averages.items():
            if value is not None:
                _number(value, f"nutrition average {key}")
    rows = _nutrition_rows(payload, section)
    start = dt.date.fromisoformat(projection_window.start_local_date)
    end = dt.date.fromisoformat(projection_window.end_local_date)
    for row in rows:
        day = dt.date.fromisoformat(row["local_date"])
        if not start <= day <= end:
            _fail("nutrition row is outside the authorized window")
    calendar_days = (end - start).days + 1
    if "calendar_days" in payload and _integer(payload["calendar_days"], "calendar days") != calendar_days:
        _fail("nutrition calendar-day count is inconsistent")
    if "logged_days" in payload and _integer(payload["logged_days"], "logged days") != len(rows):
        _fail("nutrition logged-day count is inconsistent")
    missing_days = calendar_days - len(rows)
    data = {
        "requested_window": {
            "start_local_date": projection_window.start_local_date,
            "end_local_date": projection_window.end_local_date,
        },
        "daily_totals": rows,
        "coverage_counts": {
            "calendar_days": calendar_days,
            "logged_days": len(rows),
            "missing_days": missing_days,
        },
        "explicit_zero_fact": False,
    }
    states = (
        _state("requested_window", "present"),
        _state("daily_totals", "present" if rows else "absent"),
        _state("coverage_counts", "present"),
        _state("explicit_zero_fact", "missing"),
    )
    return data, states, "partial" if missing_days else "available"


def _frequency(
    payload: Mapping[str, Any], projection_window: ProjectionAbsoluteWindowV1
) -> tuple[dict[str, Any], tuple[Any, ...], str]:
    allowed = set(SELF_S1_SHADOW_ADAPTER_MAP_V1["exercise_frequency"]["allowed_payload_fields"])
    _exact_keys(payload, allowed, "exercise frequency")
    columns = _list(payload.get("columns"), "exercise frequency columns")
    expected = [
        "exercise_name",
        "effective_role",
        "set_count",
        "session_count",
        "first_day",
        "last_day",
        "role_conflict",
    ]
    if columns != expected:
        _fail("exercise-frequency columns differ from the approved V1 shape")
    exercises: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(_list(payload.get("rows"), "exercise frequency rows")):
        row = _list(raw, f"exercise frequency row {index}")
        if len(row) != len(expected):
            _fail("exercise-frequency row width is invalid")
        name = _string(row[0], "exercise name", maximum=160)
        role = _string(row[1], "effective role", maximum=32)
        _integer(row[2], "exercise set count")
        sessions = _integer(row[3], "exercise session count")
        _date(row[4], "exercise first day")
        _date(row[5], "exercise last day")
        if not isinstance(row[6], bool):
            _fail("exercise role-conflict flag must be boolean")
        if role not in {"strength", "rehab", "unknown"}:
            _fail("exercise role is outside the canonical V1 set")
        if role != "strength" or row[6]:
            continue
        key = name.casefold()
        if key in seen:
            _fail("exercise-frequency output contains a duplicate exercise")
        seen.add(key)
        exercises.append(
            {
                "exercise_name": name,
                "completed_exposure_count": sessions,
            }
        )
    data = {
        "requested_window": {
            "start_local_date": projection_window.start_local_date,
            "end_local_date": projection_window.end_local_date,
        },
        "exercises": exercises,
    }
    states = (
        _state("requested_window", "present"),
        _state("exercises", "present" if exercises else "explicit_zero"),
    )
    return data, states, "available"


def _progression(
    payload: Mapping[str, Any], projection_window: ProjectionAbsoluteWindowV1
) -> tuple[dict[str, Any], tuple[Any, ...], str, ProjectionCalculationV1]:
    allowed = set(SELF_S1_SHADOW_ADAPTER_MAP_V1["exercise_progression"]["allowed_payload_fields"])
    _exact_keys(payload, allowed, "exercise progression")
    query = _string(payload.get("exercise_query"), "exercise query", maximum=80)
    values: list[dict[str, Any]] = []
    resolved_names: set[str] = set()
    for index, raw in enumerate(_list(payload.get("observations"), "exercise observations")):
        item = _mapping(raw, f"exercise observation {index}")
        _exact_keys(
            item,
            {"date", "exercise", "sets", "reps", "max_load", "volume", "load_unit"},
            f"exercise observation {index}",
        )
        name = _string(item["exercise"], "resolved exercise name", maximum=160)
        resolved_names.add(name.casefold())
        values.append(
            {
                "local_date": _date(item["date"], "exercise observation date"),
                "set_count": _integer(item["sets"], "exercise set count"),
                "total_reps": _integer(item["reps"], "exercise rep count"),
                "max_load": _number(item["max_load"], "exercise max load"),
                "total_volume": _number(item["volume"], "exercise volume"),
                "comparable_unit": _string(item["load_unit"], "exercise load unit", maximum=32),
            }
        )
    if not values:
        _fail("exercise progression has no approved observations")
    if len(resolved_names) != 1:
        _fail("exercise progression did not resolve exactly one exercise")
    data = {
        "exercise_ref": query,
        "requested_window": {
            "start_local_date": projection_window.start_local_date,
            "end_local_date": projection_window.end_local_date,
        },
        "declared_metric": ["max_load", "total_reps", "total_volume"],
        "metric_values": sorted(values, key=lambda item: item["local_date"]),
        "calculation_version": 1,
    }
    states = (
        _state("exercise_ref", "present"),
        _state("requested_window", "present"),
        _state("metric_values", "present"),
    )
    calculation = ProjectionCalculationV1(
        calculation_id="named_exercise_progression_shadow_adapter",
        calculation_version=1,
        input_projection_ids=("training.exercise_history.v1",),
    )
    return data, states, "available", calculation


def _conditioning(
    payload: Mapping[str, Any], projection_window: ProjectionAbsoluteWindowV1
) -> tuple[dict[str, Any], tuple[Any, ...], str]:
    allowed = set(SELF_S1_SHADOW_ADAPTER_MAP_V1["training_session"]["allowed_payload_fields"])
    _exact_keys(payload, allowed, "training day")
    day = _date(payload.get("date"), "training day")
    if projection_window.start_local_date != day or projection_window.end_local_date != day:
        _fail("training-day payload differs from the authorized day")
    for index, raw in enumerate(_list(payload.get("resistance_sessions"), "resistance sessions")):
        item = _mapping(raw, f"resistance session {index}")
        _exact_keys(item, {"name", "sets", "exercises", "volume"}, f"resistance session {index}")
        _string(item["name"], "resistance session name", maximum=160)
        _integer(item["sets"], "resistance set count")
        _integer(item["exercises"], "resistance exercise count")
        _number(item["volume"], "resistance volume")
    sessions: list[dict[str, Any]] = []
    approved_fields = {
        "name",
        "category",
        "modality",
        "duration_min",
        "intensity",
        "distance",
        "distance_unit",
        "heart_rate_avg",
        "recovery_impact",
    }
    for index, raw in enumerate(_list(payload.get("conditioning_sessions"), "conditioning sessions")):
        item = _mapping(raw, f"conditioning session {index}")
        _exact_keys(item, approved_fields, f"conditioning session {index}")
        _string(item["name"], "conditioning session name", maximum=160)
        _string(item["category"], "conditioning category", maximum=80)
        modality = _string(item["modality"], "conditioning modality", maximum=120)
        duration = _number(item["duration_min"], "conditioning duration")
        intensity = item["intensity"]
        if intensity is not None:
            intensity = _string(intensity, "conditioning intensity", maximum=80)
        _number(item["distance"], "conditioning distance")
        if item["distance_unit"] is not None:
            _string(item["distance_unit"], "conditioning distance unit", maximum=32)
        _number(item["heart_rate_avg"], "conditioning heart rate")
        if item["recovery_impact"] is not None:
            _string(item["recovery_impact"], "conditioning recovery impact", maximum=80)
        sessions.append(
            {
                "local_date": day,
                "completion_state": "completed",
                "modality": modality,
                "duration": duration,
                "bounded_routine_intensity": intensity,
            }
        )
    data = {
        "requested_window": {"start_local_date": day, "end_local_date": day},
        "sessions": sessions,
    }
    states = (
        _state("requested_window", "present"),
        _state("sessions", "present" if sessions else "explicit_zero"),
    )
    return data, states, "available"


def _training_range(
    payload: Mapping[str, Any], projection_window: ProjectionAbsoluteWindowV1
) -> tuple[dict[str, Any], tuple[Any, ...], str]:
    allowed = set(SELF_S1_SHADOW_ADAPTER_MAP_V1["training_range"]["allowed_payload_fields"])
    _exact_keys(payload, allowed, "training range")
    columns = _list(payload.get("columns"), "training range columns")
    expected_columns = [
        "date",
        "strength_session_count",
        "strength_set_count",
        "strength_exercise_count",
        "rehab_session_count",
        "rehab_set_count",
        "rehab_exercise_count",
        "conditioning_session_count",
        "conditioning_minutes",
        "unknown_role_session_count",
        "unknown_role_set_count",
        "unknown_role_exercise_count",
    ]
    if columns != expected_columns:
        _fail("training range columns differ from the approved projection")
    sessions: list[dict[str, Any]] = []
    for index, raw in enumerate(_list(payload.get("rows"), "training range rows")):
        row = _list(raw, f"training range row {index}")
        if len(row) != len(expected_columns):
            _fail("training range row width differs from the approved projection")
        values = dict(zip(expected_columns, row))
        day = _date(values["date"], "training range day")
        if not (
            projection_window.start_local_date
            <= day
            <= projection_window.end_local_date
        ):
            _fail("training range row falls outside the authorized window")
        for role in ("strength", "rehab", "unknown_role"):
            session_count = _integer(
                values[f"{role}_session_count"],
                f"{role} session count",
            )
            set_count = _integer(values[f"{role}_set_count"], f"{role} set count")
            exercise_count = _integer(
                values[f"{role}_exercise_count"],
                f"{role} exercise count",
            )
            if session_count:
                sessions.append(
                    {
                        "local_date": day,
                        "completion_state": "completed",
                        "routine_session_type": (
                            "unknown" if role == "unknown_role" else role
                        ),
                        "duration": None,
                        "routine_set_and_rep_metrics": {
                            "session_count": session_count,
                            "active_set_count": set_count,
                            "exercise_count": exercise_count,
                        },
                    }
                )
        conditioning_count = _integer(
            values["conditioning_session_count"],
            "conditioning session count",
        )
        conditioning_minutes = _number(
            values["conditioning_minutes"],
            "conditioning minutes",
        )
        if conditioning_count:
            sessions.append(
                {
                    "local_date": day,
                    "completion_state": "completed",
                    "routine_session_type": "conditioning",
                    "duration": conditioning_minutes,
                    "routine_set_and_rep_metrics": {
                        "session_count": conditioning_count,
                    },
                }
            )
    data = {
        "requested_window": {
            "start_local_date": projection_window.start_local_date,
            "end_local_date": projection_window.end_local_date,
        },
        "sessions": sorted(
            sessions,
            key=lambda item: (item["local_date"], item["routine_session_type"]),
        ),
    }
    states = (
        _state("requested_window", "present"),
        _state("sessions", "present" if sessions else "explicit_zero"),
    )
    return data, states, "available"


def _decision_blocked_measurements(
    envelope: LifeSwitchDomainContextEnvelopeV1,
    authorization: AuthorizationSnapshotV2,
    section: LifeSwitchContextSectionV1,
    projection_window: ProjectionAbsoluteWindowV1,
) -> ProjectionEnvelopeV1:
    block = "D03_MEASUREMENT_COLLISIONS"
    return ProjectionEnvelopeV1(
        projection_id="measurements.core_summary.v1",
        status="decision_blocked",
        sensitivity="S1",
        data=None,
        field_states=tuple(
            _state(field, "missing", block)
            for field in ("weight", "waist", "body_fat_percent")
        ),
        provenance=_provenance(envelope, authorization, section),
        serialized_bytes=0,
        window=projection_window,
        decision_blocks=(block,),
    )


def _unavailable(
    *,
    target_projection_id: ProjectionId,
    envelope: LifeSwitchDomainContextEnvelopeV1,
    authorization: AuthorizationSnapshotV2,
    section: LifeSwitchContextSectionV1,
    projection_window: ProjectionAbsoluteWindowV1,
) -> ProjectionEnvelopeV1:
    return ProjectionEnvelopeV1(
        projection_id=target_projection_id,
        status="unavailable",
        sensitivity="S1",
        data=None,
        field_states=(_state("data", "absent"),),
        provenance=_provenance(envelope, authorization, section),
        serialized_bytes=0,
        window=projection_window,
    )


def _validate_bindings(
    *,
    request: TrustedLifeSwitchContextRequestV1,
    envelope: LifeSwitchDomainContextEnvelopeV1,
    authorization: AuthorizationSnapshotV2,
    target_projection_id: ProjectionId,
    required_scopes: tuple[str, ...],
    context_bridge_sha256: str,
) -> None:
    if authorization.binding.perspective != "self":
        _fail("shadow adapter is self-owned only")
    owner = str(request.owner_user_id)
    if str(request.authenticated_actor_user_id) != owner:
        _fail("V1 request actor is not the owner")
    if authorization.binding.actor_user_id != owner or authorization.binding.subject_user_id != owner:
        _fail("V2 authorization is not bound to the V1 owner")
    if authorization.binding.request_id != request.request_id or envelope.request_id != request.request_id:
        _fail("request binding mismatch")
    if authorization.binding.thread_id != str(request.thread_id) or envelope.thread_id != request.thread_id:
        _fail("thread binding mismatch")
    if authorization.binding.account_timezone != request.owner_timezone:
        _fail("account timezone binding mismatch")
    if envelope.owner_user_id != request.owner_user_id:
        _fail("envelope owner mismatch")
    if envelope.conversation_snapshot_sha256 != request.conversation_snapshot_sha256:
        _fail("V1 conversation snapshot mismatch")
    if envelope.query_sha256 != request.query_sha256:
        _fail("V1 query binding mismatch")
    if envelope.data_plan_sha256 != request.data_plan.plan_sha256:
        _fail("V1 data-plan binding mismatch")
    expected_bridge = canonical_sha256(
        {
            "context_snapshot_id": authorization.binding.context_snapshot_id,
            "conversation_snapshot_sha256": request.conversation_snapshot_sha256,
        }
    )
    if context_bridge_sha256 != expected_bridge:
        _fail("context snapshot bridge mismatch")
    if authorization.relationship_basis != "self" or authorization.effective_grants:
        _fail("self shadow authorization cannot carry delegated grants")
    if authorization.rechecks.pre_retrieval != "pass" or authorization.rechecks.pre_prompt != "pass":
        _fail("authorization rechecks did not pass")
    decisions = [
        item
        for item in authorization.projection_decisions
        if item.projection_id == target_projection_id
    ]
    if len(decisions) != 1:
        _fail("target projection lacks one authorization decision")
    decision = decisions[0]
    if decision.outcome != "authorized" or not decision.executed:
        _fail("target projection is not authorized")
    if decision.sensitivity_ceiling != "S1":
        _fail("self shadow adapter is limited to S1")
    if decision.required_scopes != required_scopes:
        _fail("projection authorization scopes differ from the adapter map")
    if decision.field_policy_version != SELF_S1_SHADOW_FIELD_POLICY:
        _fail("projection field-policy version mismatch")
    if authorization.budget.selected_projection_count != 1:
        _fail("self shadow adapter requires exactly one selected projection")


def adapt_lifeswitch_v1_envelope_to_self_shadow_projection_v2(
    *,
    request: TrustedLifeSwitchContextRequestV1,
    envelope: LifeSwitchDomainContextEnvelopeV1,
    authorization: AuthorizationSnapshotV2,
    context_bridge_sha256: str,
) -> LifeSwitchSelfShadowAdaptationV1:
    """Adapt one owner-only V1 section into one non-rendered V2 result."""

    if not _SHA256.fullmatch(context_bridge_sha256):
        _fail("context bridge must be a SHA-256")
    if envelope.status not in {"SELECTED", "PARTIAL"} or len(envelope.sections) != 1:
        _fail("shadow adapter requires exactly one selected V1 section")
    section = envelope.sections[0]
    mapping = SELF_S1_SHADOW_ADAPTER_MAP_V1.get(section.projection)
    if mapping is None or section.projection in _EXCLUDED_V1_PROJECTIONS:
        _fail("V1 projection is not approved for the self S1 shadow adapter")
    target_projection_id = mapping["target_projection_id"]
    required_scopes = mapping["required_scopes"]
    _validate_bindings(
        request=request,
        envelope=envelope,
        authorization=authorization,
        target_projection_id=target_projection_id,
        required_scopes=required_scopes,
        context_bridge_sha256=context_bridge_sha256,
    )
    if envelope.plan_source == "legacy_fallback":
        _fail("legacy plan fallback is not approved for V2 adaptation")
    allowed_relations = _ALLOWED_RELATIONS[section.projection]
    if not set(section.source_relations).issubset(allowed_relations):
        _fail("V1 section contains an unapproved source relation")
    projection_window = _window(request, envelope, section)
    if mapping["mode"] == "decision_blocked":
        result = _decision_blocked_measurements(
            envelope,
            authorization,
            section,
            projection_window,
        )
    elif section.status != "AVAILABLE":
        result = _unavailable(
            target_projection_id=target_projection_id,
            envelope=envelope,
            authorization=authorization,
            section=section,
            projection_window=projection_window,
        )
    else:
        payload = _mapping(section.payload, "V1 section payload")
        calculation = None
        if section.projection == "current_plan":
            data, states, status = _plan(payload)
        elif section.projection in {"nutrition_day", "nutrition_range"}:
            data, states, status = _nutrition(payload, section, projection_window)
        elif section.projection == "exercise_frequency":
            data, states, status = _frequency(payload, projection_window)
            calculation = ProjectionCalculationV1(
                calculation_id="exercise_frequency_shadow_adapter",
                calculation_version=1,
                input_projection_ids=("training.sessions_by_day.v1",),
            )
        elif section.projection == "exercise_progression":
            data, states, status, calculation = _progression(payload, projection_window)
        elif section.projection == "training_session":
            data, states, status = _conditioning(payload, projection_window)
        elif section.projection == "training_range":
            data, states, status = _training_range(
                payload,
                projection_window,
            )
        else:  # defensive: mapping and implementation must remain closed together
            _fail("V1 projection has no approved transformer")
        serialized_bytes = len(canonical_json_bytes(data))
        if serialized_bytes > 32_768:
            _fail("adapted projection exceeds the V2 context-byte ceiling")
        result = ProjectionEnvelopeV1(
            projection_id=target_projection_id,
            status=status,
            sensitivity="S1",
            data=data,
            field_states=states,
            provenance=_provenance(envelope, authorization, section),
            serialized_bytes=serialized_bytes,
            window=projection_window,
            calculation=calculation,
        )
    actual_bytes = result.serialized_bytes
    authorized_bytes = authorization.budget.serialized_projection_bytes
    if authorized_bytes not in {0, actual_bytes}:
        _fail("authorization byte accounting differs from the adapted result")
    capabilities = CoachingCapabilitiesV2()
    payload = {
        "contract_version": SELF_S1_SHADOW_ADAPTER_CONTRACT,
        "source_contract_version": LIFESWITCH_CONTEXT_ENVELOPE_CONTRACT,
        "source_envelope_sha256": envelope.envelope_sha256,
        "source_conversation_snapshot_sha256": request.conversation_snapshot_sha256,
        "context_bridge_sha256": context_bridge_sha256,
        "authorization_snapshot_sha256": canonical_sha256(authorization),
        "source_projection": section.projection,
        "target_projection_id": target_projection_id,
        "serialized_projection_bytes": actual_bytes,
        "capabilities": capabilities,
        "result": result,
    }
    hash_payload = {
        **payload,
        "capabilities": capabilities.model_dump(mode="json"),
        "result": result.model_dump(mode="json"),
    }
    return LifeSwitchSelfShadowAdaptationV1(
        **payload,
        adaptation_sha256=canonical_sha256(hash_payload),
    )


__all__ = [
    "LifeSwitchSelfShadowAdaptationV1",
    "LifeSwitchSelfShadowAdapterError",
    "SELF_S1_SHADOW_ADAPTER_CONTRACT",
    "SELF_S1_SHADOW_ADAPTER_MAP_V1",
    "adapt_lifeswitch_v1_envelope_to_self_shadow_projection_v2",
]
