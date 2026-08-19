from __future__ import annotations

from contextlib import asynccontextmanager
import importlib
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"


class TranscriptRoutesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with patch.dict(
            os.environ,
            {
                "POSTGRES_DSN": (
                    "postgresql://brains_app:synthetic@127.0.0.1:5432/memory"
                )
            },
            clear=False,
        ):
            cls.routes = importlib.import_module(
                "seebx.capabilities.conversation.transcript_routes"
            )

    def application(self, postgres: object) -> FastAPI:
        application = FastAPI()
        application.include_router(
            self.routes.create_transcript_ingest_router(postgres)
        )
        return application

    def test_canonical_router_owns_exact_transcript_surface(self) -> None:
        application = self.application(SimpleNamespace())
        actual = {
            (route.path, tuple(sorted(route.methods or ())))
            for route in application.routes
            if route.path == "/log"
        }
        self.assertEqual(actual, {("/log", ("POST",))})

    def test_retired_identity_write_fails_before_actor_or_database(self) -> None:
        actor = AsyncMock()

        def database_bomb(*args, **kwargs):
            raise AssertionError("database reached for retired identity write")

        postgres = SimpleNamespace(owner_connection=database_bomb)
        with patch.object(self.routes, "require_actor", new=actor):
            response = TestClient(self.application(postgres)).post(
                "/log",
                json={
                    "user_id": OWNER,
                    "source": "frontend/identity",
                    "text": "FULL_NAME: Retired",
                },
            )
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json()["detail"], "legacy_identity_memory_retired")
        actor.assert_not_awaited()

    def test_invalid_voice_turn_fails_before_database(self) -> None:
        actor = AsyncMock(return_value=OWNER)

        def database_bomb(*args, **kwargs):
            raise AssertionError("database reached after invalid voice turn")

        postgres = SimpleNamespace(owner_connection=database_bomb)
        with patch.object(self.routes, "require_actor", new=actor):
            response = TestClient(self.application(postgres)).post(
                "/log",
                headers={"x-vs-voice-turn-id": "not-a-uuid"},
                json={"user_id": OWNER, "text": "Synthetic transcript"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "invalid_voice_turn_id")

    def test_no_store_requires_actor_but_never_reaches_database(self) -> None:
        actor = AsyncMock(return_value=OWNER)

        def database_bomb(*args, **kwargs):
            raise AssertionError("database reached for no-store request")

        postgres = SimpleNamespace(owner_connection=database_bomb)
        with patch.object(self.routes, "require_actor", new=actor):
            response = TestClient(self.application(postgres)).post(
                "/log",
                json={
                    "user_id": OWNER,
                    "text": "Do not retain",
                    "no_store": True,
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "status": "no_store",
                "detail": "transcript_and_memory_not_stored",
            },
        )
        actor.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
