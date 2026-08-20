from __future__ import annotations

"""PostgreSQL reads for metadata-only trusted-web monitoring summaries."""

from typing import Any


_LOAD_BUCKETS_SQL = """
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
"""


class PostgresTrustedWebMonitoringRepository:
    """Load bounded metadata buckets through an injected connection."""

    def __init__(self, connection: Any) -> None:
        if connection is None or not callable(getattr(connection, "fetch", None)):
            raise ValueError("a PostgreSQL connection with fetch is required")
        self._connection = connection

    async def load_buckets(self, *, hours: int) -> tuple[Any, ...]:
        rows = await self._connection.fetch(_LOAD_BUCKETS_SQL, hours)
        return tuple(rows)


__all__ = ["PostgresTrustedWebMonitoringRepository"]
