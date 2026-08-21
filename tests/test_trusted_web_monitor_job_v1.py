from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import unittest

import httpx

from scripts.trusted_web_monitor_job_v1 import (
    ALERT_CONTRACT_VERSION,
    INBOX_CONTRACT_VERSION,
    JOB_CONTRACT_VERSION,
    MonitorJobConfigurationError,
    TrustedWebMonitorJobConfigV1,
    _validate_config,
    run_trusted_web_alert_drill_v1,
    run_trusted_web_monitor_job_v1,
)


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "ops/systemd/trusted-web-monitor-v1.service.in"
DRILL_SERVICE = ROOT / "ops/systemd/trusted-web-monitor-drill-v1.service.in"
TIMER = ROOT / "ops/systemd/trusted-web-monitor-v1.timer"
ENV_EXAMPLE = (
    ROOT / "ops/systemd/trusted-web-monitor-v1.env.example"
)


def monitor_row(
    *,
    completed: int = 1,
    failed: int = 0,
    blocked: int = 0,
    relevance: int = 0,
    no_result: int = 0,
    dependency: int = 0,
) -> dict[str, object]:
    return {
        "bucket_start": datetime(
            2026,
            7,
            31,
            3,
            tzinfo=timezone.utc,
        ),
        "topic": "training_evidence",
        "policy_version": "trusted_web_policy_v1_4",
        "request_count": completed + failed + blocked,
        "completed_count": completed,
        "failed_count": failed,
        "blocked_count": blocked,
        "fail_closed_count": failed + blocked,
        "relevance_fail_closed_count": relevance,
        "no_result_fail_closed_count": no_result,
        "dependency_failure_count": dependency,
    }


class FakeConnection:
    def __init__(self, rows, *, inbox_error: bool = False) -> None:
        self.rows = rows
        self.inbox_error = inbox_error
        self.inbox_calls: list[tuple[object, ...]] = []
        self.closed = False

    async def fetch(self, _sql, *_args):
        return self.rows

    async def fetchval(self, _sql, *args):
        if self.inbox_error:
            raise RuntimeError("private inbox failure detail")
        self.inbox_calls.append(args)
        return {
            "contract_version": INBOX_CONTRACT_VERSION,
            "action": "opened",
        }

    async def close(self) -> None:
        self.closed = True


def connect_for(rows, *, inbox_error: bool = False):
    connection = FakeConnection(rows, inbox_error=inbox_error)

    async def connect(_dsn, **_kwargs):
        return connection

    return connect, connection


