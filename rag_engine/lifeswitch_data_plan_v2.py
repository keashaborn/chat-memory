from __future__ import annotations

"""Bounded, runtime-neutral projection planning for LifeSwitch query signals V2."""

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rag_engine.lifeswitch_query_signals_v2 import (
    LifeSwitchDomainV2,
    LifeSwitchQuerySignalsV2,
    OutputGrainV2,
)
from rag_engine.lifeswitch_temporal_semantics_v1 import LifeSwitchTemporalWindowV1


DATA_PLAN_V2_CONTRACT = "lifeswitch_data_plan_v2"
DATA_PLAN_V2_POLICY_VERSION = "lifeswitch_data_policy_v2_0"
MAX_CONTRACT_SELECTIONS = 6
DEFAULT_RUNTIME_SELECTIONS = 4
MAX_COMBINED_ROWS = 500
MAX_COMBINED_TOKENS = 1000

ProjectionIdV2 = Literal[
    "plan.current.v1",
    "nutrition.daily.v1",
    "nutrition.range.v1",
    "nutrition.adherence.v1",
    "training.sessions_by_day.v1",
    "training.exercise_frequency.v1",
    "training.exercise_progression.v1",
    "training.plan_adherence.v1",
    "conditioning.sessions_by_day.v1",
    "conditioning.plan_adherence.v1",
    "measurements.core_summary.v1",
]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DOMAIN_ORDER: tuple[LifeSwitchDomainV2, ...] = (
    "plan",
    "nutrition",
    "training",
    "conditioning",
    "measurements",
)
_PROJECTION_ORDER: dict[str, int] = {
    "plan.current.v1": 0,
    "nutrition.daily.v1": 10,
    "nutrition.range.v1": 11,
    "nutrition.adherence.v1": 12,
    "training.sessions_by_day.v1": 20,
    "training.exercise_frequency.v1": 21,
    "training.exercise_progression.v1": 22,
    "training.plan_adherence.v1": 23,
    "conditioning.sessions_by_day.v1": 30,
    "conditioning.plan_adherence.v1": 31,
    "measurements.core_summary.v1": 40,
}
_BUDGETS: dict[str, tuple[int, int]] = {
    "plan.current.v1": (2, 180),
    "nutrition.daily.v1": (50, 260),
    "nutrition.range.v1": (150, 320),
    "nutrition.adherence.v1": (150, 320),
    "training.sessions_by_day.v1": (200, 320),
    "training.exercise_frequency.v1": (50, 350),
    "training.exercise_progression.v1": (200, 450),
    "training.plan_adherence.v1": (100, 300),
    "conditioning.sessions_by_day.v1": (100, 180),
    "conditioning.plan_adherence.v1": (100, 220),
    "measurements.core_summary.v1": (48, 180),
}


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _sha256(value: Any) -> str:
    raw = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class LifeSwitchSelectionBudgetV2(_StrictFrozenModel):
    max_rows: int = Field(ge=0, le=MAX_COMBINED_ROWS)
    max_prompt_tokens: int = Field(ge=0, le=MAX_COMBINED_TOKENS)


class LifeSwitchDataSelectionV2(_StrictFrozenModel):
    projection_id: ProjectionIdV2
    domains: tuple[LifeSwitchDomainV2, ...]
    output_grain: OutputGrainV2
    windows: tuple[LifeSwitchTemporalWindowV1, ...] = Field(max_length=2)
    named_subject: str | None = Field(default=None, min_length=1, max_length=80)
    reason_codes: tuple[str, ...]
    row_budget: int = Field(ge=1, le=MAX_COMBINED_ROWS)
    token_budget: int = Field(ge=1, le=MAX_COMBINED_TOKENS)
    execution_ordinal: int = Field(ge=1, le=MAX_CONTRACT_SELECTIONS)

    @model_validator(mode="after")
    def coherent(self) -> "LifeSwitchDataSelectionV2":
        ordered = tuple(domain for domain in _DOMAIN_ORDER if domain in self.domains)
        if ordered != self.domains:
            raise ValueError("selection domains must be unique and ordered")
        if self.reason_codes != tuple(dict.fromkeys(self.reason_codes)):
            raise ValueError("selection reason codes must be unique and ordered")
        if self.named_subject is not None and self.projection_id != "training.exercise_progression.v1":
            raise ValueError("named subject is allowed only for exercise progression")
        return self


