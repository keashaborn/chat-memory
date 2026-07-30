from __future__ import annotations

"""Metadata-only operational summaries for trusted-web retrieval."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any


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


async def load_trusted_web_monitoring_summary_v1(
    conn,
    *,
    hours: int = 24,
) -> TrustedWebMonitoringSummaryV1:
    bounded_hours = _bounded_hours(hours)
    rows = await conn.fetch(
        """
        SELECT
            bucket_start,
            topic,
            policy_version,
            request_count,
            completed_count,
            failed_count,
            blocked_count,
            fail_closed_count,
            relevance_fail_closed_count,
            no_result_fail_closed_count,
            dependency_failure_count
        FROM trusted_web.retrieval_monitor_hourly_v1
        WHERE bucket_start >= (
            date_trunc('hour', now())
            - make_interval(hours => GREATEST($1 - 1, 0))
        )
        ORDER BY bucket_start DESC, topic, policy_version
        """,
        bounded_hours,
    )
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
    "load_trusted_web_monitoring_summary_v1",
]
