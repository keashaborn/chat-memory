from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("LIFESWITCH_POSTGRES_DSN", "postgresql://isolated.invalid/lifeswitch")

from fastapi import HTTPException

from seebx.adapters import lifeswitch_postgres
from seebx.capabilities.measurements import routes as measurements
from seebx.capabilities.nutrition import logs as nutrition
from seebx.capabilities.training import routes as training


OWNER = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


class FakeRequest:
    headers = {"x-vs-actor-user-id": OWNER}


class FakeConnection:
    def __init__(self):
        self.execute_calls = []
        self.closed = False

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))
        return "SELECT 1"

    async def close(self):
        self.closed = True


class NeverQueryPeople:
    async def fetchrow(self, *_args, **_kwargs):
        raise AssertionError("self-only mode must not query lifeswitch_people")


class IsolatedPostgresV1Tests(unittest.IsolatedAsyncioTestCase):
    def test_people_routes_are_removed(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "app.py").read_text()
        self.assertNotIn("lifeswitch_people_router", app_source)
        self.assertNotIn('prefix="/lifeswitch/people"', app_source)
        self.assertFalse((root / "rag_engine" / "lifeswitch_people_router.py").exists())

    async def test_connection_binds_authenticated_actor_for_rls(self):
        conn = FakeConnection()
        with patch.object(
            lifeswitch_postgres.asyncpg,
            "connect",
            AsyncMock(return_value=conn),
        ):
            actual = await lifeswitch_postgres.connect_lifeswitch(FakeRequest())

        self.assertIs(actual, conn)
        self.assertEqual(len(conn.execute_calls), 1)
        query, args = conn.execute_calls[0]
        self.assertIn("set_config('app.user_id',$1,false)", query)
        self.assertIn("set_config('app.lifeswitch_owner_id',$1,false)", query)
        self.assertEqual(args, (OWNER,))

    async def test_connection_resolves_isolated_dsn_at_call_time(self):
        conn = FakeConnection()
        connect = AsyncMock(return_value=conn)
        with (
            patch.dict(
                os.environ,
                {
                    "LIFESWITCH_POSTGRES_DSN": (
                        "postgresql://current.invalid/lifeswitch"
                    )
                },
                clear=False,
            ),
            patch.object(lifeswitch_postgres.asyncpg, "connect", connect),
        ):
            await lifeswitch_postgres.connect_lifeswitch(FakeRequest())

        connect.assert_awaited_once_with(
            "postgresql://current.invalid/lifeswitch"
        )

    async def test_platform_dsn_is_never_a_lifeswitch_fallback(self):
        with patch.dict(
            os.environ,
            {"POSTGRES_DSN": "postgresql://platform.invalid/memory"},
            clear=True,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "LIFESWITCH_POSTGRES_DSN missing",
            ):
                await lifeswitch_postgres.connect_lifeswitch(FakeRequest())

    async def test_delegated_reads_are_disabled_without_people_database(self):
        os.environ.pop("LIFESWITCH_DELEGATED_READS_ENABLED", None)
        cases = (
            nutrition._resolve_nutrition_view_target,
            training._resolve_training_view_target,
            measurements._resolve_measurements_view_target,
        )
        for resolver in cases:
            with self.subTest(resolver=resolver.__name__):
                with self.assertRaises(HTTPException) as raised:
                    await resolver(NeverQueryPeople(), OWNER, OTHER)
                self.assertEqual(raised.exception.status_code, 403)
                self.assertEqual(raised.exception.detail, "delegated_access_disabled")

    async def test_every_account_can_read_its_own_records(self):
        cases = (
            nutrition._resolve_nutrition_view_target,
            training._resolve_training_view_target,
            measurements._resolve_measurements_view_target,
        )
        for resolver in cases:
            with self.subTest(resolver=resolver.__name__):
                owner, delegated = await resolver(NeverQueryPeople(), OWNER, "")
                self.assertEqual(owner, OWNER)
                self.assertIs(delegated, False)


if __name__ == "__main__":
    unittest.main()