class LifeSwitchRejectedSelectionV2(_StrictFrozenModel):
    requested_domain: LifeSwitchDomainV2
    projection_id: str | None = Field(default=None, max_length=80, pattern=r"^[a-z][a-z0-9_.-]*$")
    reason_code: Literal[
        "HISTORICAL_PLAN_AUTHORITY_UNAVAILABLE",
        "MEASUREMENT_DATE_PROJECTION_UNAVAILABLE",
        "COMPARISON_PROJECTION_UNAVAILABLE",
        "PROJECTION_COUNT_LIMIT",
        "PROJECTION_BUDGET_LIMIT",
    ]
    data_access: Literal[False] = False
    row_budget: Literal[0] = 0
    token_budget: Literal[0] = 0


class LifeSwitchCombinedBudgetV2(_StrictFrozenModel):
    max_rows: int = Field(ge=0, le=MAX_COMBINED_ROWS)
    max_prompt_tokens: int = Field(ge=0, le=MAX_COMBINED_TOKENS)
    contract_max_selections: Literal[MAX_CONTRACT_SELECTIONS] = MAX_CONTRACT_SELECTIONS
    runtime_max_selections: int = Field(ge=1, le=MAX_CONTRACT_SELECTIONS)
    truncation_reason_codes: tuple[str, ...]


class LifeSwitchDataPlanV2(_StrictFrozenModel):
    contract_version: Literal[DATA_PLAN_V2_CONTRACT] = DATA_PLAN_V2_CONTRACT
    policy_version: Literal[DATA_PLAN_V2_POLICY_VERSION] = DATA_PLAN_V2_POLICY_VERSION
    status: Literal["OFF", "ACTIVE", "PARTIAL", "UNAVAILABLE"]
    query_context: Literal["validated_signals_v2"] = "validated_signals_v2"
    signal_manifest_sha256: str
    temporal_context_sha256: str
    requested_selection_count: int = Field(ge=0, le=MAX_CONTRACT_SELECTIONS)
    selected_projection_count: int = Field(ge=0, le=MAX_CONTRACT_SELECTIONS)
    selections: tuple[LifeSwitchDataSelectionV2, ...] = Field(max_length=MAX_CONTRACT_SELECTIONS)
    rejected_selections: tuple[LifeSwitchRejectedSelectionV2, ...] = Field(max_length=MAX_CONTRACT_SELECTIONS)
    combined_budget: LifeSwitchCombinedBudgetV2
    data_access: bool
    plan_manifest_sha256: str

    @field_validator(
        "signal_manifest_sha256",
        "temporal_context_sha256",
        "plan_manifest_sha256",
    )
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("invalid SHA-256")
        return value

    @model_validator(mode="after")
    def coherent_and_hashed(self) -> "LifeSwitchDataPlanV2":
        if self.selected_projection_count != len(self.selections):
            raise ValueError("selected projection count mismatch")
        if self.requested_selection_count != len(self.selections) + len(self.rejected_selections):
            raise ValueError("requested selection count mismatch")
        ids = [selection.projection_id for selection in self.selections]
        if len(ids) != len(set(ids)):
            raise ValueError("selected projection IDs must be unique")
        if ids != sorted(ids, key=lambda item: _PROJECTION_ORDER[item]):
            raise ValueError("selected projections are not deterministically ordered")
        if [selection.execution_ordinal for selection in self.selections] != list(range(1, len(self.selections) + 1)):
            raise ValueError("execution ordinals must be contiguous")
        rows = sum(selection.row_budget for selection in self.selections)
        tokens = sum(selection.token_budget for selection in self.selections)
        if rows != self.combined_budget.max_rows or tokens != self.combined_budget.max_prompt_tokens:
            raise ValueError("combined budget does not equal selection budgets")
        if self.status == "OFF":
            if self.data_access or self.selections or self.rejected_selections or rows or tokens:
                raise ValueError("OFF plan requires zero access and zero budget")
        elif self.status == "ACTIVE":
            if not self.data_access or not self.selections or self.rejected_selections:
                raise ValueError("ACTIVE plan requires only selected projections")
        elif self.status == "PARTIAL":
            if not self.data_access or not self.selections or not self.rejected_selections:
                raise ValueError("PARTIAL plan requires selections and rejections")
        elif self.status == "UNAVAILABLE":
            if self.data_access or self.selections or not self.rejected_selections or rows or tokens:
                raise ValueError("UNAVAILABLE plan requires zero reads")
        payload = self.model_dump(mode="json", exclude={"plan_manifest_sha256"})
        if self.plan_manifest_sha256 != _sha256(payload):
            raise ValueError("data plan V2 hash mismatch")
        return self


