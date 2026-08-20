from __future__ import annotations

import os
import unittest
import uuid
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("POSTGRES_DSN", "postgresql://test-only")

from seebx.capabilities.observability import telemetry


ACTOR = "1240822d-ac9a-4096-95aa-e2b24d36ef50"


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: Any) -> None:
        return None


class FakeConnection:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.closed = False

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, sql: str, *args: Any) -> str:
        self.execute_calls.append((sql, args))
        return "INSERT 0 1"

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
        self.assertTrue(conn.closed)
        set_config = conn.execute_calls[0]
        insert = conn.execute_calls[1]
        self.assertIn("set_config('app.user_id'", set_config[0])
        self.assertEqual(set_config[1], (ACTOR,))
        self.assertEqual(insert[1][11], ACTOR)
        self.assertNotIn("vantage_id", insert[0])
        self.assertIn("no-store", response.headers["cache-control"])


if __name__ == "__main__":
    unittest.main()
