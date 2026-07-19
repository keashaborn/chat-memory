from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping


PLAN_DOCUMENT_SCHEMA_VERSION = 1
PLAN_VALIDATION_VERSION = 1
VALID_PHASES = frozenset({"cut", "maintenance", "lean_gain", "recomp", "other"})
PLAN_SECTION_FIELDS = (
    "body_state",
    "nutrition_targets",
    "training_targets",
    "conditioning_targets",
    "activity_targets",
    "recovery_targets",
    "monitoring_rules",
)
PLAN_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "phase",
        "phase_label",
        "primary_goal",
        "start_date",
        "review_date",
        "review_cadence",
        *PLAN_SECTION_FIELDS,
        "coach_notes",
    }
)


class RevisionState(StrEnum):
    DRAFT = "draft"
    PROPOSED = "proposed"
    NEEDS_CHANGES = "needs_changes"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    CONFLICTED = "conflicted"
    ACTIVATED = "activated"


class RevisionTrigger(StrEnum):
    OWNER_REQUEST = "owner_request"
    COACH_REQUEST = "coach_request"
    ANALYSIS_RECOMMENDATION = "analysis_recommendation"
    INITIAL_PLAN = "initial_plan"


class PlanDomainError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PlanValidationIssue:
    code: str
    field_path: str
    severity: str
    message: str


@dataclass(frozen=True, slots=True)
class PlanChange:
    field_path: str
    old_present: bool
    old_value: Any
    new_present: bool
    new_value: Any


@dataclass(frozen=True, slots=True)
class ActivationDecision:
    owner_user_id: uuid.UUID
    base_plan_version_id: uuid.UUID | None
    prior_active_plan_version_id: uuid.UUID | None
    next_version_number: int


def _clean_text(value: Any, *, field: str, max_length: int) -> str:
    text = str(value or "").strip()
    if len(text) > max_length:
        raise PlanDomainError("field_too_long", f"{field} exceeds {max_length} characters")
    return text


def _parse_optional_date(value: Any, *, field: str) -> dt.date | None:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, dt.datetime):
        raise PlanDomainError("invalid_date", f"{field} must be a date, not a datetime")
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise PlanDomainError("invalid_date", f"{field} must use YYYY-MM-DD") from exc


