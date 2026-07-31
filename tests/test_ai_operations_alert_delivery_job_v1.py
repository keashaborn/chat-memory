from __future__ import annotations

import json
from pathlib import Path
import unittest
from uuid import UUID

import httpx

from scripts.ai_operations_alert_delivery_job_v1 import (
    AlertDeliveryConfigV1,
    AlertDeliveryConfigurationError,
    CLAIM_CONTRACT_VERSION,
    ClaimedAlertV1,
    _send_resend_email,
    _validate_config,
    run_alert_delivery_drill_v1,
    run_alert_delivery_job_v1,
)


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "ops/sql/20260731_ai_operations_alert_delivery_v1.sql"
ROLLBACK = (
    ROOT / "ops/sql/20260731_ai_operations_alert_delivery_v1_rollback.sql"
)
SERVICE = (
    ROOT / "ops/systemd/ai-operations-alert-delivery-v1.service"
)
TIMER = ROOT / "ops/systemd/ai-operations-alert-delivery-v1.timer"
DRILL_SERVICE = (
    ROOT / "ops/systemd/ai-operations-alert-delivery-drill-v1.service"
)
ENV_EXAMPLE = (
    ROOT / "ops/systemd/ai-operations-alert-delivery-v1.env.example"
)


def config(**overrides) -> AlertDeliveryConfigV1:
    values = {
        "enabled": "authorized",
        "postgres_dsn": "postgresql://private",
        "resend_api_key": "re_test_private_value",
        "to_email": "owner@example.invalid",
    }
    values.update(overrides)
    return AlertDeliveryConfigV1(**values)


def claim_payload() -> dict[str, object]:
    return {
        "contract_version": CLAIM_CONTRACT_VERSION,
        "status": "claimed",
        "delivery_id": "11111111-1111-4111-8111-111111111111",
        "incident_id": "22222222-2222-4222-8222-222222222222",
        "monitor_name": "trusted_web_retrieval",
        "severity": "critical",
        "observation_status": "violated",
        "reason_codes": ["dependency_failure_count"],
        "first_seen_at": "2026-07-31T12:00:00Z",
        "last_seen_at": "2026-07-31T12:05:00Z",
        "observation_count": 1,
        "window_hours": 24,
        "request_count": 4,
        "completed_count": 2,
        "fail_closed_count": 2,
        "dependency_failure_count": 1,
        "fail_closed_rate": 0.5,
        "attempt_count": 1,
    }


def claimed_alert() -> ClaimedAlertV1:
    payload = claim_payload()
    return ClaimedAlertV1(
        delivery_id=UUID(str(payload["delivery_id"])),
        incident_id=UUID(str(payload["incident_id"])),
        monitor_name=str(payload["monitor_name"]),
        severity=str(payload["severity"]),
        observation_status=str(payload["observation_status"]),
        reason_codes=tuple(payload["reason_codes"]),
        first_seen_at=str(payload["first_seen_at"]),
        last_seen_at=str(payload["last_seen_at"]),
        observation_count=int(payload["observation_count"]),
        window_hours=int(payload["window_hours"]),
        request_count=int(payload["request_count"]),
        completed_count=int(payload["completed_count"]),
        fail_closed_count=int(payload["fail_closed_count"]),
        dependency_failure_count=int(payload["dependency_failure_count"]),
        fail_closed_rate=float(payload["fail_closed_rate"]),
        attempt_count=int(payload["attempt_count"]),
    )


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, _type, _value, _traceback):
        return False


