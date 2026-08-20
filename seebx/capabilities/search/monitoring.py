from __future__ import annotations

"""Metadata-only operational summaries for trusted-web retrieval."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from seebx.adapters.trusted_web_monitoring_postgres import (
    PostgresTrustedWebMonitoringRepository,
)


MIN_MONITOR_HOURS = 1
MAX_MONITOR_HOURS = 24 * 31


def _bounded_hours(hours: int) -> int:
    value = int(hours)
    if value < MIN_MONITOR_HOURS or value > MAX_MONITOR_HOURS:
        raise ValueError(
            f"hours must be between {MIN_MONITOR_HOURS} and "
            f"{MAX_MONITOR_HOURS}"
        )
    return value


def _row_value(row: Any, key: str) -> Any:
    if hasattr(row, "get"):
        return row.get(key)
    return row[key]


@dataclass(frozen=True)
class TrustedWebMonitorBucketV1:
    bucket_start: datetime
    topic: str
    policy_version: str
    request_count: int
    completed_count: int
    failed_count: int
    blocked_count: int
    fail_closed_count: int
    relevance_fail_closed_count: int
    no_result_fail_closed_count: int
    dependency_failure_count: int

    @classmethod
    def from_row(cls, row: Any) -> "TrustedWebMonitorBucketV1":
        return cls(
            bucket_start=_row_value(row, "bucket_start"),
            topic=str(_row_value(row, "topic") or ""),
            policy_version=str(_row_value(row, "policy_version") or ""),
            request_count=int(_row_value(row, "request_count") or 0),
            completed_count=int(_row_value(row, "completed_count") or 0),
            failed_count=int(_row_value(row, "failed_count") or 0),
            blocked_count=int(_row_value(row, "blocked_count") or 0),
            fail_closed_count=int(
                _row_value(row, "fail_closed_count") or 0
            ),
            relevance_fail_closed_count=int(
                _row_value(row, "relevance_fail_closed_count") or 0
            ),
            no_result_fail_closed_count=int(
                _row_value(row, "no_result_fail_closed_count") or 0
            ),
            dependency_failure_count=int(
                _row_value(row, "dependency_failure_count") or 0
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "bucket_start": self.bucket_start.isoformat(),
            "topic": self.topic,
            "policy_version": self.policy_version,
            "request_count": self.request_count,
            "completed_count": self.completed_count,
            "failed_count": self.failed_count,
            "blocked_count": self.blocked_count,
            "fail_closed_count": self.fail_closed_count,
            "relevance_fail_closed_count": (
                self.relevance_fail_closed_count
            ),
            "no_result_fail_closed_count": (
                self.no_result_fail_closed_count
            ),
            "dependency_failure_count": self.dependency_failure_count,
        }


@dataclass(frozen=True)
class TrustedWebMonitoringSummaryV1:
    contract_version: str
    window_hours: int
    request_count: int
    completed_count: int
    failed_count: int
    blocked_count: int
    fail_closed_count: int
    relevance_fail_closed_count: int
    no_result_fail_closed_count: int
    dependency_failure_count: int
    fail_closed_rate: float
    buckets: tuple[TrustedWebMonitorBucketV1, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "window_hours": self.window_hours,
            "request_count": self.request_count,
            "completed_count": self.completed_count,
            "failed_count": self.failed_count,
            "blocked_count": self.blocked_count,
            "fail_closed_count": self.fail_closed_count,
            "relevance_fail_closed_count": (
                self.relevance_fail_closed_count
            ),
            "no_result_fail_closed_count": (
                self.no_result_fail_closed_count
            ),
            "dependency_failure_count": self.dependency_failure_count,
            "fail_closed_rate": self.fail_closed_rate,
            "buckets": [bucket.as_dict() for bucket in self.buckets],
        }


def evaluate_trusted_web_monitoring_thresholds_v1(
    summary: TrustedWebMonitoringSummaryV1,
    *,
    max_relevance_fail_closed: int | None = None,
    max_dependency_failures: int | None = None,
    max_fail_closed_rate: float | None = None,
) -> tuple[str, ...]:
    if (
        max_relevance_fail_closed is not None
        and max_relevance_fail_closed < 0
    ):
        raise ValueError("max_relevance_fail_closed must be >= 0")
    if (
        max_dependency_failures is not None
        and max_dependency_failures < 0
    ):
        raise ValueError("max_dependency_failures must be >= 0")
    if (
        max_fail_closed_rate is not None
        and not 0.0 <= max_fail_closed_rate <= 1.0
    ):
        raise ValueError("max_fail_closed_rate must be between 0 and 1")

    violations: list[str] = []
    if (
        max_relevance_fail_closed is not None
        and summary.relevance_fail_closed_count
        > max_relevance_fail_closed
    ):
        violations.append("relevance_fail_closed_count")
    if (
        max_dependency_failures is not None
        and summary.dependency_failure_count > max_dependency_failures
    ):
        violations.append("dependency_failure_count")
    if (
        max_fail_closed_rate is not None
        and summary.fail_closed_rate > max_fail_closed_rate
    ):
        violations.append("fail_closed_rate")
    return tuple(violations)


async def load_trusted_web_monitoring_summary_v1(
    conn,
    *,
    hours: int = 24,
) -> TrustedWebMonitoringSummaryV1:
    bounded_hours = _bounded_hours(hours)
    repository = PostgresTrustedWebMonitoringRepository(conn)
    rows = await repository.load_buckets(hours=bounded_hours)
    buckets = tuple(TrustedWebMonitorBucketV1.from_row(row) for row in rows)

    def total(field: str) -> int:
        return sum(int(getattr(bucket, field)) for bucket in buckets)

    completed_count = total("completed_count")
    fail_closed_count = total("fail_closed_count")
    executed_count = completed_count + fail_closed_count
    return TrustedWebMonitoringSummaryV1(
        contract_version="trusted_web_monitoring_v1",
        window_hours=bounded_hours,
        request_count=total("request_count"),
        completed_count=completed_count,
        failed_count=total("failed_count"),
        blocked_count=total("blocked_count"),
        fail_closed_count=fail_closed_count,
        relevance_fail_closed_count=total(
            "relevance_fail_closed_count"
        ),
        no_result_fail_closed_count=total(
            "no_result_fail_closed_count"
        ),
        dependency_failure_count=total("dependency_failure_count"),
        fail_closed_rate=(
            round(fail_closed_count / executed_count, 6)
            if executed_count
            else 0.0
        ),
        buckets=buckets,
    )


__all__ = [
    "MAX_MONITOR_HOURS",
    "MIN_MONITOR_HOURS",
    "TrustedWebMonitorBucketV1",
    "TrustedWebMonitoringSummaryV1",
    "evaluate_trusted_web_monitoring_thresholds_v1",
    "load_trusted_web_monitoring_summary_v1",
]