def _normalize_json(value: Any, *, field: str) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PlanDomainError("invalid_json_number", f"{field} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise PlanDomainError("invalid_json_key", f"{field} contains a non-string key")
            normalized[key] = _normalize_json(item, field=f"{field}.{key}")
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item, field=f"{field}[]") for item in value]
    raise PlanDomainError("invalid_json_value", f"{field} contains unsupported JSON value {type(value).__name__}")


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _normalize_section(value: Any, *, field: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise PlanDomainError("invalid_section", f"{field} must be a JSON object")
    normalized = _normalize_json(value, field=field)
    assert isinstance(normalized, dict)
    return _freeze_json(normalized)


@dataclass(frozen=True, slots=True)
class PlanDocumentV1:
    phase: str
    phase_label: str
    primary_goal: str
    start_date: dt.date | None
    review_date: dt.date | None
    review_cadence: str
    body_state: Mapping[str, Any]
    nutrition_targets: Mapping[str, Any]
    training_targets: Mapping[str, Any]
    conditioning_targets: Mapping[str, Any]
    activity_targets: Mapping[str, Any]
    recovery_targets: Mapping[str, Any]
    monitoring_rules: Mapping[str, Any]
    coach_notes: str
    schema_version: int = PLAN_DOCUMENT_SCHEMA_VERSION

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PlanDocumentV1":
        unknown_fields = sorted(set(value) - PLAN_TOP_LEVEL_FIELDS)
        if unknown_fields:
            raise PlanDomainError(
                "unknown_plan_field",
                f"unknown plan fields: {', '.join(unknown_fields)}",
            )
        phase = _clean_text(value.get("phase", "maintenance"), field="phase", max_length=40)
        if phase not in VALID_PHASES:
            raise PlanDomainError("invalid_phase", "phase must be cut|maintenance|lean_gain|recomp|other")

        schema_version = value.get("schema_version", PLAN_DOCUMENT_SCHEMA_VERSION)
        if (
            not isinstance(schema_version, int)
            or isinstance(schema_version, bool)
            or schema_version != PLAN_DOCUMENT_SCHEMA_VERSION
        ):
            raise PlanDomainError("unsupported_schema_version", "only plan document schema version 1 is supported")

        sections = {
            field: _normalize_section(value.get(field, {}), field=field)
            for field in PLAN_SECTION_FIELDS
        }
        return cls(
            phase=phase,
            phase_label=_clean_text(value.get("phase_label"), field="phase_label", max_length=160),
            primary_goal=_clean_text(value.get("primary_goal"), field="primary_goal", max_length=2000),
            start_date=_parse_optional_date(value.get("start_date"), field="start_date"),
            review_date=_parse_optional_date(value.get("review_date"), field="review_date"),
            review_cadence=_clean_text(
                value.get("review_cadence", "weekly"), field="review_cadence", max_length=40
            )
            or "weekly",
            coach_notes=_clean_text(value.get("coach_notes"), field="coach_notes", max_length=20000),
            schema_version=schema_version,
            **sections,
        )

    @classmethod
    def from_legacy_profile(cls, value: Mapping[str, Any]) -> "PlanDocumentV1":
        compatible = {field: value[field] for field in PLAN_TOP_LEVEL_FIELDS if field in value}
        compatible["schema_version"] = PLAN_DOCUMENT_SCHEMA_VERSION
        return cls.from_mapping(compatible)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "phase": self.phase,
            "phase_label": self.phase_label,
            "primary_goal": self.primary_goal,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "review_date": self.review_date.isoformat() if self.review_date else None,
            "review_cadence": self.review_cadence,
            "coach_notes": self.coach_notes,
        }
        for field in PLAN_SECTION_FIELDS:
            result[field] = _thaw_json(getattr(self, field))
        return result

    def canonical_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def validate_plan_document(document: PlanDocumentV1) -> tuple[PlanValidationIssue, ...]:
    issues: list[PlanValidationIssue] = []
    if not document.primary_goal:
        issues.append(
            PlanValidationIssue(
                code="primary_goal_missing",
                field_path="primary_goal",
                severity="error",
                message="Define the outcome this plan is intended to support.",
            )
        )
    if document.start_date and document.review_date and document.review_date < document.start_date:
        issues.append(
            PlanValidationIssue(
                code="review_before_start",
                field_path="review_date",
                severity="error",
                message="Review date cannot be before the plan start date.",
            )
        )
    if document.phase == "cut" and not document.body_state:
        issues.append(
            PlanValidationIssue(
                code="cut_body_state_missing",
                field_path="body_state",
                severity="warning",
                message="Add a starting outcome measure so change can be evaluated.",
            )
        )
    if not document.monitoring_rules:
        issues.append(
            PlanValidationIssue(
                code="monitoring_rules_missing",
                field_path="monitoring_rules",
                severity="warning",
                message="Define when the plan should be reviewed.",
            )
        )
    return tuple(issues)


def plan_validation_result(document: PlanDocumentV1) -> dict[str, Any]:
    issues = validate_plan_document(document)
    if any(issue.severity == "error" for issue in issues):
        status = "invalid"
    elif issues:
        status = "valid_with_warnings"
    else:
        status = "valid"
    return {
        "status": status,
        "validation_version": PLAN_VALIDATION_VERSION,
        "issues": [
            {
                "code": issue.code,
                "field_path": issue.field_path,
                "severity": issue.severity,
                "message": issue.message,
            }
            for issue in issues
        ],
    }