class FakeConnection:
    def __init__(self, claims: list[dict[str, object]]) -> None:
        self.claims = list(claims)
        self.execute_calls: list[tuple[object, ...]] = []
        self.complete_calls: list[tuple[object, ...]] = []
        self.closed = False

    def transaction(self):
        return FakeTransaction()

    async def execute(self, sql, *args):
        self.execute_calls.append((sql, *args))

    async def fetchval(self, sql, *args):
        if "claim_monitor_alert_delivery_v1" in sql:
            if self.claims:
                return self.claims.pop(0)
            return {
                "contract_version": CLAIM_CONTRACT_VERSION,
                "status": "empty",
            }
        if "complete_monitor_alert_delivery_v1" in sql:
            self.complete_calls.append(args)
            outcome = str(args[2])
            mapped = {
                "delivered": "delivered",
                "retryable_failed": "retry_scheduled",
                "permanent_failed": "permanent_failed",
            }[outcome]
            return {
                "contract_version": "ai_operations_alert_delivery_complete_v1",
                "delivery_id": str(args[0]),
                "outcome": mapped,
            }
        raise AssertionError("unexpected SQL")

    async def close(self):
        self.closed = True


def connector(connection: FakeConnection):
    async def connect(_dsn, **_kwargs):
        return connection

    return connect


