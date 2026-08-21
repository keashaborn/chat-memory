from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from seebx.adapters.postgres import PostgresConnectionProvider
from seebx.adapters.telemetry_postgres import PostgresTelemetryRepository
from seebx.capabilities.observability.telemetry_contracts import TelemetryEventRecordV1


ROOT = Path(__file__).resolve().parents[1]
ACTOR = "1240822d-ac9a-4096-95aa-e2b24d36ef50"


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: Any) -> None:
        return None


class FakeConnection:
    def __init__(self) -> None:
        self.codec_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.closed = False

    async def set_type_codec(self, *args: Any, **kwargs: Any) -> None:
        self.codec_calls.append((args, kwargs))

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, sql: str, *args: Any) -> str:
        self.execute_calls.append((sql, args))
        return "INSERT 0 1"

    async def close(self) -> None:
        self.closed = True


class TelemetryPostgresAdapterV1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_write_owns_codec_transaction_actor_sql_and_close(self) -> None:
        connection = FakeConnection()

        async def connect(*args: Any, **kwargs: Any) -> FakeConnection:
            return connection

        repository = PostgresTelemetryRepository(
            PostgresConnectionProvider("synthetic", connect_factory=connect)
        )
        record = TelemetryEventRecordV1(
            event_id=UUID("2240822d-ac9a-4096-95aa-e2b24d36ef50"),
            event_type="voice.turn.trace",
            subject_type="voice_turn",
            subject_id="synthetic",
            target_model_id=None,
            target_model_version=None,
            judge_model_id=None,
            judge_model_version=None,
            condition_id=None,
            thread_id=None,
            turn_id=None,
            actor_user_id=ACTOR,
            payload={},
            occurred_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
        )
        await repository.write_events(actor_user_id=ACTOR, events=(record,))
        self.assertEqual(
            [call[0][0] for call in connection.codec_calls],
            ["json", "jsonb"],
        )
        self.assertIn("set_config('app.user_id'", connection.execute_calls[0][0])
        self.assertEqual(connection.execute_calls[0][1], (ACTOR,))
        self.assertIn("INSERT INTO telemetry_event", connection.execute_calls[1][0])
        self.assertEqual(connection.execute_calls[1][1][11], ACTOR)
        self.assertTrue(connection.closed)

    async def test_unknown_metric_fails_before_connection(self) -> None:
        calls = 0

        async def connect(*args: Any, **kwargs: Any) -> FakeConnection:
            nonlocal calls
            calls += 1
            return FakeConnection()

        repository = PostgresTelemetryRepository(
            PostgresConnectionProvider("synthetic", connect_factory=connect)
        )
        with self.assertRaises(KeyError):
            await repository.read_timeseries(
                actor_user_id=ACTOR,
                metric_key="not_allowed",
                subject_type="thread",
                subject_id="synthetic",
                start=datetime(2026, 8, 20, tzinfo=timezone.utc),
                end=datetime(2026, 8, 21, tzinfo=timezone.utc),
                bucket="day",
                target_model_id=None,
            )
        self.assertEqual(calls, 0)

    def test_capability_has_no_database_effects(self) -> None:
        capability = (
            ROOT / "seebx/capabilities/observability/telemetry.py"
        ).read_text()
        adapter = (ROOT / "seebx/adapters/telemetry_postgres.py").read_text()
        for forbidden in ("import asyncpg", "asyncpg.connect", "conn.close"):
            self.assertNotIn(forbidden, capability)
        self.assertNotIn("INSERT INTO telemetry_event", capability)
        self.assertNotIn("WITH voice AS", capability)
        self.assertIn("INSERT INTO telemetry_event", adapter)
        self.assertIn("WITH voice AS", adapter)
        for required in (
            "actor_user_id=$1",
            "detected_speech_end_v1",
            "synthetic_turn_start_v1",
            "consecutive_successes",
            "failure_code",
            "condition.set",
        ):
            self.assertIn(required, adapter)


if __name__ == "__main__":
    unittest.main()