_ALLOWED_TRANSITIONS: dict[RevisionState, frozenset[RevisionState]] = {
    RevisionState.DRAFT: frozenset({RevisionState.DRAFT, RevisionState.PROPOSED, RevisionState.WITHDRAWN}),
    RevisionState.PROPOSED: frozenset(
        {
            RevisionState.NEEDS_CHANGES,
            RevisionState.REJECTED,
            RevisionState.WITHDRAWN,
            RevisionState.CONFLICTED,
            RevisionState.ACTIVATED,
        }
    ),
    RevisionState.NEEDS_CHANGES: frozenset(),
    RevisionState.REJECTED: frozenset(),
    RevisionState.WITHDRAWN: frozenset(),
    RevisionState.CONFLICTED: frozenset(),
    RevisionState.ACTIVATED: frozenset(),
}


def assert_revision_transition(old: RevisionState, new: RevisionState) -> None:
    if new not in _ALLOWED_TRANSITIONS[old]:
        raise PlanDomainError("invalid_revision_transition", f"cannot transition revision from {old} to {new}")


def validate_revision_base(
    *,
    trigger: RevisionTrigger,
    base_plan_version_id: uuid.UUID | None,
    current_active_plan_version_id: uuid.UUID | None,
) -> None:
    if trigger is RevisionTrigger.INITIAL_PLAN:
        if base_plan_version_id is not None or current_active_plan_version_id is not None:
            raise PlanDomainError("state_conflict", "initial plan requires no base and no active plan")
        return
    if base_plan_version_id is None:
        raise PlanDomainError("base_plan_required", "a non-initial revision requires a base plan version")
    if current_active_plan_version_id != base_plan_version_id:
        raise PlanDomainError("state_conflict", "the active plan changed since this revision was drafted")


def validate_activation(
    *,
    proposal_state: RevisionState,
    trigger: RevisionTrigger,
    base_plan_version_id: uuid.UUID | None,
    current_active_plan_version_id: uuid.UUID | None,
    owner_user_id: uuid.UUID,
    approving_actor_user_id: uuid.UUID,
    validation_status: str,
    next_version_number: int,
) -> ActivationDecision:
    if proposal_state is not RevisionState.PROPOSED:
        raise PlanDomainError("revision_not_proposed", "only a proposed revision can activate")
    if approving_actor_user_id != owner_user_id:
        raise PlanDomainError("owner_approval_required", "only the plan owner can approve activation")
    if validation_status not in {"valid", "valid_with_warnings"}:
        raise PlanDomainError("revision_not_valid", "revision must pass deterministic validation")
    if next_version_number < 1:
        raise PlanDomainError("invalid_version_number", "next version number must be positive")
    validate_revision_base(
        trigger=trigger,
        base_plan_version_id=base_plan_version_id,
        current_active_plan_version_id=current_active_plan_version_id,
    )
    return ActivationDecision(
        owner_user_id=owner_user_id,
        base_plan_version_id=base_plan_version_id,
        prior_active_plan_version_id=current_active_plan_version_id,
        next_version_number=next_version_number,
    )


_MISSING = object()


def _json_pointer_segment(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _diff_json(old: Any, new: Any, path: str, changes: list[PlanChange]) -> None:
    if old is not _MISSING and new is not _MISSING and old == new:
        return
    if isinstance(old, Mapping) and isinstance(new, Mapping):
        for key in sorted(set(old) | set(new)):
            child_path = f"{path}/{_json_pointer_segment(key)}"
            _diff_json(old.get(key, _MISSING), new.get(key, _MISSING), child_path, changes)
        return
    changes.append(
        PlanChange(
            field_path=path or "/",
            old_present=old is not _MISSING,
            old_value=None if old is _MISSING else copy.deepcopy(old),
            new_present=new is not _MISSING,
            new_value=None if new is _MISSING else copy.deepcopy(new),
        )
    )


def diff_plan_documents(
    old: PlanDocumentV1 | None,
    new: PlanDocumentV1,
) -> tuple[PlanChange, ...]:
    old_value: Mapping[str, Any] = {} if old is None else old.to_dict()
    changes: list[PlanChange] = []
    _diff_json(old_value, new.to_dict(), "", changes)
    return tuple(changes)
