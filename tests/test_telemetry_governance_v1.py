from __future__ import annotations

import os
import unittest
import uuid
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("POSTGRES_DSN", "postgresql://test-only")

from rag_engine import telemetry_router as telemetry


ACTOR = "1240822d-ac9a-4096-95aa-e2b24d36ef50"


def slo_event(
    *,
    overall_status: str,
    evaluated_turns: int,
    completed: int,
    failed: int,
    failed_checks: list[str],
) -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": "voice.slo.observation",
        "subject_type": "voice_slo",
        "subject_id": "voice_slo_7d",
        "occurred_at": "2026-07-31T18:00:00Z",
        "payload": {
            "contract_version": "voice_slo_monitor_trace_v1",
            "synthetic": True,
            "canary_contract_version": "voice_synthetic_canary_v1_4",
            "window_days": 7,
            "overall_status": overall_status,
            "sample": {
                "evaluated_turns": evaluated_turns,
                "completed": completed,
                "failed": failed,
            },
            "failed_checks": failed_checks,
        },
    }


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: Any) -> None:
        return None


class FakeConnection:
    def __init__(self, *, insert_event: bool = True) -> None:
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchval_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.insert_event = insert_event
        self.closed = False

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, sql: str, *args: Any) -> str:
        self.execute_calls.append((sql, args))
        return "SELECT 1"

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.fetchval_calls.append((sql, args))
        if "INSERT INTO telemetry_event" in sql:
            return args[0] if self.insert_event else None
        if "ai_operations.record_monitor_observation_v1" in sql:
            return {
                "contract_version": "ai_operations_monitor_record_v1",
                "action": "opened",
            }
        raise AssertionError("unexpected fetchval SQL")

    async def close(self) -> None:
        self.closed = True


