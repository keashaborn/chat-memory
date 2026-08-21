from __future__ import annotations

"""Fail-closed dispatch for LifeSwitch structured-context projections."""

import datetime as dt
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from seebx.capabilities.plans.domain_context import (
    LifeSwitchContextSectionV1,
    LifeSwitchDomainContextEnvelopeV1,
    TrustedLifeSwitchContextRequestV1,
    create_lifeswitch_context_envelope_v1,
)


_PROJECTION_BY_INTENT = {
    "OVERALL_STATUS": "plan_adherence",
    "PLAN": "current_plan",
    "NUTRITION_DAY": "nutrition_day",
    "NUTRITION_RANGE": "nutrition_range",
    "TRAINING_SUMMARY": "training_summary",
    "TRAINING_SESSION": "training_session",
    "TRAINING_RANGE": "training_range",
    "DAILY_STATUS_RANGE": "daily_status_range",
    "EXERCISE_PROGRESSION": "exercise_progression",
    "EXERCISE_FREQUENCY": "exercise_frequency",
    "LIFTING_PROGRESSION_SUMMARY": "lifting_progression_summary",
    "MEASUREMENTS_SUMMARY": "measurements_summary",
}

_ALLOWED_RELATIONS = {
    "current_plan": frozenset(
        {
            "lifeswitch_plan.plan_profile",
        }
    ),
    "plan_adherence": frozenset(
        {
            "lifeswitch_plan.plan_profile",
            "lifeswitch_nutrition.nutrition_day",
            "lifeswitch_nutrition.nutrition_entry",
            "lifeswitch_nutrition.my_food",
            "lifeswitch_nutrition.my_food_serving",
            "lifeswitch_nutrition.meal_item",
            "lifeswitch_training.training_session_current_v",
            "lifeswitch_training.training_set_log",
            "lifeswitch_training.training_set_effective_role_v1",
            "lifeswitch_training.conditioning_session_current_v",
            "public.lifeswitch_measurement_entries",
        }
    ),
    "nutrition_day": frozenset(
        {
            "lifeswitch_plan.plan_profile",
            "lifeswitch_nutrition.nutrition_day",
            "lifeswitch_nutrition.nutrition_entry",
            "lifeswitch_nutrition.my_food",
            "lifeswitch_nutrition.my_food_serving",
            "lifeswitch_nutrition.meal_item",
        }
    ),
    "nutrition_range": frozenset(
        {
            "lifeswitch_plan.plan_profile",
            "lifeswitch_nutrition.nutrition_day",
            "lifeswitch_nutrition.nutrition_entry",
            "lifeswitch_nutrition.my_food",
            "lifeswitch_nutrition.my_food_serving",
            "lifeswitch_nutrition.meal_item",
        }
    ),
    "training_summary": frozenset(
        {
            "lifeswitch_plan.plan_profile",
            "lifeswitch_training.training_session_current_v",
            "lifeswitch_training.training_set_log",
            "lifeswitch_training.training_set_effective_role_v1",
            "lifeswitch_training.conditioning_session_current_v",
        }
    ),
    "training_session": frozenset(
        {
            "lifeswitch_training.training_session_current_v",
            "lifeswitch_training.training_set_log",
            "lifeswitch_training.conditioning_session_current_v",
        }
    ),
    "training_range": frozenset(
        {
            "lifeswitch_training.training_session_current_v",
            "lifeswitch_training.training_set_log",
            "lifeswitch_training.training_set_effective_role_v1",
            "lifeswitch_training.conditioning_session_current_v",
        }
    ),
    "daily_status_range": frozenset(
        {
            "lifeswitch_plan.plan_profile",
            "lifeswitch_nutrition.nutrition_day",
            "lifeswitch_nutrition.nutrition_entry",
            "lifeswitch_nutrition.my_food",
            "lifeswitch_nutrition.my_food_serving",
            "lifeswitch_nutrition.meal_item",
            "lifeswitch_training.training_session_current_v",
            "lifeswitch_training.training_set_log",
            "lifeswitch_training.training_set_effective_role_v1",
            "lifeswitch_training.conditioning_session_current_v",
        }
    ),
    "exercise_progression": frozenset(
        {
            "lifeswitch_training.training_session_current_v",
            "lifeswitch_training.training_set_log",
            "lifeswitch_training.training_set_effective_role_v1",
        }
    ),
    "exercise_frequency": frozenset(
        {
            "lifeswitch_training.training_session_current_v",
            "lifeswitch_training.training_set_log",
            "lifeswitch_training.training_set_effective_role_v1",
        }
    ),
    "lifting_progression_summary": frozenset(
        {
            "lifeswitch_plan.plan_profile",
            "lifeswitch_training.training_session_current_v",
            "lifeswitch_training.training_set_log",
            "lifeswitch_training.training_set_effective_role_v1",
        }
    ),
    "measurements_summary": frozenset(
        {"public.lifeswitch_measurement_entries"}
    ),
}


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
    )