class TrustedWebMonitorJobV1Tests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_pass_report_is_aggregate_only(self) -> None:
        connect, connection = connect_for([monitor_row(completed=4)])
        report, exit_code = await run_trusted_web_monitor_job_v1(
            TrustedWebMonitorJobConfigV1(
                enabled="authorized",
                postgres_dsn="postgresql://private",
            ),
            connect=connect,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(
            report["contract_version"],
            "trusted_web_monitor_job_v1",
        )
        self.assertEqual(
            report["monitor_contract_version"],
            "trusted_web_monitoring_v1",
        )
        self.assertEqual(report["request_count"], 4)
        self.assertEqual(report["alert_store"], "recorded")
        self.assertEqual(report["alert_delivery"], "not_needed")
        self.assertTrue(connection.closed)
        self.assertEqual(len(connection.inbox_calls), 1)
        serialized = json.dumps(report, sort_keys=True)
        for forbidden in (
            "buckets",
            "actor_user_id",
            "query_sha256",
            "request_id",
            "provider_response_id",
            "source_metadata",
            "source_url",
            "answer",
        ):
            self.assertNotIn(forbidden, serialized)

    async def test_violation_delivers_metadata_only_alert(self) -> None:
        connect, _connection = connect_for(
            [
                monitor_row(
                    completed=2,
                    failed=2,
                    relevance=1,
                    dependency=1,
                )
            ]
        )
        captured: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(204)

        report, exit_code = await run_trusted_web_monitor_job_v1(
            TrustedWebMonitorJobConfigV1(
                enabled="authorized",
                postgres_dsn="postgresql://private",
                alert_webhook_url="https://alerts.example.invalid/hook",
            ),
            connect=connect,
            alert_transport=httpx.MockTransport(handler),
        )

        self.assertEqual(exit_code, 2)
        self.assertEqual(report["status"], "violated")
        self.assertEqual(report["alert_store"], "recorded")
        self.assertEqual(report["alert_delivery"], "delivered")
        self.assertEqual(
            report["threshold_violations"],
            [
                "relevance_fail_closed_count",
                "dependency_failure_count",
                "fail_closed_rate",
            ],
        )
        self.assertEqual(len(captured), 1)
        alert = captured[0]
        self.assertEqual(alert["contract_version"], JOB_CONTRACT_VERSION)
        self.assertNotIn("buckets", alert)
        self.assertNotIn("topic", alert)
        self.assertNotIn("policy_version", alert)

    async def test_safe_drill_records_without_search_access(self) -> None:
        connect, connection = connect_for([])
        captured: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(204)

        report, exit_code = await run_trusted_web_alert_drill_v1(
            TrustedWebMonitorJobConfigV1(
                enabled="authorized",
                postgres_dsn="postgresql://unused",
                alert_webhook_url="https://alerts.example.invalid/hook",
                drill_enabled="authorized",
            ),
            connect=connect,
            alert_transport=httpx.MockTransport(handler),
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "drill")
        self.assertTrue(report["drill"])
        self.assertEqual(report["request_count"], 0)
        self.assertEqual(report["alert_store"], "recorded")
        self.assertEqual(report["alert_delivery"], "delivered")
        self.assertTrue(connection.closed)
        self.assertEqual(len(connection.inbox_calls), 1)
        self.assertEqual(
            connection.inbox_calls[0][1],
            "drill",
        )
        self.assertEqual(len(captured), 1)
        alert = captured[0]
        self.assertEqual(
            alert["alert_contract_version"],
            ALERT_CONTRACT_VERSION,
        )
        self.assertEqual(
            alert["event_type"],
            "trusted_web_monitor_delivery_drill",
        )
        self.assertEqual(alert["severity"], "test")
        self.assertTrue(alert["drill"])
        serialized = json.dumps(alert, sort_keys=True)
        for forbidden in (
            "actor_user_id",
            "query_sha256",
            "request_id",
            "provider_response_id",
            "source_metadata",
            "source_url",
            "answer",
        ):
            self.assertNotIn(forbidden, serialized)

    async def test_drill_requires_authorization_not_webhook(self) -> None:
        with self.assertRaises(MonitorJobConfigurationError) as ctx:
            await run_trusted_web_alert_drill_v1(
                TrustedWebMonitorJobConfigV1(
                    enabled="authorized",
                    postgres_dsn="postgresql://unused",
                    alert_webhook_url=(
                        "https://alerts.example.invalid/hook"
                    ),
                )
            )
        self.assertEqual(ctx.exception.code, "drill_not_authorized")

        connect, _connection = connect_for([])
        report, exit_code = await run_trusted_web_alert_drill_v1(
            TrustedWebMonitorJobConfigV1(
                enabled="authorized",
                postgres_dsn="postgresql://unused",
                drill_enabled="authorized",
            ),
            connect=connect,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["alert_store"], "recorded")
        self.assertEqual(report["alert_delivery"], "unconfigured")

    async def test_failed_drill_delivery_fails_closed(self) -> None:
        connect, _connection = connect_for([])
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(503)
        )
        report, exit_code = await run_trusted_web_alert_drill_v1(
            TrustedWebMonitorJobConfigV1(
                enabled="authorized",
                postgres_dsn="postgresql://unused",
                alert_webhook_url="https://alerts.example.invalid/hook",
                drill_enabled="authorized",
            ),
            connect=connect,
            alert_transport=transport,
        )
        self.assertEqual(exit_code, 3)
        self.assertEqual(report["alert_store"], "recorded")
        self.assertEqual(report["alert_delivery"], "http_503")

    async def test_inbox_failure_fails_closed_without_leaking(self) -> None:
        connect, connection = connect_for(
            [monitor_row(completed=4)],
            inbox_error=True,
        )
        report, exit_code = await run_trusted_web_monitor_job_v1(
            TrustedWebMonitorJobConfigV1(
                enabled="authorized",
                postgres_dsn="postgresql://private",
            ),
            connect=connect,
        )
        self.assertEqual(exit_code, 3)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["alert_store"], "failed")
        self.assertTrue(connection.closed)
        self.assertNotIn("private inbox failure detail", json.dumps(report))

    async def test_query_failure_is_safe_and_fails_job(self) -> None:
        async def fail_connect(_dsn, **_kwargs):
            raise RuntimeError("secret database detail")

        report, exit_code = await run_trusted_web_monitor_job_v1(
            TrustedWebMonitorJobConfigV1(
                enabled="authorized",
                postgres_dsn="postgresql://private",
            ),
            connect=fail_connect,
        )
        self.assertEqual(exit_code, 3)
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["error_code"], "monitor_query_failed")
        self.assertEqual(report["alert_store"], "failed")
        self.assertEqual(report["alert_delivery"], "unconfigured")
        self.assertNotIn("secret database detail", json.dumps(report))

    def test_configuration_is_explicit_and_bounded(self) -> None:
        base = {
            "enabled": "authorized",
            "postgres_dsn": "postgresql://private",
        }
        for overrides, code in (
            ({"enabled": ""}, "monitor_not_authorized"),
            ({"postgres_dsn": ""}, "missing_postgres_dsn"),
            ({"window_hours": 0}, "invalid_window_hours"),
            (
                {"max_relevance_fail_closed": -1},
                "invalid_max_relevance_fail_closed",
            ),
            (
                {"max_dependency_failures": -1},
                "invalid_max_dependency_failures",
            ),
            (
                {"max_fail_closed_rate": 2.0},
                "invalid_max_fail_closed_rate",
            ),
            (
                {"alert_webhook_url": "http://alerts.invalid"},
                "invalid_alert_webhook_url",
            ),
        ):
            values = {**base, **overrides}
            with self.assertRaises(MonitorJobConfigurationError) as ctx:
                _validate_config(TrustedWebMonitorJobConfigV1(**values))
            self.assertEqual(ctx.exception.code, code)

    def test_systemd_units_are_hourly_and_hardened(self) -> None:
        service = SERVICE.read_text(encoding="utf-8")
        drill_service = DRILL_SERVICE.read_text(encoding="utf-8")
        timer = TIMER.read_text(encoding="utf-8")
        env_example = ENV_EXAMPLE.read_text(encoding="utf-8")

        self.assertIn("Type=oneshot", service)
        self.assertIn("User=ubuntu", service)
        self.assertIn("EnvironmentFile=/opt/chat-memory/.env", service)
        self.assertIn(
            "EnvironmentFile=-/etc/verbalsage/"
            "trusted-web-monitor.env",
            service,
        )
        self.assertIn("NoNewPrivileges=true", service)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("PrivateDevices=true", service)
        self.assertIn("CapabilityBoundingSet=", service)
        self.assertIn("MemoryMax=128M", service)
        self.assertIn("OnCalendar=hourly", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("Type=oneshot", drill_service)
        self.assertIn("--drill", drill_service)
        self.assertIn(
            "Environment=TRUSTED_WEB_MONITOR_DRILL_ENABLED=authorized",
            drill_service,
        )
        self.assertIn("NoNewPrivileges=true", drill_service)
        self.assertIn("ProtectSystem=strict", drill_service)
        self.assertIn("RandomizedDelaySec=5m", timer)
        self.assertIn(
            "TRUSTED_WEB_MONITOR_ENABLED=authorized",
            env_example,
        )
        self.assertIn("internal inbox", env_example.lower())
        self.assertNotIn("POSTGRES_DSN=", env_example)
        self.assertNotIn("http://", env_example)


if __name__ == "__main__":
    unittest.main()
