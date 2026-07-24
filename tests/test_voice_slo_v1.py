from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("POSTGRES_DSN", "postgresql://test-only")

from rag_engine import telemetry_router as telemetry


ACTOR = "1240822d-ac9a-4096-95aa-e2b24d36ef50"


def slo_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "latest_sample_at": datetime(
            2026,
            7,
            23,
            11,
            30,
            tzinfo=timezone.utc,
        ),
        "latest_status": "completed",
        "consecutive_successes": 1,
        "latest_failure_at": None,
        "latest_failure_stage": None,
        "latest_failure_code": None,
        "total": 1,
        "completed": 1,
        "failed": 0,
        "cancelled": 0,
        "transcription_failures": 0,
        "response_failures": 0,
        "tts_failures": 0,
        "transcription_ms_p50": 1531.0,
        "transcription_ms_p95": 1531.0,
        "transcription_ms_p99": 1531.0,
        "response_ms_p50": 5845.0,
        "response_ms_p95": 5845.0,
        "response_ms_p99": 5845.0,
        "tts_first_audio_ms_p50": 1348.0,
        "tts_first_audio_ms_p95": 1348.0,
        "tts_first_audio_ms_p99": 1348.0,
        "end_of_speech_to_first_audio_ms_p50": 9486.0,
        "end_of_speech_to_first_audio_ms_p95": 9486.0,
        "end_of_speech_to_first_audio_ms_p99": 9486.0,
    }
    row.update(overrides)
    return row


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: Any) -> None:
        return None


class FakeConnection:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.closed = False

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, sql: str, *args: Any) -> str:
        self.execute_calls.append((sql, args))
        return "SELECT 1"

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any]:
        self.fetchrow_calls.append((sql, args))
        return self.row

    async def close(self) -> None:
        self.closed = True


class VoiceSloV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(telemetry.router)
        self.client = TestClient(app)

    def test_endpoint_is_owner_scoped_and_insufficient_below_30(self) -> None:
        conn = FakeConnection(slo_row())

        async def connect() -> FakeConnection:
            return conn

        with patch.object(telemetry, "_connect", connect):
            response = self.client.get(
                "/metrics/voice-slo?window_days=7",
                headers={"x-vs-actor-user-id": ACTOR},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["cache-control"])
        payload = response.json()
        self.assertEqual(payload["contract_version"], "voice_slo_v1")
        self.assertEqual(payload["window_days"], 7)
        self.assertEqual(payload["overall_status"], "insufficient_data")
        self.assertEqual(
            payload["latest_sample_at"],
            "2026-07-23T11:30:00Z",
        )
        self.assertEqual(payload["sample"]["completed"], 1)
        self.assertEqual(payload["current"]["status"], "pass")
        self.assertEqual(payload["current"]["consecutive_successes"], 1)
        self.assertEqual(
            payload["latency_ms"]["end_of_speech_to_first_audio"]["p95"],
            9486.0,
        )
        self.assertTrue(conn.closed)
        self.assertIn("set_config('app.user_id'", conn.execute_calls[0][0])
        self.assertEqual(conn.execute_calls[0][1], (ACTOR,))
        self.assertIn("actor_user_id=$1", conn.fetchrow_calls[0][0])
        self.assertEqual(conn.fetchrow_calls[0][1], (ACTOR, 7))
        query = conn.fetchrow_calls[0][0]
        self.assertIn("detected_speech_end_v1", query)
        self.assertIn("synthetic_turn_start_v1", query)
        self.assertIn("coalesce(", query)
        self.assertIn("max(occurred_at)", query)
        self.assertIn("consecutive_successes", query)
        self.assertIn("failure_code", query)

    def test_contract_passes_only_after_minimum_samples(self) -> None:
        payload = telemetry._voice_slo_payload(
            slo_row(total=30, completed=30),
            30,
        )

        self.assertEqual(payload["overall_status"], "pass")
        self.assertTrue(
            all(
                check["status"] == "pass"
                for check in payload["checks"].values()
            )
        )

    def test_contract_fails_when_pause_exceeds_target(self) -> None:
        payload = telemetry._voice_slo_payload(
            slo_row(
                total=30,
                completed=30,
                end_of_speech_to_first_audio_ms_p95=16000.0,
            ),
            30,
        )

        self.assertEqual(payload["overall_status"], "fail")
        self.assertEqual(
            payload["checks"][
                "end_of_speech_to_first_audio_ms_p95"
            ]["status"],
            "fail",
        )

    def test_current_health_is_separate_from_historical_slo(self) -> None:
        latest_failure_at = datetime(
            2026,
            7,
            24,
            21,
            2,
            tzinfo=timezone.utc,
        )
        payload = telemetry._voice_slo_payload(
            slo_row(
                total=64,
                completed=57,
                failed=7,
                latest_status="completed",
                consecutive_successes=1,
                latest_failure_at=latest_failure_at,
                latest_failure_stage="tts",
                latest_failure_code="upstream_http_409",
            ),
            30,
        )

        self.assertEqual(payload["overall_status"], "fail")
        self.assertEqual(payload["current"]["status"], "pass")
        self.assertEqual(payload["current"]["consecutive_successes"], 1)
        self.assertEqual(
            payload["current"]["latest_failure_at"],
            "2026-07-24T21:02:00Z",
        )
        self.assertEqual(
            payload["current"]["latest_failure_stage"],
            "tts",
        )
        self.assertEqual(
            payload["current"]["latest_failure_code"],
            "upstream_http_409",
        )


if __name__ == "__main__":
    unittest.main()
