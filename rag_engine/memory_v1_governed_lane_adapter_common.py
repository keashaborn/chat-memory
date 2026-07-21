from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence
from uuid import UUID

from .memory_v1_selection_envelope import (
    LaneOutcomeCode,
    MemoryLane,
    MemoryLaneSelectionResultV1,
    ReasonCountV1,
    RejectionCode,
    SelectedMemoryRecordV1,
)


class GovernedLaneAdapterError(RuntimeError):
    """Fail-closed error at a governed source-to-envelope boundary."""


class GovernedLaneSchemaGap(GovernedLaneAdapterError):
    """A governed read contract omits fields required by the frozen envelope."""

    def __init__(self, source: str, missing_fields: Iterable[str]) -> None:
        missing = tuple(sorted(set(missing_fields)))
        if not source or not missing:
            raise ValueError("schema-gap errors require a source and missing fields")
        self.source = source
        self.missing_fields = missing
        super().__init__(f"{source} omits required fields: {', '.join(missing)}")


SENSITIVITY_RANK = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "restricted": 3,
}

FORBIDDEN_LEGACY_KEYS = frozenset({"memory_chunks", "prompt_block"})


def reject_legacy_prompt_payload(value: Mapping[str, Any], *, source: str) -> None:
    present = sorted(FORBIDDEN_LEGACY_KEYS.intersection(value))
    if present:
        raise GovernedLaneAdapterError(
            f"{source} returned forbidden legacy fields: {', '.join(present)}"
        )


def verify_governed_read_controls(
    value: Any,
    *,
    source: str,
    require_forced_rls: bool = False,
    require_restricted_read_contract: bool = False,
) -> dict[str, Any]:
    controls = parse_mapping(value, field=f"{source}.controls")
    if (
        controls.get("effective_role_brains_app") is not True
        or controls.get("transaction_read_only") is not True
    ):
        raise GovernedLaneAdapterError(
            f"{source} did not prove the restricted read-only role"
        )
    if require_forced_rls and controls.get("forced_rls") is not True:
        raise GovernedLaneAdapterError(f"{source} did not prove forced RLS")
    if (
        require_restricted_read_contract
        and controls.get("restricted_read_contract") is not True
    ):
        raise GovernedLaneAdapterError(
            f"{source} did not prove its restricted read contract"
        )
    return controls


def require_fields(
    value: Mapping[str, Any],
    fields: Sequence[str],
    *,
    source: str,
) -> None:
    reject_legacy_prompt_payload(value, source=source)
    missing = [field for field in fields if field not in value]
    if missing:
        raise GovernedLaneSchemaGap(source, missing)


def parse_uuid(value: Any, *, field: str) -> UUID:
    try:
        return UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise GovernedLaneAdapterError(f"{field} must be a UUID") from exc


def parse_optional_uuid(value: Any, *, field: str) -> UUID | None:
    if value in (None, ""):
        return None
    return parse_uuid(value, field=field)


def parse_uuid_tuple(value: Any, *, field: str) -> tuple[UUID, ...]:
    if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes)):
        raise GovernedLaneAdapterError(f"{field} must be an array of UUIDs")
    parsed = tuple(parse_uuid(item, field=field) for item in value)
    canonical = tuple(sorted(set(parsed), key=str))
    if len(parsed) != len(canonical):
        raise GovernedLaneAdapterError(f"{field} must not contain duplicate UUIDs")
    return canonical