def _projection_for(domain: LifeSwitchDomainV2, signals: LifeSwitchQuerySignalsV2) -> tuple[str | None, str | None]:
    windows = signals.temporal_request.windows
    if domain == "plan":
        if signals.plan_relationship == "historical_comparison":
            return None, "HISTORICAL_PLAN_AUTHORITY_UNAVAILABLE"
        return "plan.current.v1", None
    if domain == "nutrition":
        if signals.plan_relationship == "adherence":
            return "nutrition.adherence.v1", None
        if windows and len(windows) == 1 and windows[0].start_date == windows[0].end_date:
            return "nutrition.daily.v1", None
        return "nutrition.range.v1", None
    if domain == "training":
        if signals.named_subject and signals.output_grain == "trend":
            return "training.exercise_progression.v1", None
        if signals.output_grain == "ranking":
            return "training.exercise_frequency.v1", None
        if signals.plan_relationship == "adherence" and not windows:
            return "training.plan_adherence.v1", None
        return "training.sessions_by_day.v1", None
    if domain == "conditioning":
        if signals.plan_relationship == "adherence" and not windows:
            return "conditioning.plan_adherence.v1", None
        return "conditioning.sessions_by_day.v1", None
    if domain == "measurements":
        if windows:
            return None, "MEASUREMENT_DATE_PROJECTION_UNAVAILABLE"
        return "measurements.core_summary.v1", None
    raise AssertionError("unreachable domain")


def _empty_plan(
    signals: LifeSwitchQuerySignalsV2,
    *,
    status: Literal["OFF", "UNAVAILABLE"],
    rejections: tuple[LifeSwitchRejectedSelectionV2, ...],
    runtime_max_selections: int,
) -> LifeSwitchDataPlanV2:
    payload = {
        "contract_version": DATA_PLAN_V2_CONTRACT,
        "policy_version": DATA_PLAN_V2_POLICY_VERSION,
        "status": status,
        "query_context": "validated_signals_v2",
        "signal_manifest_sha256": signals.signal_manifest_sha256,
        "temporal_context_sha256": signals.temporal_request.resolution_sha256,
        "requested_selection_count": len(rejections),
        "selected_projection_count": 0,
        "selections": (),
        "rejected_selections": rejections,
        "combined_budget": LifeSwitchCombinedBudgetV2(
            max_rows=0,
            max_prompt_tokens=0,
            runtime_max_selections=runtime_max_selections,
            truncation_reason_codes=(),
        ),
        "data_access": False,
    }
    return LifeSwitchDataPlanV2(**payload, plan_manifest_sha256=_sha256(payload))


