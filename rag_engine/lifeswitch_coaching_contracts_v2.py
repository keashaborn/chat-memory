from __future__ import annotations

"""Frozen, runtime-neutral contracts for LifeSwitch Coaching Context V2.

This module is deliberately not imported by the production response path. It
defines the typed boundary that a later integration candidate may consume.
"""

import datetime as dt
import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator, model_validator


MAX_PROJECTIONS = 6
MAX_CONTEXT_BYTES = 32_768
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TIMEZONE_RE = re.compile(r"^[A-Za-z0-9_+./-]+$")
_FIELD_RE = re.compile(r"^[a-z][a-z0-9_.]*$")

ProjectionId: TypeAlias = Literal[
    "plan.current.v1",
    "plan.targets.current.v1",
    "plan.history.v1",
    "plan.recovery_adjustments.v1",
    "nutrition.daily.v1",
    "nutrition.range.v1",
    "nutrition.adherence.v1",
    "nutrition.meal_patterns.v1",
    "training.sessions_by_day.v1",
    "training.exercise_frequency.v1",
    "training.exercise_history.v1",
    "training.exercise_progression.v1",
    "training.plan_adherence.v1",
    "conditioning.sessions_by_day.v1",
    "conditioning.plan_adherence.v1",
    "measurements.core_summary.v1",
    "measurements.timeline.v1",
    "people.subject_authorization.v1",
    "messages.recent_direct.v1",
    "checkins.relevant.v1",
]
Sensitivity: TypeAlias = Literal["S0", "S1", "S2", "S3", "S4"]
ReadScope: TypeAlias = Literal[
    "plan:view",
    "nutrition:view",
    "training:view",
    "measurements:view",
    "messages:view",
]
CurrentReadScope: TypeAlias = Literal[
    "plan:view",
    "nutrition:view",
    "training:view",
    "measurements:view",
]
LifecycleState: TypeAlias = Literal[
    "present",
    "explicit_zero",
    "missing",
    "absent",
    "in_progress",
    "incomplete",
    "corrected",
    "deleted",
]
DecisionBlockId: TypeAlias = Literal[
    "D01_HISTORICAL_PLAN_AUTHORITY",
    "D02_RELATIVE_DATE_SEMANTICS",
    "D03_MEASUREMENT_COLLISIONS",
    "D04_RECOVERY_ADJUSTMENT_PRECEDENCE",
    "D05_HEALTH_ESCALATION_THRESHOLDS",
    "D06_FORECAST_METHOD",
]
TruncationReason: TypeAlias = Literal[
    "projection_count_limit",
    "projection_row_limit",
    "projection_byte_limit",
    "context_byte_limit",
]


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
        validate_default=True,
    )