class LifeSwitchReadResultV1(_StrictFrozenModel):
    status: str
    plan_source: str
    record_count: int = Field(ge=0, le=500)
    source_relations: tuple[str, ...]
    payload: dict[str, Any] = Field(default_factory=dict, repr=False)

    @model_validator(mode="after")
    def coherent(self) -> "LifeSwitchReadResultV1":
        if self.status not in {"AVAILABLE", "EMPTY", "UNAVAILABLE"}:
            raise ValueError("invalid LifeSwitch read status")
        if self.plan_source not in {
            "not_requested",
            "canonical_plan",
            "unavailable",
        }:
            raise ValueError("invalid LifeSwitch plan source")
        if self.source_relations != tuple(dict.fromkeys(self.source_relations)):
            raise ValueError("source relations must be unique and ordered")
        if self.status == "AVAILABLE" and not self.payload:
            raise ValueError("available result requires payload")
        if self.status != "AVAILABLE" and self.payload:
            raise ValueError("non-available result cannot carry payload")
        return self


class LifeSwitchDomainReaderV1(Protocol):
    async def read_plan(
        self, *, owner_user_id: Any, owner_timezone: str
    ) -> LifeSwitchReadResultV1: ...

    async def read_overall_status(
        self, *, owner_user_id: Any, owner_timezone: str
    ) -> LifeSwitchReadResultV1: ...

    async def read_nutrition_day(
        self,
        *,
        owner_user_id: Any,
        owner_timezone: str,
        day: dt.date,
    ) -> LifeSwitchReadResultV1: ...

    async def read_nutrition_range(
        self,
        *,
        owner_user_id: Any,
        owner_timezone: str,
        start_date: dt.date,
        end_date: dt.date,
    ) -> LifeSwitchReadResultV1: ...

    async def read_training_summary(
        self,
        *,
        owner_user_id: Any,
        owner_timezone: str,
        start_date: dt.date,
        end_date: dt.date,
    ) -> LifeSwitchReadResultV1: ...

    async def read_training_session(
        self,
        *,
        owner_user_id: Any,
        owner_timezone: str,
        day: dt.date,
    ) -> LifeSwitchReadResultV1: ...

    async def read_training_range(
        self,
        *,
        owner_user_id: Any,
        owner_timezone: str,
        start_date: dt.date,
        end_date: dt.date,
    ) -> LifeSwitchReadResultV1: ...

    async def read_daily_status_range(
        self,
        *,
        owner_user_id: Any,
        owner_timezone: str,
        start_date: dt.date,
        end_date: dt.date,
    ) -> LifeSwitchReadResultV1: ...

    async def read_exercise_progression(
        self,
        *,
        owner_user_id: Any,
        owner_timezone: str,
        start_date: dt.date,
        end_date: dt.date,
        subject: str,
    ) -> LifeSwitchReadResultV1: ...

    async def read_exercise_frequency(
        self,
        *,
        owner_user_id: Any,
        owner_timezone: str,
        start_date: dt.date,
        end_date: dt.date,
    ) -> LifeSwitchReadResultV1: ...

    async def read_lifting_progression_summary(
        self,
        *,
        owner_user_id: Any,
        owner_timezone: str,
        start_date: dt.date,
        end_date: dt.date,
    ) -> LifeSwitchReadResultV1: ...

    async def read_measurements_summary(
        self,
        *,
        owner_user_id: Any,
        owner_timezone: str,
        start_date: dt.date,
        end_date: dt.date,
    ) -> LifeSwitchReadResultV1: ...


