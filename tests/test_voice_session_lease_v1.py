from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from fastapi import FastAPI
from fastapi import Request
from fastapi.testclient import TestClient

from seebx.capabilities.voice import session as lease


ROOT = Path(__file__).resolve().parents[1]
ACTOR = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
SESSION = "0fc3d70a-a6d0-4e55-9e39-20e060b416c8"


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeConnection:
    def __init__(
        self,
        *,
        heartbeat_active: bool = True,
        session_active: bool = True,
    ) -> None:
        self.heartbeat_active = heartbeat_active
        self.session_active = session_active
        self.closed = False
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, sql: str, *args: object) -> str:
        self.calls.append((sql, args))
        if "DELETE FROM voice.voice_session_lease" in sql:
            return "DELETE 1"
        return "SELECT 1"

    async def fetchrow(self, sql: str, *args: object) -> dict[str, object] | None:
        self.calls.append((sql, args))
        if "UPDATE voice.voice_session_lease" in sql and not self.heartbeat_active:
            return None
        return {
            "session_id": UUID(SESSION),
            "expires_at": datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc),
        }

    async def fetchval(self, sql: str, *args: object) -> bool:
        self.calls.append((sql, args))
        return self.session_active

    async def close(self) -> None:
        self.closed = True


class VoiceSessionLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(lease.router)

        @app.get("/validate")
        async def validate(req: Request):
            session_id = await lease.require_active_voice_session(req, ACTOR)
            return {"session_id": str(session_id)}

        self.client = TestClient(app)

    @staticmethod
    def headers(*, owner: str = ACTOR) -> dict[str, str]:
        return {
            "x-vs-actor-user-id": ACTOR,
            "x-vs-owner-user-id": owner,
        }

    def test_wire_uuid_is_strictly_parsed(self) -> None:
        parsed = lease.VoiceSessionRequestV1.model_validate(
            {"session_id": SESSION}
        )
        self.assertEqual(parsed.session_id, UUID(SESSION))
        with self.assertRaises(ValueError):
            lease.VoiceSessionRequestV1.model_validate(
                {"session_id": "not-a-uuid"}
            )

    def test_acquire_is_owner_bound_and_no_store(self) -> None:
        connection = FakeConnection()

        async def connect(*args: object, **kwargs: object) -> FakeConnection:
            return connection

        with (
            patch.object(lease.store, "POSTGRES_DSN", "postgresql://test"),
            patch.object(lease.store.asyncpg, "connect", connect),
        ):
            response = self.client.post(
                "/voice/session/acquire",
                headers=self.headers(),
                json={"session_id": SESSION},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["acquired"])
        self.assertEqual(response.json()["session_id"], SESSION)
        self.assertEqual(response.headers["cache-control"].split(",")[0], "private")
        self.assertTrue(connection.closed)
        sql = "\n".join(call[0] for call in connection.calls)
        self.assertIn("ON CONFLICT(owner_user_id) DO UPDATE", sql)
        self.assertIn("set_config('app.user_id'", sql)

    def test_actor_owner_mismatch_is_rejected_before_database(self) -> None:
        response = self.client.post(
            "/voice/session/acquire",
            headers=self.headers(owner="557ea042-cb82-48f8-9429-472e96c957ef"),
            json={"session_id": SESSION},
        )
        self.assertEqual(response.status_code, 403)

    def test_active_session_is_owner_bound_and_store_backed(self) -> None:
        connection = FakeConnection()

        async def connect(*args: object, **kwargs: object) -> FakeConnection:
            return connection

        with (
            patch.object(lease.store, "POSTGRES_DSN", "postgresql://test"),
            patch.object(lease.store.asyncpg, "connect", connect),
        ):
            response = self.client.get(
                "/validate",
                headers={
                    "x-vs-actor-user-id": ACTOR,
                    "x-vs-voice-session-id": SESSION,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["session_id"], SESSION)
        self.assertTrue(connection.closed)
        sql = "\n".join(call[0] for call in connection.calls)
        self.assertIn("FROM voice.voice_session_lease", sql)
        self.assertIn("set_config('app.user_id'", sql)

    def test_superseded_heartbeat_returns_conflict(self) -> None:
        connection = FakeConnection(heartbeat_active=False)

        async def connect(*args: object, **kwargs: object) -> FakeConnection:
            return connection

        with (
            patch.object(lease.store, "POSTGRES_DSN", "postgresql://test"),
            patch.object(lease.store.asyncpg, "connect", connect),
        ):
            response = self.client.post(
                "/voice/session/heartbeat",
                headers=self.headers(),
                json={"session_id": SESSION},
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["error"],
            "voice_session_not_active",
        )

    def test_missing_session_header_is_rejected_before_database(self) -> None:
        response = self.client.get(
            "/validate",
            headers={"x-vs-actor-user-id": ACTOR},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["error"],
            "voice_session_required",
        )

    def test_release_is_owner_scoped_and_no_store(self) -> None:
        connection = FakeConnection()

        async def connect(*args: object, **kwargs: object) -> FakeConnection:
            return connection

        with (
            patch.object(lease.store, "POSTGRES_DSN", "postgresql://test"),
            patch.object(lease.store.asyncpg, "connect", connect),
        ):
            response = self.client.post(
                "/voice/session/release",
                headers=self.headers(),
                json={"session_id": SESSION},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["released"])
        self.assertEqual(response.headers["cache-control"].split(",")[0], "private")
        self.assertTrue(connection.closed)
        sql = "\n".join(call[0] for call in connection.calls)
        self.assertIn("DELETE FROM voice.voice_session_lease", sql)

    def test_migration_forces_owner_rls_and_has_rollback(self) -> None:
        migration = (
            ROOT / "ops/sql/20260724_voice_session_lease_v1.sql"
        ).read_text()
        rollback = (
            ROOT / "ops/sql/20260724_voice_session_lease_v1_rollback.sql"
        ).read_text()
        self.assertIn("FORCE ROW LEVEL SECURITY", migration)
        self.assertIn("voice_session_lease_owner_policy", migration)
        self.assertIn("current_setting('app.user_id', true)", migration)
        self.assertIn("DROP TABLE IF EXISTS public.voice_session_lease", rollback)


if __name__ == "__main__":
    unittest.main()