def _require_uuid(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("invalid UUID") from exc
    if str(parsed) != value.lower():
        raise ValueError("UUID must use canonical hyphenated form")
    return value


def _require_datetime(value: str) -> str:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid RFC 3339 date-time") from exc
    if parsed.tzinfo is None:
        raise ValueError("date-time must include an offset")
    return value


def _require_date(value: str) -> str:
    try:
        dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("invalid ISO date") from exc
    return value


def _require_sha256(value: str) -> str:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError("invalid SHA-256")
    return value


def _unique(values: tuple[Any, ...], label: str) -> tuple[Any, ...]:
    comparable = [canonical_json_bytes(value) for value in values]
    if len(comparable) != len(set(comparable)):
        raise ValueError(f"duplicate {label}")
    return values


_FORBIDDEN_DATA_KEYS = {
    "credentials",
    "service_tokens",
    "authentication_headers",
    "token_bearing_urls",
    "raw_database_rows",
    "untyped_json",
    "raw_actor_user_id_in_trace",
    "raw_subject_user_id_in_trace",
    "raw_relationship_id_in_trace",
    "raw_permission_id_in_trace",
    "raw_record_id_in_trace",
    "owner_user_id",
    "actor_user_id",
    "subject_user_id",
    "relationship_id",
    "permission_id",
    "record_id",
    "entry_notes",
    "session_notes",
    "exercise_notes",
    "measurement_notes",
    "private_notes",
    "private_rationale",
    "private_reason",
    "private_text",
    "pain_text",
    "injury_text",
    "eating_risk_text",
    "digestion_text",
    "diagnosis",
    "medication",
    "health_material",
    "health_content",
    "opaque_json",
    "full_plan_document",
    "scan_json",
    "skinfolds_json",
    "photographs",
    "messages",
    "checkins",
}
_S0_SUBJECT_AUTHORIZATION_KEYS = {
    "perspective",
    "authorization_outcome",
    "scope_names_used",
    "authorization_epoch_digest",
    "relationship_binding_digest",
}


def _validate_projection_data(projection_id: str, value: Any) -> Any:
    def walk(item: Any) -> None:
        if item is None or isinstance(item, (str, int, float, bool)):
            return
        if isinstance(item, list):
            for child in item:
                walk(child)
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError("projection data keys must be strings")
                lowered = key.lower()
                if lowered.startswith("raw_") or lowered.endswith(
                    ("_user_id", "_relationship_id", "_permission_id", "_record_id")
                ):
                    raise ValueError("projection data contains a raw identifier")
                if lowered in _FORBIDDEN_DATA_KEYS:
                    raise ValueError("projection data contains a forbidden field")
                walk(child)
            return
        raise ValueError("projection data must be JSON-compatible")

    walk(value)
    canonical_json_bytes(value)
    if projection_id == "people.subject_authorization.v1" and isinstance(value, dict):
        unknown = set(value) - _S0_SUBJECT_AUTHORIZATION_KEYS
        if unknown:
            raise ValueError("subject authorization data must remain content-free")
    return value


def canonical_json_bytes(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class AuthorizationBindingV2(_StrictFrozenModel):
    request_id: str = Field(min_length=36, max_length=36)
    thread_id: str = Field(min_length=36, max_length=36)
    context_snapshot_id: str = Field(min_length=36, max_length=36)
    actor_user_id: str = Field(min_length=36, max_length=36)
    subject_user_id: str = Field(min_length=36, max_length=36)
    subject_selection: Literal["explicit"] = "explicit"
    perspective: Literal["self", "delegated"]
    account_timezone: str = Field(min_length=1, max_length=80)
    evaluated_at: str
    transaction_isolation: Literal["repeatable_read"] = "repeatable_read"
    transaction_access: Literal["read_only"] = "read_only"
    transaction_snapshot_digest: str

    @field_validator(
        "request_id",
        "thread_id",
        "context_snapshot_id",
        "actor_user_id",
        "subject_user_id",
    )
    @classmethod
    def valid_uuid(cls, value: str) -> str:
        return _require_uuid(value)

    @field_validator("account_timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        if not _TIMEZONE_RE.fullmatch(value):
            raise ValueError("invalid account timezone")
        return value

    @field_validator("evaluated_at")
    @classmethod
    def valid_datetime(cls, value: str) -> str:
        return _require_datetime(value)

    @field_validator("transaction_snapshot_digest")
    @classmethod
    def valid_snapshot_digest(cls, value: str) -> str:
        return _require_sha256(value)

    @model_validator(mode="after")
    def perspective_matches_identities(self) -> "AuthorizationBindingV2":
        same = self.actor_user_id == self.subject_user_id
        if self.perspective == "self" and not same:
            raise ValueError("self perspective requires actor and subject equality")
        if self.perspective == "delegated" and same:
            raise ValueError("delegated perspective requires a distinct subject")
        return self


class EffectiveGrantV2(_StrictFrozenModel):
    scope: CurrentReadScope
    permission_level: Literal["view", "comment", "edit", "admin"]
    grant_digest: str

    @field_validator("grant_digest")
    @classmethod
    def valid_digest(cls, value: str) -> str:
        return _require_sha256(value)


class ProjectionAuthorizationDecisionV2(_StrictFrozenModel):
    projection_id: ProjectionId
    outcome: Literal["authorized", "unavailable", "rejected", "error"]
    executed: bool
    required_scopes: tuple[ReadScope, ...]
    sensitivity_ceiling: Sensitivity
    field_policy_version: str = Field(min_length=1, max_length=160)
    internal_reason_code: Literal[
        "AUTH_UNAVAILABLE",
        "projection_count_limit",
        "projection_row_limit",
        "projection_byte_limit",
        "context_byte_limit",
        "incompatible_projection_version",
        "malformed_projection_payload",
        "field_policy_violation",
    ] | None

    @field_validator("required_scopes")
    @classmethod
    def unique_scopes(cls, value: tuple[ReadScope, ...]) -> tuple[ReadScope, ...]:
        return _unique(value, "required scope")

    @model_validator(mode="after")
    def execution_matches_outcome(self) -> "ProjectionAuthorizationDecisionV2":
        if self.outcome == "authorized":
            if not self.executed or self.internal_reason_code is not None:
                raise ValueError("authorized projections must execute without a reason")
        elif self.executed:
            raise ValueError("non-authorized projections cannot execute")
        return self


class AuthorizationRechecksV2(_StrictFrozenModel):
    pre_retrieval: Literal["pass", "fail"]
    pre_prompt: Literal["pass", "fail", "not_run"]
    delivery: Literal["pass", "fail", "not_run"]


class AuthorizationBudgetV2(_StrictFrozenModel):
    projection_count_limit: Literal[MAX_PROJECTIONS] = MAX_PROJECTIONS
    context_byte_limit: Literal[MAX_CONTEXT_BYTES] = MAX_CONTEXT_BYTES
    selected_projection_count: int = Field(ge=0, le=MAX_PROJECTIONS)
    serialized_projection_bytes: int = Field(ge=0, le=MAX_CONTEXT_BYTES)
    truncation_reasons: tuple[TruncationReason, ...]

    @field_validator("truncation_reasons")
    @classmethod
    def unique_reasons(
        cls, value: tuple[TruncationReason, ...]
    ) -> tuple[TruncationReason, ...]:
        return _unique(value, "truncation reason")


class AuthorizationSnapshotV2(_StrictFrozenModel):
    schema_id: Literal["lifeswitch.authorization_snapshot"] = (
        "lifeswitch.authorization_snapshot"
    )
    schema_version: Literal[2] = 2
    binding: AuthorizationBindingV2
    decision: Literal["allow", "partial", "deny"]
    authorization_epoch: str = Field(min_length=1, max_length=256)
    sensitivity_ceiling: Literal["S1", "S2", "S3"]
    relationship_basis: Literal["self", "accepted_relationship"]
    relationship_binding_digest: str | None = None
    effective_grants: tuple[EffectiveGrantV2, ...]
    projection_decisions: tuple[ProjectionAuthorizationDecisionV2, ...] = Field(
        min_length=1, max_length=20
    )
    rechecks: AuthorizationRechecksV2
    budget: AuthorizationBudgetV2

    @field_validator("relationship_binding_digest")
    @classmethod
    def valid_relationship_digest(cls, value: str | None) -> str | None:
        return None if value is None else _require_sha256(value)

    @field_validator("effective_grants")
    @classmethod
    def unique_grants(
        cls, value: tuple[EffectiveGrantV2, ...]
    ) -> tuple[EffectiveGrantV2, ...]:
        return _unique(value, "effective grant")

    @field_validator("projection_decisions")
    @classmethod
    def unique_projection_decisions(
        cls, value: tuple[ProjectionAuthorizationDecisionV2, ...]
    ) -> tuple[ProjectionAuthorizationDecisionV2, ...]:
        ids = [item.projection_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate projection authorization decision")
        return value

    @model_validator(mode="after")
    def exact_authorization(self) -> "AuthorizationSnapshotV2":
        delegated = self.binding.perspective == "delegated"
        if delegated:
            if self.sensitivity_ceiling != "S1":
                raise ValueError("delegated access is capped at S1")
            if self.relationship_basis != "accepted_relationship":
                raise ValueError("delegated access requires an accepted relationship")
            if self.relationship_binding_digest is None or not self.effective_grants:
                raise ValueError("delegated access requires a bound relationship and grants")
        else:
            if self.relationship_basis != "self":
                raise ValueError("self access requires self relationship basis")
            if self.relationship_binding_digest is not None or self.effective_grants:
                raise ValueError("self access cannot carry relationship grants")

        authorized = [item for item in self.projection_decisions if item.executed]
        if len(authorized) != self.budget.selected_projection_count:
            raise ValueError("authorization projection count differs from budget")
        if self.decision == "allow" and len(authorized) != len(self.projection_decisions):
            raise ValueError("allow decision requires every projection to be authorized")
        if self.decision == "deny" and authorized:
            raise ValueError("deny decision cannot authorize a projection")
        if self.decision == "partial" and (
            not authorized or len(authorized) == len(self.projection_decisions)
        ):
            raise ValueError("partial decision requires both allowed and denied projections")
        return self


class SelectedProjectionDecisionV1(_StrictFrozenModel):
    projection_id: ProjectionId
    status: Literal["selected"] = "selected"
    executed: Literal[True] = True
    execution_ordinal: int = Field(ge=1, le=MAX_PROJECTIONS)


class RejectedProjectionDecisionV1(_StrictFrozenModel):
    projection_id: ProjectionId
    status: Literal["rejected"] = "rejected"
    executed: Literal[False] = False
    reason: Literal["projection_count_limit"] = "projection_count_limit"


ProjectionSelectionDecisionV1: TypeAlias = (
    SelectedProjectionDecisionV1 | RejectedProjectionDecisionV1
)


class ProjectionCountTruncationV1(_StrictFrozenModel):
    reason: Literal["projection_count_limit"] = "projection_count_limit"
    limit: Literal[MAX_PROJECTIONS] = MAX_PROJECTIONS
    requested: int = Field(ge=7)
    selected: Literal[MAX_PROJECTIONS] = MAX_PROJECTIONS


class ProjectionSelectionResultV1(_StrictFrozenModel):
    schema_id: Literal["lifeswitch.projection_selection"] = (
        "lifeswitch.projection_selection"
    )
    schema_version: Literal[1] = 1
    requested_count: int = Field(ge=0)
    selected_count: int = Field(ge=0, le=MAX_PROJECTIONS)
    max_selected_count: Literal[MAX_PROJECTIONS] = MAX_PROJECTIONS
    decisions: tuple[ProjectionSelectionDecisionV1, ...]
    truncated: bool
    truncation: ProjectionCountTruncationV1 | None = None

    @model_validator(mode="after")
    def exact_selection(self) -> "ProjectionSelectionResultV1":
        if self.requested_count != len(self.decisions):
            raise ValueError("requested count differs from selection decisions")
        selected = [item for item in self.decisions if item.status == "selected"]
        if self.selected_count != len(selected):
            raise ValueError("selected count differs from selection decisions")
        ids = [item.projection_id for item in self.decisions]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate projection selection decision")
        ordinals = [item.execution_ordinal for item in selected]
        if ordinals != list(range(1, len(selected) + 1)):
            raise ValueError("selected execution ordinals must be contiguous")
        if self.truncated:
            if self.truncation is None:
                raise ValueError("truncated selection requires truncation metadata")
            if self.truncation.requested != self.requested_count:
                raise ValueError("selection truncation requested count mismatch")
        elif self.truncation is not None:
            raise ValueError("untruncated selection cannot carry truncation metadata")
        return self


class ProjectionFieldStateV1(_StrictFrozenModel):
    field: str = Field(min_length=1, max_length=128)
    state: LifecycleState
    decision_block: DecisionBlockId | None = None

    @field_validator("field")
    @classmethod
    def valid_field_name(cls, value: str) -> str:
        if not _FIELD_RE.fullmatch(value):
            raise ValueError("invalid field-state name")
        return value

    @model_validator(mode="after")
    def block_matches_state(self) -> "ProjectionFieldStateV1":
        if self.decision_block is not None and self.state not in {
            "missing",
            "incomplete",
        }:
            raise ValueError("decision block requires a non-present field state")
        return self


class ProjectionAbsoluteWindowV1(_StrictFrozenModel):
    start_local_date: str
    end_local_date: str
    timezone: str = Field(min_length=1, max_length=64)
    boundary: Literal["inclusive_local_dates"] = "inclusive_local_dates"
    relative_expression: None = None

    @field_validator("start_local_date", "end_local_date")
    @classmethod
    def valid_date(cls, value: str) -> str:
        return _require_date(value)

    @model_validator(mode="after")
    def ordered_window(self) -> "ProjectionAbsoluteWindowV1":
        if self.start_local_date > self.end_local_date:
            raise ValueError("projection date window is reversed")
        return self


class ProjectionSourceRefV1(_StrictFrozenModel):
    source_id: str = Field(min_length=1, max_length=128)
    gateway_id: str = Field(min_length=1, max_length=128)
    gateway_version: int = Field(ge=1)
    field_policy_version: int = Field(ge=1)


class ProjectionProvenanceV1(_StrictFrozenModel):
    source_refs: tuple[ProjectionSourceRefV1, ...] = Field(max_length=8)
    snapshot_id: str = Field(min_length=1, max_length=128)
    retrieved_at_utc: str

    @field_validator("source_refs")
    @classmethod
    def unique_source_refs(
        cls, value: tuple[ProjectionSourceRefV1, ...]
    ) -> tuple[ProjectionSourceRefV1, ...]:
        return _unique(value, "source reference")

    @field_validator("retrieved_at_utc")
    @classmethod
    def valid_retrieved_at(cls, value: str) -> str:
        return _require_datetime(value)


class ProjectionCalculationV1(_StrictFrozenModel):
    calculation_id: str = Field(min_length=1, max_length=128)
    calculation_version: int = Field(ge=1)
    input_projection_ids: tuple[ProjectionId, ...] = Field(max_length=MAX_PROJECTIONS)

    @field_validator("input_projection_ids")
    @classmethod
    def unique_inputs(
        cls, value: tuple[ProjectionId, ...]
    ) -> tuple[ProjectionId, ...]:
        return _unique(value, "calculation input")


class ProjectionTruncationV1(_StrictFrozenModel):
    truncated: Literal[True] = True
    reason: Literal[
        "projection_row_limit",
        "projection_byte_limit",
        "context_byte_limit",
    ]
    returned_rows: int | None = Field(default=None, ge=0)
    available_rows: int | None = Field(default=None, ge=0)
    serialized_bytes: int | None = Field(default=None, ge=0)


class ProjectionEnvelopeV1(_StrictFrozenModel):
    projection_id: ProjectionId
    projection_schema_version: Literal[1] = 1
    status: Literal[
        "available",
        "partial",
        "unavailable",
        "not_captured",
        "decision_blocked",
    ]
    executed: Literal[True] = True
    sensitivity: Sensitivity
    data: dict[str, Any] | list[Any] | None
    field_states: tuple[ProjectionFieldStateV1, ...] = Field(max_length=128)
    provenance: ProjectionProvenanceV1
    serialized_bytes: int = Field(ge=0, le=MAX_CONTEXT_BYTES)
    fallback_used: Literal[False] = False
    window: ProjectionAbsoluteWindowV1 | None = None
    calculation: ProjectionCalculationV1 | None = None
    decision_blocks: tuple[DecisionBlockId, ...] = ()
    truncation: ProjectionTruncationV1 | None = None

    @field_validator("decision_blocks")
    @classmethod
    def unique_blocks(
        cls, value: tuple[DecisionBlockId, ...]
    ) -> tuple[DecisionBlockId, ...]:
        return _unique(value, "decision block")

    @model_validator(mode="after")
    def exact_projection(self) -> "ProjectionEnvelopeV1":
        _validate_projection_data(self.projection_id, self.data)
        if self.status in {"available", "partial"} and self.data is None:
            raise ValueError("available projection requires typed data")
        if self.status in {"unavailable", "not_captured", "decision_blocked"}:
            if self.data is not None:
                raise ValueError("unavailable projection cannot carry data")
        if self.status == "decision_blocked" and not self.decision_blocks:
            raise ValueError("decision-blocked projection requires a block identifier")
        if self.status != "decision_blocked" and self.decision_blocks:
            raise ValueError("only decision-blocked projections may carry blocks")
        return self


class ProjectionErrorV1(_StrictFrozenModel):
    projection_id: ProjectionId
    projection_schema_version: Literal[1] = 1
    status: Literal["error"] = "error"
    executed: Literal[True] = True
    error_code: Literal[
        "incompatible_projection_version",
        "malformed_projection_payload",
    ]
    data: None = None
    fail_closed: Literal[True] = True
    fallback_used: Literal[False] = False


ProjectionResultValueV1: TypeAlias = ProjectionEnvelopeV1 | ProjectionErrorV1


class ProjectionExecutionResultV1(RootModel[ProjectionResultValueV1]):
    model_config = ConfigDict(
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
    )


class CoachingContextBindingV2(_StrictFrozenModel):
    request_id: str = Field(min_length=1, max_length=128)
    thread_id: str = Field(min_length=1, max_length=128)
    context_snapshot_id: str = Field(min_length=1, max_length=128)
    actor_user_id: str = Field(min_length=36, max_length=36)
    subject_user_id: str = Field(min_length=36, max_length=36)
    account_timezone: str = Field(min_length=1, max_length=64)
    as_of_utc: str
    as_of_local_date: str
    authorization_epoch: str = Field(min_length=1, max_length=128)
    transaction_isolation: Literal["repeatable_read"] = "repeatable_read"
    transaction_access: Literal["read_only"] = "read_only"

    @field_validator("actor_user_id", "subject_user_id")
    @classmethod
    def valid_uuid(cls, value: str) -> str:
        return _require_uuid(value)

    @field_validator("as_of_utc")
    @classmethod
    def valid_datetime(cls, value: str) -> str:
        return _require_datetime(value)

    @field_validator("as_of_local_date")
    @classmethod
    def valid_local_date(cls, value: str) -> str:
        return _require_date(value)


class UnavailableDomainV2(_StrictFrozenModel):
    domain: Literal["plan", "nutrition", "training", "conditioning", "measurements"]
    status: Literal["unavailable"] = "unavailable"


class ContextTruncationV2(_StrictFrozenModel):
    reason: Literal["context_byte_limit"] = "context_byte_limit"
    limit_bytes: Literal[MAX_CONTEXT_BYTES] = MAX_CONTEXT_BYTES
    serialized_bytes: int = Field(ge=0, le=MAX_CONTEXT_BYTES)


class CoachingCapabilitiesV2(_StrictFrozenModel):
    read: Literal[True] = True
    change_design: Literal[False] = False
    experiment_consent: Literal[False] = False
    write_consent: Literal[False] = False
    proactive_follow_up: Literal[False] = False


class StructuredCoachingContextV2(_StrictFrozenModel):
    schema_id: Literal["lifeswitch.structured_coaching_context"] = (
        "lifeswitch.structured_coaching_context"
    )
    schema_version: Literal[2] = 2
    binding: CoachingContextBindingV2
    authorization: AuthorizationSnapshotV2
    selection: ProjectionSelectionResultV1
    projections: tuple[ProjectionResultValueV1, ...] = Field(max_length=MAX_PROJECTIONS)
    unavailable_domains: tuple[UnavailableDomainV2, ...] = Field(max_length=5)
    serialized_projection_bytes: int = Field(ge=0, le=MAX_CONTEXT_BYTES)
    max_projection_bytes: Literal[MAX_CONTEXT_BYTES] = MAX_CONTEXT_BYTES
    capabilities: CoachingCapabilitiesV2
    context_truncation: ContextTruncationV2 | None = None

    @field_validator("unavailable_domains")
    @classmethod
    def unique_unavailable_domains(
        cls, value: tuple[UnavailableDomainV2, ...]
    ) -> tuple[UnavailableDomainV2, ...]:
        return _unique(value, "unavailable domain")

    @model_validator(mode="after")
    def exact_context(self) -> "StructuredCoachingContextV2":
        auth_binding = self.authorization.binding
        pairs = (
            (self.binding.request_id, auth_binding.request_id, "request"),
            (self.binding.thread_id, auth_binding.thread_id, "thread"),
            (
                self.binding.context_snapshot_id,
                auth_binding.context_snapshot_id,
                "context snapshot",
            ),
            (self.binding.actor_user_id, auth_binding.actor_user_id, "actor"),
            (self.binding.subject_user_id, auth_binding.subject_user_id, "subject"),
            (
                self.binding.account_timezone,
                auth_binding.account_timezone,
                "account timezone",
            ),
            (
                self.binding.authorization_epoch,
                self.authorization.authorization_epoch,
                "authorization epoch",
            ),
        )
        for context_value, authorization_value, label in pairs:
            if context_value != authorization_value:
                raise ValueError(f"{label} binding mismatch")

        if self.authorization.rechecks.pre_retrieval != "pass":
            raise ValueError("context requires a passing pre-retrieval recheck")
        if self.authorization.rechecks.pre_prompt != "pass":
            raise ValueError("context requires a passing pre-prompt recheck")

        projection_ids = [item.projection_id for item in self.projections]
        if len(projection_ids) != len(set(projection_ids)):
            raise ValueError("duplicate executed projection")
        selected_ids = [
            item.projection_id
            for item in self.selection.decisions
            if item.status == "selected"
        ]
        if projection_ids != selected_ids:
            raise ValueError("executed projections differ from ordered selection")
        if len(self.projections) != self.selection.selected_count:
            raise ValueError("executed projection count differs from selection")

        authorization_map = {
            item.projection_id: item for item in self.authorization.projection_decisions
        }
        for projection in self.projections:
            decision = authorization_map.get(projection.projection_id)
            if decision is None or not decision.executed:
                raise ValueError("executed projection lacks authorization")
            if isinstance(projection, ProjectionEnvelopeV1):
                levels = {"S0": 0, "S1": 1, "S2": 2, "S3": 3, "S4": 4}
                if levels[projection.sensitivity] > levels[decision.sensitivity_ceiling]:
                    raise ValueError("projection exceeds its sensitivity ceiling")
                if (
                    auth_binding.perspective == "delegated"
                    and levels[projection.sensitivity] > levels["S1"]
                ):
                    raise ValueError("delegated context cannot exceed S1")

        serialized = sum(
            item.serialized_bytes
            for item in self.projections
            if isinstance(item, ProjectionEnvelopeV1)
        )
        if serialized != self.serialized_projection_bytes:
            raise ValueError("serialized projection byte count mismatch")
        if self.authorization.budget.serialized_projection_bytes != serialized:
            raise ValueError("authorization byte budget differs from context")
        if self.authorization.budget.selected_projection_count != len(self.projections):
            raise ValueError("authorization count budget differs from context")
        if self.context_truncation is not None:
            if self.context_truncation.serialized_bytes != serialized:
                raise ValueError("context truncation byte count mismatch")
            if "context_byte_limit" not in self.authorization.budget.truncation_reasons:
                raise ValueError("context truncation is absent from authorization budget")
        return self


__all__ = [
    "AuthorizationBindingV2",
    "AuthorizationBudgetV2",
    "AuthorizationRechecksV2",
    "AuthorizationSnapshotV2",
    "CoachingCapabilitiesV2",
    "CoachingContextBindingV2",
    "ContextTruncationV2",
    "EffectiveGrantV2",
    "ProjectionAbsoluteWindowV1",
    "ProjectionAuthorizationDecisionV2",
    "ProjectionCalculationV1",
    "ProjectionCountTruncationV1",
    "ProjectionEnvelopeV1",
    "ProjectionErrorV1",
    "ProjectionExecutionResultV1",
    "ProjectionFieldStateV1",
    "ProjectionProvenanceV1",
    "ProjectionSelectionResultV1",
    "ProjectionSourceRefV1",
    "ProjectionTruncationV1",
    "RejectedProjectionDecisionV1",
    "SelectedProjectionDecisionV1",
    "StructuredCoachingContextV2",
    "UnavailableDomainV2",
    "canonical_json_bytes",
    "canonical_sha256",
    "file_sha256",
]