class LifeSwitchDomainContextProviderV1:
    def __init__(self, reader: LifeSwitchDomainReaderV1) -> None:
        self._reader = reader

    async def select(
        self,
        request: TrustedLifeSwitchContextRequestV1,
    ) -> LifeSwitchDomainContextEnvelopeV1:
        as_of = dt.datetime.now(ZoneInfo(request.owner_timezone)).date()
        plan = request.data_plan
        if not plan.data_access:
            return create_lifeswitch_context_envelope_v1(
                request=request,
                plan_source="not_requested",
                as_of_local_date=as_of,
                sections=(),
            )

        window = plan.window
        intent = plan.intent
        if intent == "PLAN":
            result = await self._reader.read_plan(
                owner_user_id=request.owner_user_id,
                owner_timezone=request.owner_timezone,
            )
        elif intent == "OVERALL_STATUS":
            result = await self._reader.read_overall_status(
                owner_user_id=request.owner_user_id,
                owner_timezone=request.owner_timezone,
            )
        elif intent == "NUTRITION_DAY":
            assert window is not None
            result = await self._reader.read_nutrition_day(
                owner_user_id=request.owner_user_id,
                owner_timezone=request.owner_timezone,
                day=window.end_date,
            )
        elif intent == "NUTRITION_RANGE":
            assert window is not None
            result = await self._reader.read_nutrition_range(
                owner_user_id=request.owner_user_id,
                owner_timezone=request.owner_timezone,
                start_date=window.start_date,
                end_date=window.end_date,
            )
        elif intent == "TRAINING_SUMMARY":
            assert window is not None
            result = await self._reader.read_training_summary(
                owner_user_id=request.owner_user_id,
                owner_timezone=request.owner_timezone,
                start_date=window.start_date,
                end_date=window.end_date,
            )
        elif intent == "TRAINING_SESSION":
            assert window is not None
            result = await self._reader.read_training_session(
                owner_user_id=request.owner_user_id,
                owner_timezone=request.owner_timezone,
                day=window.end_date,
            )
        elif intent == "TRAINING_RANGE":
            assert window is not None
            result = await self._reader.read_training_range(
                owner_user_id=request.owner_user_id,
                owner_timezone=request.owner_timezone,
                start_date=window.start_date,
                end_date=window.end_date,
            )
        elif intent == "DAILY_STATUS_RANGE":
            assert window is not None
            result = await self._reader.read_daily_status_range(
                owner_user_id=request.owner_user_id,
                owner_timezone=request.owner_timezone,
                start_date=window.start_date,
                end_date=window.end_date,
            )
        elif intent == "EXERCISE_PROGRESSION":
            assert window is not None and plan.subject is not None
            result = await self._reader.read_exercise_progression(
                owner_user_id=request.owner_user_id,
                owner_timezone=request.owner_timezone,
                start_date=window.start_date,
                end_date=window.end_date,
                subject=plan.subject,
            )
        elif intent == "EXERCISE_FREQUENCY":
            assert window is not None
            result = await self._reader.read_exercise_frequency(
                owner_user_id=request.owner_user_id,
                owner_timezone=request.owner_timezone,
                start_date=window.start_date,
                end_date=window.end_date,
            )
        elif intent == "LIFTING_PROGRESSION_SUMMARY":
            assert window is not None
            result = await self._reader.read_lifting_progression_summary(
                owner_user_id=request.owner_user_id,
                owner_timezone=request.owner_timezone,
                start_date=window.start_date,
                end_date=window.end_date,
            )
        elif intent == "MEASUREMENTS_SUMMARY":
            assert window is not None
            result = await self._reader.read_measurements_summary(
                owner_user_id=request.owner_user_id,
                owner_timezone=request.owner_timezone,
                start_date=window.start_date,
                end_date=window.end_date,
            )
        else:
            raise ValueError("unsupported LifeSwitch data intent")

        projection = _PROJECTION_BY_INTENT[intent]
        unexpected = set(result.source_relations) - _ALLOWED_RELATIONS[projection]
        if unexpected:
            raise ValueError("LifeSwitch reader returned an unauthorized source")
        if result.record_count > plan.budget.max_rows:
            raise ValueError("LifeSwitch reader exceeded the row budget")
        section = LifeSwitchContextSectionV1.create(
            projection=projection,
            status=result.status,
            window=window,
            record_count=result.record_count,
            source_relations=result.source_relations,
            payload=result.payload,
        )
        return create_lifeswitch_context_envelope_v1(
            request=request,
            plan_source=result.plan_source,
            as_of_local_date=as_of,
            sections=(section,),
        )


__all__ = [
    "LifeSwitchDomainContextProviderV1",
    "LifeSwitchDomainReaderV1",
    "LifeSwitchReadResultV1",
]