class AiOperationsAlertDeliveryV1Tests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_successful_job_is_metadata_only_and_idempotent(self) -> None:
        connection = FakeConnection([claim_payload()])
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"id": "email_123"})

        report, exit_code = await run_alert_delivery_job_v1(
            config(batch_size=2),
            connect=connector(connection),
            transport=httpx.MockTransport(handler),
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["claimed_count"], 1)
        self.assertEqual(report["delivered_count"], 1)
        self.assertTrue(connection.closed)
        self.assertEqual(len(connection.complete_calls), 1)
        self.assertEqual(connection.complete_calls[0][2], "delivered")
        self.assertEqual(len(captured), 1)
        request = captured[0]
        self.assertEqual(request.url, httpx.URL("https://api.resend.com/emails"))
        self.assertEqual(
            request.headers["idempotency-key"],
            "ai-operations-alert-11111111-1111-4111-8111-111111111111",
        )
        payload = json.loads(request.content)
        self.assertEqual(
            set(payload), {"from", "to", "subject", "text", "tags"}
        )
        self.assertEqual(payload["to"], ["owner@example.invalid"])
        self.assertEqual(payload["tags"][0]["value"], "ai_operations")
        serialized = json.dumps(payload, sort_keys=True)
        self.assertNotIn("re_test_private_value", serialized)
        self.assertNotIn("postgresql://private", serialized)
        self.assertEqual(payload["text"].count("https://"), 1)
        self.assertIn("https://lifeswitch.com/admin", payload["text"])

    async def test_503_is_persisted_as_retryable_failure(self) -> None:
        connection = FakeConnection([claim_payload()])
        report, exit_code = await run_alert_delivery_job_v1(
            config(batch_size=1),
            connect=connector(connection),
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(503)
            ),
        )
        self.assertEqual(exit_code, 3)
        self.assertEqual(report["status"], "delivery_incomplete")
        self.assertEqual(report["retry_scheduled_count"], 1)
        self.assertEqual(connection.complete_calls[0][2], "retryable_failed")
        self.assertEqual(
            connection.complete_calls[0][4], "provider_http_503"
        )

    async def test_400_is_persisted_as_permanent_failure(self) -> None:
        connection = FakeConnection([claim_payload()])
        report, exit_code = await run_alert_delivery_job_v1(
            config(batch_size=1),
            connect=connector(connection),
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(400)
            ),
        )
        self.assertEqual(exit_code, 3)
        self.assertEqual(report["permanent_failed_count"], 1)
        self.assertEqual(connection.complete_calls[0][2], "permanent_failed")

    async def test_provider_response_body_and_id_are_bounded(self) -> None:
        oversized = httpx.MockTransport(
            lambda _request: httpx.Response(
                200, content=b"x" * 16_385
            )
        )
        outcome = await _send_resend_email(
            _validate_config(config()),
            claimed_alert(),
            transport=oversized,
        )
        self.assertEqual(outcome.outcome, "retryable_failed")
        self.assertEqual(outcome.error_code, "provider_response_oversized")

        invalid_id = httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"id": "bad id"})
        )
        outcome = await _send_resend_email(
            _validate_config(config()),
            claimed_alert(),
            transport=invalid_id,
        )
        self.assertEqual(outcome.error_code, "provider_id_invalid")

    async def test_drill_requires_secondary_authorization(self) -> None:
        with self.assertRaises(AlertDeliveryConfigurationError) as ctx:
            await run_alert_delivery_drill_v1(config())
        self.assertEqual(ctx.exception.code, "drill_not_authorized")

        captured: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "drill_123"})

        report, exit_code = await run_alert_delivery_drill_v1(
            config(drill_enabled="authorized"),
            transport=httpx.MockTransport(handler),
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "drill_delivered")
        self.assertTrue(report["metadata_only"])
        self.assertEqual(len(captured), 1)
        self.assertIn("[TEST]", captured[0]["subject"])
        self.assertNotIn("postgresql://private", json.dumps(report))

    def test_configuration_is_explicit_and_bounded(self) -> None:
        for overrides, code in (
            ({"enabled": ""}, "delivery_not_authorized"),
            ({"postgres_dsn": ""}, "missing_postgres_dsn"),
            ({"resend_api_key": "bad"}, "invalid_resend_api_key"),
            ({"to_email": "bad"}, "invalid_to_email"),
            ({"from_email": "bad"}, "invalid_from_email"),
            ({"batch_size": 0}, "invalid_batch_size"),
            ({"batch_size": 26}, "invalid_batch_size"),
        ):
            with self.assertRaises(AlertDeliveryConfigurationError) as ctx:
                _validate_config(config(**overrides))
            self.assertEqual(ctx.exception.code, code)

    def test_sql_outbox_is_private_deduplicated_and_retry_bounded(self) -> None:
        sql = MIGRATION.read_text(encoding="utf-8")
        rollback = ROLLBACK.read_text(encoding="utf-8")
        self.assertIn("FORCE ROW LEVEL SECURITY", sql)
        self.assertIn("UNIQUE (incident_id, channel)", sql)
        self.assertIn("FOR UPDATE SKIP LOCKED", sql)
        self.assertIn("attempt_count BETWEEN 0 AND max_attempts", sql)
        self.assertIn("worker_lease_expired", sql)
        self.assertIn("OR NEW.severity <> 'critical'", sql)
        self.assertNotIn("NEW.severity NOT IN ('warning','critical')", sql)
        self.assertIn("interval '1 minute'", sql)
        self.assertIn("interval '12 hours'", sql)
        self.assertIn("REVOKE ALL ON ai_operations.monitor_alert_delivery_v1 FROM brains_app", sql)
        self.assertIn("Append-only metadata history", sql)
        self.assertNotIn("recipient_email", sql.lower())
        self.assertNotIn("destination_email", sql.lower())
        self.assertNotIn("message_body text", sql.lower())
        self.assertIn("DROP TABLE IF EXISTS ai_operations.monitor_alert_delivery_v1", rollback)

    def test_systemd_units_are_resource_bounded_and_secret_safe(self) -> None:
        service = SERVICE.read_text(encoding="utf-8")
        timer = TIMER.read_text(encoding="utf-8")
        drill = DRILL_SERVICE.read_text(encoding="utf-8")
        env_example = ENV_EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", service)
        self.assertIn("/usr/bin/flock -n", service)
        self.assertIn("NoNewPrivileges=true", service)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("MemoryMax=128M", service)
        self.assertIn("CPUQuota=10%", service)
        self.assertIn("OnCalendar=*-*-* *:*:00", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("--drill", drill)
        self.assertIn(
            "AI_OPERATIONS_ALERT_DRILL_ENABLED=authorized", drill
        )
        self.assertIn("AI_OPERATIONS_RESEND_API_KEY=", env_example)
        self.assertNotIn("POSTGRES_DSN=", env_example)
        self.assertNotIn("re_test", env_example)


if __name__ == "__main__":
    unittest.main()