def parse_utc(value: Any, *, field: str) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise GovernedLaneAdapterError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GovernedLaneAdapterError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def parse_score(value: Any, *, field: str) -> float:
    if isinstance(value, bool):
        raise GovernedLaneAdapterError(f"{field} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise GovernedLaneAdapterError(f"{field} must be numeric") from exc
    if not math.isfinite(parsed) or not 0.0 <= parsed <= 1.0:
        raise GovernedLaneAdapterError(f"{field} must be between zero and one")
    return parsed


def parse_text(value: Any, *, field: str, maximum: int = 4000) -> str:
    if not isinstance(value, str):
        raise GovernedLaneAdapterError(f"{field} must be text")
    if not value.strip() or "\x00" in value or len(value) > maximum:
        raise GovernedLaneAdapterError(
            f"{field} must contain 1 to {maximum} characters"
        )
    return value


def parse_mapping(value: Any, *, field: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    raise GovernedLaneAdapterError(f"{field} must be a JSON object")


def canonical_json(value: Any, *, field: str, maximum: int = 4000) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise GovernedLaneAdapterError(f"{field} is not canonicalizable JSON") from exc
    if not encoded or len(encoded.encode("utf-8")) > maximum:
        raise GovernedLaneAdapterError(f"{field} exceeds {maximum} bytes")
    return encoded


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_sha256(value: Any, *, field: str) -> str:
    parsed = str(value or "")
    if len(parsed) != 64 or any(character not in "0123456789abcdef" for character in parsed):
        raise GovernedLaneAdapterError(f"{field} must be a lowercase SHA-256")
    return parsed


def reason_tuple(counter: Mapping[RejectionCode, int]) -> tuple[ReasonCountV1, ...]:
    return tuple(
        ReasonCountV1(code=code, count=count)
        for code, count in sorted(counter.items(), key=lambda item: item[0].value)
        if count
    )


def primary_and_all_counts(
    reason_sets: Iterable[Sequence[RejectionCode]],
) -> tuple[tuple[ReasonCountV1, ...], tuple[ReasonCountV1, ...]]:
    primary: Counter[RejectionCode] = Counter()
    all_reasons: Counter[RejectionCode] = Counter()
    for reasons in reason_sets:
        if not reasons:
            continue
        primary[reasons[0]] += 1
        all_reasons.update(set(reasons))
    return reason_tuple(primary), reason_tuple(all_reasons)


def build_lane_result(
    *,
    owner_user_id: UUID,
    lane: MemoryLane,
    records: Sequence[SelectedMemoryRecordV1],
    candidate_count: int,
    visible_candidate_count: int,
    eligible_count: int,
    candidate_set_sha256: str,
    rejected_reason_sets: Iterable[Sequence[RejectionCode]],
    qdrant_role: str,
    controls: Sequence[Any] = (),
    control_candidate_count: int = 0,
    rejected_control_reason_sets: Iterable[Sequence[RejectionCode]] = (),
) -> MemoryLaneSelectionResultV1:
    primary, all_reasons = primary_and_all_counts(rejected_reason_sets)
    control_primary, control_all = primary_and_all_counts(
        rejected_control_reason_sets
    )
    if records:
        outcome = LaneOutcomeCode.SELECTED
    elif controls:
        outcome = LaneOutcomeCode.CONTROLS_ONLY
    elif visible_candidate_count == 0 and control_candidate_count == 0:
        outcome = LaneOutcomeCode.NO_VISIBLE_MEMORY
    else:
        outcome = LaneOutcomeCode.NO_SELECTION
    try:
        return MemoryLaneSelectionResultV1(
            owner_user_id=owner_user_id,
            lane=lane,
            records=tuple(records),
            controls=tuple(controls),
            candidate_count=candidate_count,
            visible_candidate_count=visible_candidate_count,
            eligible_count=eligible_count,
            control_candidate_count=control_candidate_count,
            candidate_set_sha256=validate_sha256(
                candidate_set_sha256, field="candidate_set_sha256"
            ),
            primary_rejection_counts=primary,
            reason_counts=all_reasons,
            control_primary_rejection_counts=control_primary,
            control_reason_counts=control_all,
            outcome_code=outcome,
            owner_scope_verified=True,
            postgres_revalidated=True,
            qdrant_role=qdrant_role,
            database_writes=0,
            qdrant_writes=0,
            external_model_calls=0,
        )
    except Exception as exc:
        raise GovernedLaneAdapterError(
            f"{lane.value} adapter result violates the frozen Memory V1 contract"
        ) from exc


__all__ = [
    "FORBIDDEN_LEGACY_KEYS",
    "GovernedLaneAdapterError",
    "GovernedLaneSchemaGap",
    "SENSITIVITY_RANK",
    "build_lane_result",
    "canonical_json",
    "canonical_sha256",
    "parse_mapping",
    "parse_optional_uuid",
    "parse_score",
    "parse_text",
    "parse_utc",
    "parse_uuid",
    "parse_uuid_tuple",
    "reject_legacy_prompt_payload",
    "require_fields",
    "validate_sha256",
    "verify_governed_read_controls",
]
