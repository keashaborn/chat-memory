from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from seebx.capabilities.observability.telemetry import create_telemetry_router
from seebx.capabilities.observability.telemetry_contracts import (
    TelemetryTimeseriesSnapshotV1,
)


ACTOR = "1240822d-ac9a-4096-95aa-e2b24d36ef50"


class FakeRepository:
    def __init__(self) -> None:
        self.write_calls: list[dict[str, Any]] = []
        self.timeseries_calls: list[dict[str, Any]] = []

    async def write_events(self, **kwargs: Any) -> None:
        self.write_calls.append(kwargs)

    async def read_timeseries(self, **kwargs: Any) -> TelemetryTimeseriesSnapshotV1:
        self.timeseries_calls.append(kwargs)
        now = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)
        return TelemetryTimeseriesSnapshotV1(
            point_rows=({"t": now, "v": 0.75, "n": 4},),
            condition_rows=(
                {"condition_id": "A", "occurred_at": now, "payload": {"label": "Baseline"}},
            ),
        )

    async def read_voice_slo(self, **kwargs: Any) -> Any:
        raise AssertionError("unexpected voice SLO call")


class TelemetryGovernanceV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = FakeRepository()
        app = FastAPI()
        app.include_router(create_telemetry_router(self.repository))
        self.client = TestClient(app)

    def test_write_requires_authenticated_actor_before_repository(self) -> None:
        response = self.client.post(
            "/telemetry/event",
            json={"events": [{"event_id": str(uuid.uuid4())}]},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["errors"][0]["reason"], "missing_actor_user_id")
        self.assertEqual(self.repository.write_calls, [])
        self.assertIn("no-store", response.headers["cache-control"])

    def test_write_normalizes_actor_and_passes_typed_records(self) -> None:
        event_id = uuid.uuid4()
        response = self.client.post(
            "/telemetry/event",
            headers={"x-vs-actor-user-id": ACTOR.upper()},
            json={
                "events": [
                    {
                        "event_id": str(event_id),
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
        call = self.repository.write_calls[0]
        self.assertEqual(call["actor_user_id"], ACTOR)
        self.assertEqual(len(call["events"]), 1)
        record = call["events"][0]
        self.assertEqual(record.event_id, event_id)
        self.assertEqual(record.actor_user_id, ACTOR)
        self.assertIn("no-store", response.headers["cache-control"])

    def test_timeseries_transforms_repository_snapshot(self) -> None:
        response = self.client.get(
            "/metrics/timeseries",
            headers={"x-vs-actor-user-id": ACTOR},
            params={
                "metric_key": "probe_overall",
                "subject_type": "thread",
                "subject_id": "synthetic",
                "from": "2026-08-20T00:00:00Z",
                "to": "2026-08-22T00:00:00Z",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["points"][0]["v"], 0.75)
        self.assertEqual(response.json()["phases"][0]["label"], "Baseline")
        self.assertEqual(self.repository.timeseries_calls[0]["actor_user_id"], ACTOR)


if __name__ == "__main__":
    unittest.main()
