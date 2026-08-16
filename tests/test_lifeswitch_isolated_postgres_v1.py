from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("POSTGRES_DSN", "postgresql://legacy.invalid/lifeswitch")

from fastapi import HTTPException

from rag_engine import lifeswitch_db
from rag_engine import lifeswitch_measurements_router as measurements
from rag_engine import lifeswitch_nutrition_log_router as nutrition
from rag_engine import lifeswitch_training_router as training


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
    def test_people_routes_are_preserved_but_disabled_by_default(self):
        app_source = (Path(__file__).resolve().parents[1] / "app.py").read_text()
        self.assertIn('os.getenv("LIFESWITCH_PEOPLE_ENABLED", "0") == "1"', app_source)
        self.assertIn('prefix="/lifeswitch/people"', app_source)

    async def test_connection_binds_authenticated_actor_for_rls(self):
        conn = FakeConnection()
        with patch.object(
            lifeswitch_db.asyncpg,
            "connect",
            AsyncMock(return_value=conn),
        ):
            actual = await lifeswitch_db.connect_lifeswitch(FakeRequest())

        self.assertIs(actual, conn)
        self.assertEqual(len(conn.execute_calls), 1)
        query, args = conn.execute_calls[0]
        self.assertIn("set_config('app.user_id',$1,false)", query)
        self.assertIn("set_config('app.lifeswitch_owner_id',$1,false)", query)
        self.assertEqual(args, (OWNER,))

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
