from __future__ import annotations

"""Content-free, monotonic pilot-start marker contract."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping
from uuid import UUID

from ..contracts import (
    ContractViolation,
    framed_sha256,
    require_key,
    require_sha256,
    require_utc,
    require_uuid,
)


PILOT_MARKER_RECEIPT_DOMAIN = "governed_memory.pilot_ever_started.v1"
PILOT_MARKER_FIELDS = (
    "authorization_receipt_sha256",
    "marker_receipt_sha256",
    "operation_id",
    "pilot_contract_sha256",
    "pilot_ever_started",
    "pilot_id",
    "started_at",
)


def pilot_marker_receipt_sha256(
    *,
    pilot_id: str,
    operation_id: UUID,
    pilot_contract_sha256: str,
    authorization_receipt_sha256: str,
    started_at: datetime,
) -> str:
    checked_pilot_id = require_key(pilot_id, "invalid_pilot_id")
    operation = require_uuid(operation_id, "invalid_pilot_operation_id")
    contract_hash = require_sha256(
        pilot_contract_sha256,
        "invalid_pilot_contract_sha256",
    )
    authorization_hash = require_sha256(
        authorization_receipt_sha256,
        "invalid_pilot_authorization_receipt_sha256",
    )
    started = require_utc(started_at, "invalid_pilot_started_at")
    return framed_sha256(
        PILOT_MARKER_RECEIPT_DOMAIN,
        (
            ("pilot_id", checked_pilot_id),
            ("operation_id", str(operation)),
            ("pilot_contract_sha256", contract_hash),
            ("authorization_receipt_sha256", authorization_hash),
            (
                "started_at",
                started.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            ),
        ),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class PilotMarker:
    pilot_ever_started: bool
    pilot_id: str
    operation_id: UUID
    pilot_contract_sha256: str
    authorization_receipt_sha256: str
    started_at: datetime
    marker_receipt_sha256: str

    def __post_init__(self) -> None:
        if self.pilot_ever_started is not True:
            raise ContractViolation("pilot_marker_cannot_represent_false_row")
        expected = pilot_marker_receipt_sha256(
            pilot_id=self.pilot_id,
            operation_id=self.operation_id,
            pilot_contract_sha256=self.pilot_contract_sha256,
            authorization_receipt_sha256=self.authorization_receipt_sha256,
            started_at=self.started_at,
        )
        if require_sha256(
            self.marker_receipt_sha256,
            "invalid_pilot_marker_receipt_sha256",
        ) != expected:
            raise ContractViolation("pilot_marker_receipt_sha256_mismatch")

    @property
    def as_row(self) -> dict[str, object]:
        return {
            "authorization_receipt_sha256": self.authorization_receipt_sha256,
            "marker_receipt_sha256": self.marker_receipt_sha256,
            "operation_id": self.operation_id,
            "pilot_contract_sha256": self.pilot_contract_sha256,
            "pilot_ever_started": True,
            "pilot_id": self.pilot_id,
            "started_at": self.started_at,
        }


def pilot_marker_from_row(value: Mapping[str, Any]) -> PilotMarker:
    if not isinstance(value, Mapping) or tuple(sorted(value)) != PILOT_MARKER_FIELDS:
        raise ContractViolation("invalid_pilot_marker_row")
    operation = value["operation_id"]
    if not isinstance(operation, UUID):
        try:
            operation = UUID(str(operation))
        except (TypeError, ValueError) as exc:
            raise ContractViolation("invalid_pilot_operation_id") from exc
    started = value["started_at"]
    if isinstance(started, str):
        try:
            started = datetime.fromisoformat(started.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ContractViolation("invalid_pilot_started_at") from exc
    return PilotMarker(
        authorization_receipt_sha256=value["authorization_receipt_sha256"],  # type: ignore[arg-type]
        marker_receipt_sha256=value["marker_receipt_sha256"],  # type: ignore[arg-type]
        operation_id=operation,
        pilot_contract_sha256=value["pilot_contract_sha256"],  # type: ignore[arg-type]
        pilot_ever_started=value["pilot_ever_started"],  # type: ignore[arg-type]
        pilot_id=value["pilot_id"],  # type: ignore[arg-type]
        started_at=started,  # type: ignore[arg-type]
    )


def pilot_ever_started(value: Mapping[str, Any] | None) -> bool:
    """Absence means false; a valid row can only mean true."""

    return False if value is None else pilot_marker_from_row(value).pilot_ever_started


class PilotStartDisposition(str, Enum):
    INSERT = "insert"
    REPLAY = "replay"


@dataclass(frozen=True, slots=True, kw_only=True)
class PilotStartPlan:
    disposition: PilotStartDisposition
    marker: PilotMarker


def plan_pilot_start(
    existing: Mapping[str, Any] | None,
    *,
    pilot_id: str,
    operation_id: UUID,
    pilot_contract_sha256: str,
    authorization_receipt_sha256: str,
    started_at: datetime,
) -> PilotStartPlan:
    marker_hash = pilot_marker_receipt_sha256(
        pilot_id=pilot_id,
        operation_id=operation_id,
        pilot_contract_sha256=pilot_contract_sha256,
        authorization_receipt_sha256=authorization_receipt_sha256,
        started_at=started_at,
    )
    desired = PilotMarker(
        authorization_receipt_sha256=authorization_receipt_sha256,
        marker_receipt_sha256=marker_hash,
        operation_id=operation_id,
        pilot_contract_sha256=pilot_contract_sha256,
        pilot_ever_started=True,
        pilot_id=pilot_id,
        started_at=started_at,
    )
    if existing is None:
        return PilotStartPlan(
            disposition=PilotStartDisposition.INSERT,
            marker=desired,
        )
    current = pilot_marker_from_row(existing)
    if current.marker_receipt_sha256 != desired.marker_receipt_sha256:
        raise ContractViolation("pilot_marker_conflicting_replay")
    return PilotStartPlan(
        disposition=PilotStartDisposition.REPLAY,
        marker=current,
    )


__all__ = [
    "PILOT_MARKER_FIELDS",
    "PILOT_MARKER_RECEIPT_DOMAIN",
    "PilotMarker",
    "PilotStartDisposition",
    "PilotStartPlan",
    "pilot_ever_started",
    "pilot_marker_from_row",
    "pilot_marker_receipt_sha256",
    "plan_pilot_start",
]