class TelemetryGovernanceV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(telemetry.router)
        self.client = TestClient(app)

    def test_write_requires_authenticated_actor(self) -> None:
        response = self.client.post(
            "/telemetry/event",
            json={
                "events": [
                    {
                        "event_id": str(uuid.uuid4()),
                        "event_type": "voice.turn.trace",
                        "subject_type": "voice_turn",
                        "subject_id": str(uuid.uuid4()),
                    }
                ]
            },
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json()["errors"][0]["reason"],
            "missing_actor_user_id",
        )
        self.assertIn("no-store", response.headers["cache-control"])

    def test_write_sets_rls_actor_and_stamps_canonical_owner(self) -> None:
        conn = FakeConnection()

        async def connect() -> FakeConnection:
            return conn

        with patch.object(telemetry, "_connect", connect):
            response = self.client.post(
                "/telemetry/event",
                headers={"x-vs-actor-user-id": ACTOR.upper()},
                json={
                    "events": [
                        {
                            "event_id": str(uuid.uuid4()),
                            "event_type": "voice.turn.trace",
                            "subject_type": "voice_turn",
                            "subject_id": str(uuid.uuid4()),
                            "payload": {},
                        }
                    ]
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["accepted"], 1)
        self.assertEqual(response.json()["monitor_observations"], 0)
        self.assertTrue(conn.closed)
        set_config = conn.execute_calls[0]
        insert = conn.fetchval_calls[0]
        self.assertIn("set_config('app.user_id'", set_config[0])
        self.assertEqual(set_config[1], (ACTOR,))
        self.assertEqual(insert[1][12], ACTOR)
        self.assertIn("no-store", response.headers["cache-control"])

    def test_failed_synthetic_voice_canary_records_private_incident(self) -> None:
        conn = FakeConnection()

        async def connect() -> FakeConnection:
            return conn

        with patch.object(telemetry, "_connect", connect):
            response = self.client.post(
                "/telemetry/event",
                headers={"x-vs-actor-user-id": ACTOR},
                json={
                    "events": [
                        {
                            "event_id": str(uuid.uuid4()),
                            "event_type": "voice.turn.trace",
                            "subject_type": "voice_turn",
                            "subject_id": str(uuid.uuid4()),
                            "occurred_at": "2026-07-31T18:00:00Z",
                            "payload": {
                                "contract_version": "voice_turn_trace_v1",
                                "synthetic": True,
                                "canary_contract_version": (
                                    "voice_synthetic_canary_v1_4"
                                ),
                                "status": "failed",
                                "failure_stage": "tts",
                                "failure_code": "upstream_http_422",
                            },
                        }
                    ]
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["monitor_observations"], 1)
        self.assertEqual(len(conn.fetchval_calls), 2)
        monitor_call = conn.fetchval_calls[1]
        self.assertIn(
            "ai_operations.record_monitor_observation_v1",
            monitor_call[0],
        )
        self.assertEqual(monitor_call[1][0], "voice_synthetic_canary")
        self.assertEqual(monitor_call[1][1], "unavailable")
        self.assertEqual(monitor_call[1][2], "critical")
        self.assertEqual(monitor_call[1][4], ["upstream_http_422"])
        self.assertEqual(monitor_call[1][6], 1)
        self.assertEqual(monitor_call[1][7], 0)

    def test_completed_synthetic_voice_canary_records_pass(self) -> None:
        conn = FakeConnection()

        async def connect() -> FakeConnection:
            return conn

        with patch.object(telemetry, "_connect", connect):
            response = self.client.post(
                "/telemetry/event",
                headers={"x-vs-actor-user-id": ACTOR},
                json={
                    "events": [
                        {
                            "event_id": str(uuid.uuid4()),
                            "event_type": "voice.turn.trace",
                            "subject_type": "voice_turn",
                            "subject_id": str(uuid.uuid4()),
                            "payload": {
                                "contract_version": "voice_turn_trace_v1",
                                "synthetic": True,
                                "canary_contract_version": (
                                    "voice_synthetic_canary_v1_4"
                                ),
                                "status": "completed",
                                "failure_stage": "none",
                                "failure_code": None,
                            },
                        }
                    ]
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["monitor_observations"], 1)
        monitor_args = conn.fetchval_calls[1][1]
        self.assertEqual(monitor_args[1], "pass")
        self.assertEqual(monitor_args[2], "info")
        self.assertEqual(monitor_args[4], [])
        self.assertEqual(monitor_args[7], 1)

    def test_duplicate_synthetic_event_does_not_repeat_observation(self) -> None:
        conn = FakeConnection(insert_event=False)

        async def connect() -> FakeConnection:
            return conn

        with patch.object(telemetry, "_connect", connect):
            response = self.client.post(
                "/telemetry/event",
                headers={"x-vs-actor-user-id": ACTOR},
                json={
                    "events": [
                        {
                            "event_id": str(uuid.uuid4()),
                            "event_type": "voice.turn.trace",
                            "subject_type": "voice_turn",
                            "subject_id": str(uuid.uuid4()),
                            "payload": {
                                "contract_version": "voice_turn_trace_v1",
                                "synthetic": True,
                                "canary_contract_version": (
                                    "voice_synthetic_canary_v1_4"
                                ),
                                "status": "failed",
                                "failure_stage": "tts",
                                "failure_code": "upstream_http_422",
                            },
                        }
                    ]
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["accepted"], 1)
        self.assertEqual(response.json()["monitor_observations"], 0)
        self.assertEqual(len(conn.fetchval_calls), 1)

    def test_failed_voice_slo_records_separate_rolling_incident(self) -> None:
        conn = FakeConnection()

        async def connect() -> FakeConnection:
            return conn

        with patch.object(telemetry, "_connect", connect):
            response = self.client.post(
                "/telemetry/event",
                headers={"x-vs-actor-user-id": ACTOR},
                json={
                    "events": [
                        slo_event(
                            overall_status="fail",
                            evaluated_turns=30,
                            completed=29,
                            failed=1,
                            failed_checks=["turn_success_rate"],
                        )
                    ]
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["monitor_observations"], 1)
        monitor_args = conn.fetchval_calls[1][1]
        self.assertEqual(monitor_args[0], "voice_slo")
        self.assertEqual(monitor_args[1], "violated")
        self.assertEqual(monitor_args[2], "critical")
        self.assertEqual(monitor_args[4], ["voice_slo_turn_success_rate"])
        self.assertEqual(monitor_args[5], 168)
        self.assertEqual(monitor_args[6], 30)
        self.assertEqual(monitor_args[7], 29)
        self.assertEqual(monitor_args[10], 0)

    def test_passing_voice_slo_records_separate_rolling_pass(self) -> None:
        conn = FakeConnection()

        async def connect() -> FakeConnection:
            return conn

        with patch.object(telemetry, "_connect", connect):
            response = self.client.post(
                "/telemetry/event",
                headers={"x-vs-actor-user-id": ACTOR},
                json={
                    "events": [
                        slo_event(
                            overall_status="pass",
                            evaluated_turns=40,
                            completed=40,
                            failed=0,
                            failed_checks=[],
                        )
                    ]
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["monitor_observations"], 1)
        monitor_args = conn.fetchval_calls[1][1]
        self.assertEqual(monitor_args[0], "voice_slo")
        self.assertEqual(monitor_args[1], "pass")
        self.assertEqual(monitor_args[2], "info")
        self.assertEqual(monitor_args[4], [])
        self.assertEqual(monitor_args[5], 168)
        self.assertEqual(monitor_args[6], 40)
        self.assertEqual(monitor_args[7], 40)

    def test_insufficient_voice_slo_data_does_not_mutate_incidents(self) -> None:
        conn = FakeConnection()

        async def connect() -> FakeConnection:
            return conn

        with patch.object(telemetry, "_connect", connect):
            response = self.client.post(
                "/telemetry/event",
                headers={"x-vs-actor-user-id": ACTOR},
                json={
                    "events": [
                        slo_event(
                            overall_status="insufficient_data",
                            evaluated_turns=1,
                            completed=1,
                            failed=0,
                            failed_checks=[],
                        )
                    ]
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["accepted"], 1)
        self.assertEqual(response.json()["monitor_observations"], 0)
        self.assertEqual(len(conn.fetchval_calls), 1)

    def test_unavailable_voice_slo_records_dependency_failure(self) -> None:
        conn = FakeConnection()

        async def connect() -> FakeConnection:
            return conn

        with patch.object(telemetry, "_connect", connect):
            response = self.client.post(
                "/telemetry/event",
                headers={"x-vs-actor-user-id": ACTOR},
                json={
                    "events": [
                        slo_event(
                            overall_status="unavailable",
                            evaluated_turns=0,
                            completed=0,
                            failed=0,
                            failed_checks=[],
                        )
                    ]
                },
            )

        self.assertEqual(response.status_code, 200)
        monitor_args = conn.fetchval_calls[1][1]
        self.assertEqual(monitor_args[0], "voice_slo")
        self.assertEqual(monitor_args[1], "unavailable")
        self.assertEqual(monitor_args[2], "critical")
        self.assertEqual(monitor_args[4], ["voice_slo_query_failed"])
        self.assertEqual(monitor_args[5], 168)
        self.assertEqual(monitor_args[10], 1)

    def test_voice_slo_sample_mismatch_fails_closed(self) -> None:
        conn = FakeConnection()

        async def connect() -> FakeConnection:
            return conn

        with patch.object(telemetry, "_connect", connect):
            with self.assertRaisesRegex(ValueError, "invalid_voice_slo_sample"):
                self.client.post(
                    "/telemetry/event",
                    headers={"x-vs-actor-user-id": ACTOR},
                    json={
                        "events": [
                            slo_event(
                                overall_status="fail",
                                evaluated_turns=30,
                                completed=30,
                                failed=1,
                                failed_checks=["turn_success_rate"],
                            )
                        ]
                    },
                )

        self.assertTrue(conn.closed)
        self.assertEqual(len(conn.fetchval_calls), 1)

    def test_stale_synthetic_contract_fails_closed(self) -> None:
        conn = FakeConnection()

        async def connect() -> FakeConnection:
            return conn

        with patch.object(telemetry, "_connect", connect):
            with self.assertRaisesRegex(
                ValueError,
                "invalid_voice_canary_contract",
            ):
                self.client.post(
                    "/telemetry/event",
                    headers={"x-vs-actor-user-id": ACTOR},
                    json={
                        "events": [
                            {
                                "event_id": str(uuid.uuid4()),
                                "event_type": "voice.turn.trace",
                                "subject_type": "voice_turn",
                                "subject_id": str(uuid.uuid4()),
                                "payload": {
                                    "contract_version": (
                                        "voice_turn_trace_v1"
                                    ),
                                    "synthetic": True,
                                    "canary_contract_version": (
                                        "voice_synthetic_canary_v1_3"
                                    ),
                                    "status": "failed",
                                    "failure_stage": "tts",
                                    "failure_code": "upstream_http_422",
                                },
                            }
                        ]
                    },
                )

        self.assertTrue(conn.closed)
        self.assertEqual(len(conn.fetchval_calls), 1)


if __name__ == "__main__":
    unittest.main()
