from __future__ import annotations

"""Typed effect contract for the SeeBx telemetry capability."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, Sequence
from uuid import UUID


@dataclass(frozen=True)
class TelemetryEventRecordV1:
    event_id: UUID
    event_type: str
    subject_type: str
    subject_id: str
    target_model_id: Any
    target_model_version: Any
    judge_model_id: Any
    judge_model_version: Any
    condition_id: Any
    thread_id: Any
    turn_id: Any
    actor_user_id: str
    payload: dict[str, Any]
    occurred_at: datetime

    def postgres_arguments(self) -> tuple[Any, ...]:
        return (
            self.event_id,
            self.event_type,
            self.subject_type,
            self.subject_id,
            self.target_model_id,
            self.target_model_version,
            self.judge_model_id,
            self.judge_model_version,
            self.condition_id,
            self.thread_id,
            self.turn_id,
            self.actor_user_id,
            self.payload,
            self.occurred_at,
        )


@dataclass(frozen=True)
class TelemetryTimeseriesSnapshotV1:
    point_rows: tuple[Any, ...]
    condition_rows: tuple[Any, ...]


class TelemetryRepository(Protocol):
    async def write_events(
        self,
        *,
        actor_user_id: str,
        events: Sequence[TelemetryEventRecordV1],
    ) -> None: ...

    async def read_timeseries(
        self,
        *,
        actor_user_id: str,
        metric_key: str,
        subject_type: str,
        subject_id: str,
        start: datetime,
        end: datetime,
        bucket: str,
        target_model_id: str | None,
    ) -> TelemetryTimeseriesSnapshotV1: ...

    async def read_voice_slo(
        self,
        *,
        actor_user_id: str,
        window_days: int,
    ) -> Any: ...


__all__ = [
    "TelemetryEventRecordV1",
    "TelemetryRepository",
    "TelemetryTimeseriesSnapshotV1",
]
