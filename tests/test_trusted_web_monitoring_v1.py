from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import unittest

from rag_engine.trusted_web_monitoring_v1 import (
    TrustedWebMonitoringSummaryV1,
    evaluate_trusted_web_monitoring_thresholds_v1,
    load_trusted_web_monitoring_summary_v1,
)


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT / "ops/sql/20260730_trusted_web_retrieval_monitor_v1.sql"
)
ROLLBACK = (
    ROOT
    / "ops/sql/20260730_trusted_web_retrieval_monitor_v1_rollback.sql"
)
BOOTSTRAP = ROOT / "sql/trusted_web_gateway_v1.sql"


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.fetch_calls = []

    async def fetch(self, sql, *args):
        self.fetch_calls.append((sql, args))
        return self.rows


class TrustedWebMonitoringV1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_report_is_metadata_only_and_aggregates_failures(
        self,
    ) -> None:
        conn = FakeConnection(
            [
                {
                    "bucket_start": datetime(
                        2026, 7, 30, 10, tzinfo=timezone.utc
                    ),
                    "topic": "training_evidence",
                    "policy_version": "trusted_web_policy_v1",
                    "request_count": 9,
                    "completed_count": 6,
                    "failed_count": 2,
                    "blocked_count": 1,
                    "fail_closed_count": 3,
                    "relevance_fail_closed_count": 1,
                    "no_result_fail_closed_count": 1,
                    "dependency_failure_count": 0,
                },
                {
                    "bucket_start": datetime(
                        2026, 7, 30, 9, tzinfo=timezone.utc
                    ),
                    "topic": "behavior_change_evidence",
                    "policy_version": "trusted_web_policy_v1",
                    "request_count": 5,
                    "completed_count": 4,
                    "failed_count": 1,
                    "blocked_count": 0,
                    "fail_closed_count": 1,
                    "relevance_fail_closed_count": 0,
                    "no_result_fail_closed_count": 0,
                    "dependency_failure_count": 1,
                },
            ]
        )
        summary = await load_trusted_web_monitoring_summary_v1(
            conn,
            hours=24,
        )

        self.assertEqual(summary.request_count, 14)
        self.assertEqual(summary.completed_count, 10)
        self.assertEqual(summary.failed_count, 3)
        self.assertEqual(summary.blocked_count, 1)
        self.assertEqual(summary.fail_closed_count, 4)
        self.assertEqual(summary.relevance_fail_closed_count, 1)
        self.assertEqual(summary.no_result_fail_closed_count, 1)
        self.assertEqual(summary.dependency_failure_count, 1)
        self.assertEqual(summary.fail_closed_rate, 0.285714)
        payload = summary.as_dict()
        serialized = str(payload)
        for forbidden in (
            "actor_user_id",
            "query_sha256",
            "request_id",
            "provider_response_id",
            "source_metadata",
        ):
            self.assertNotIn(forbidden, serialized)

        sql, args = conn.fetch_calls[0]
        self.assertIn(
            "trusted_web.retrieval_monitor_hourly_v1",
            sql,
        )
        self.assertIn("make_interval", sql)
        self.assertEqual(args, (24,))

    async def test_monitor_window_is_bounded(self) -> None:
        conn = FakeConnection([])
        for invalid in (0, -1, 745):
            with self.assertRaisesRegex(ValueError, "hours must be"):
                await load_trusted_web_monitoring_summary_v1(
                    conn,
                    hours=invalid,
                )

    def test_monitor_view_is_private_and_has_rollback(self) -> None:
        migration = MIGRATION.read_text(encoding="utf-8").lower()
        rollback = ROLLBACK.read_text(encoding="utf-8").lower()
        bootstrap = BOOTSTRAP.read_text(encoding="utf-8").lower()

        self.assertIn(
            "trusted_web.retrieval_monitor_hourly_v1",
            migration,
        )
        self.assertIn("security_invoker = true", migration)
        self.assertIn("relevance_fail_closed_count", migration)
        self.assertIn("fail_closed_count", migration)
        self.assertIn("from public", migration)
        self.assertIn("'anon'", migration)
        self.assertIn("'authenticated'", migration)
        self.assertIn("'service_role'", migration)
        self.assertIn("to brains_app", migration)
        for forbidden in (
            "actor_user_id",
            "query_sha256",
            "request_id",
            "provider_response_id",
            "source_metadata",
        ):
            self.assertNotIn(forbidden, migration)

        self.assertIn(
            "drop view if exists "
            "trusted_web.retrieval_monitor_hourly_v1",
            rollback,
        )
        self.assertIn(
            "drop index if exists "
            "trusted_web.retrieval_audit_created_at_idx",
            rollback,
        )
        self.assertIn(
            "trusted_web.retrieval_monitor_hourly_v1",
            bootstrap,
        )
        self.assertIn("security_invoker = true", bootstrap)
        self.assertIn("to brains_app", bootstrap)

    def test_monitor_thresholds_cover_relevance_dependency_and_rate(
        self,
    ) -> None:
        summary = TrustedWebMonitoringSummaryV1(
            contract_version="trusted_web_monitoring_v1",
            window_hours=24,
            request_count=10,
            completed_count=6,
            failed_count=3,
            blocked_count=1,
            fail_closed_count=4,
            relevance_fail_closed_count=1,
            no_result_fail_closed_count=1,
            dependency_failure_count=2,
            fail_closed_rate=0.4,
            buckets=(),
        )
        self.assertEqual(
            evaluate_trusted_web_monitoring_thresholds_v1(
                summary,
                max_relevance_fail_closed=0,
                max_dependency_failures=0,
                max_fail_closed_rate=0.25,
            ),
            (
                "relevance_fail_closed_count",
                "dependency_failure_count",
                "fail_closed_rate",
            ),
        )
        self.assertEqual(
            evaluate_trusted_web_monitoring_thresholds_v1(
                summary,
                max_relevance_fail_closed=1,
                max_dependency_failures=2,
                max_fail_closed_rate=0.4,
            ),
            (),
        )

    def test_monitor_thresholds_reject_unbounded_values(self) -> None:
        summary = TrustedWebMonitoringSummaryV1(
            contract_version="trusted_web_monitoring_v1",
            window_hours=24,
            request_count=0,
            completed_count=0,
            failed_count=0,
            blocked_count=0,
            fail_closed_count=0,
            relevance_fail_closed_count=0,
            no_result_fail_closed_count=0,
            dependency_failure_count=0,
            fail_closed_rate=0.0,
            buckets=(),
        )
        for kwargs in (
            {"max_relevance_fail_closed": -1},
            {"max_dependency_failures": -1},
            {"max_fail_closed_rate": 1.01},
        ):
            with self.assertRaises(ValueError):
                evaluate_trusted_web_monitoring_thresholds_v1(
                    summary,
                    **kwargs,
                )


if __name__ == "__main__":
    unittest.main()