def create_lifeswitch_data_plan_v2(
    signals: LifeSwitchQuerySignalsV2,
    *,
    runtime_max_selections: int = DEFAULT_RUNTIME_SELECTIONS,
) -> LifeSwitchDataPlanV2:
    """Map validated signals to the existing projection allowlist only."""

    if not 1 <= runtime_max_selections <= MAX_CONTRACT_SELECTIONS:
        raise ValueError("runtime selection limit is outside the contract")
    if signals.status == "OFF":
        return _empty_plan(signals, status="OFF", rejections=(), runtime_max_selections=runtime_max_selections)
    if signals.status == "UNAVAILABLE":
        rejections = tuple(
            LifeSwitchRejectedSelectionV2(
                requested_domain=domain,
                reason_code=(
                    "COMPARISON_PROJECTION_UNAVAILABLE"
                    if signals.output_grain == "comparison"
                    else "PROJECTION_BUDGET_LIMIT"
                ),
            )
            for domain in signals.requested_domains
        )
        if not rejections:
            rejections = (
                LifeSwitchRejectedSelectionV2(
                    requested_domain="plan",
                    reason_code="PROJECTION_BUDGET_LIMIT",
                ),
            )
        return _empty_plan(signals, status="UNAVAILABLE", rejections=rejections, runtime_max_selections=runtime_max_selections)

    candidates: list[tuple[LifeSwitchDomainV2, str]] = []
    rejections_list: list[LifeSwitchRejectedSelectionV2] = []
    for domain in signals.requested_domains:
        projection, rejection = _projection_for(domain, signals)
        if rejection is not None:
            rejections_list.append(LifeSwitchRejectedSelectionV2(requested_domain=domain, reason_code=rejection))
        elif projection is not None and projection not in {item[1] for item in candidates}:
            candidates.append((domain, projection))

    candidates.sort(key=lambda item: _PROJECTION_ORDER[item[1]])
    selected_candidates = candidates[:runtime_max_selections]
    for domain, projection in candidates[runtime_max_selections:]:
        rejections_list.append(LifeSwitchRejectedSelectionV2(requested_domain=domain, projection_id=projection, reason_code="PROJECTION_COUNT_LIMIT"))

    selections: list[LifeSwitchDataSelectionV2] = []
    row_total = 0
    token_total = 0
    for domain, projection in selected_candidates:
        rows, tokens = _BUDGETS[projection]
        if row_total + rows > MAX_COMBINED_ROWS or token_total + tokens > MAX_COMBINED_TOKENS:
            rejections_list.append(LifeSwitchRejectedSelectionV2(requested_domain=domain, projection_id=projection, reason_code="PROJECTION_BUDGET_LIMIT"))
            continue
        subject = signals.named_subject if projection == "training.exercise_progression.v1" else None
        selections.append(
            LifeSwitchDataSelectionV2(
                projection_id=projection,
                domains=(domain,),
                output_grain=signals.output_grain,
                windows=signals.temporal_request.windows,
                named_subject=subject,
                reason_codes=("validated_query_signal",),
                row_budget=rows,
                token_budget=tokens,
                execution_ordinal=len(selections) + 1,
            )
        )
        row_total += rows
        token_total += tokens

    if selections and rejections_list:
        status: Literal["ACTIVE", "PARTIAL", "UNAVAILABLE"] = "PARTIAL"
    elif selections:
        status = "ACTIVE"
    else:
        return _empty_plan(signals, status="UNAVAILABLE", rejections=tuple(rejections_list), runtime_max_selections=runtime_max_selections)

    truncation = tuple(
        dict.fromkeys(
            rejection.reason_code
            for rejection in rejections_list
            if rejection.reason_code in {"PROJECTION_COUNT_LIMIT", "PROJECTION_BUDGET_LIMIT"}
        )
    )
    payload = {
        "contract_version": DATA_PLAN_V2_CONTRACT,
        "policy_version": DATA_PLAN_V2_POLICY_VERSION,
        "status": status,
        "query_context": "validated_signals_v2",
        "signal_manifest_sha256": signals.signal_manifest_sha256,
        "temporal_context_sha256": signals.temporal_request.resolution_sha256,
        "requested_selection_count": len(selections) + len(rejections_list),
        "selected_projection_count": len(selections),
        "selections": tuple(selections),
        "rejected_selections": tuple(rejections_list),
        "combined_budget": LifeSwitchCombinedBudgetV2(
            max_rows=row_total,
            max_prompt_tokens=token_total,
            runtime_max_selections=runtime_max_selections,
            truncation_reason_codes=truncation,
        ),
        "data_access": True,
    }
    return LifeSwitchDataPlanV2(**payload, plan_manifest_sha256=_sha256(payload))


__all__ = [
    "DATA_PLAN_V2_CONTRACT",
    "DATA_PLAN_V2_POLICY_VERSION",
    "DEFAULT_RUNTIME_SELECTIONS",
    "MAX_COMBINED_ROWS",
    "MAX_COMBINED_TOKENS",
    "MAX_CONTRACT_SELECTIONS",
    "LifeSwitchDataPlanV2",
    "LifeSwitchDataSelectionV2",
    "LifeSwitchRejectedSelectionV2",
    "create_lifeswitch_data_plan_v2",
]
