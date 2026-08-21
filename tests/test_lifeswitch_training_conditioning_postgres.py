from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

from seebx.adapters.lifeswitch_training_access_postgres import (
    PostgresLifeSwitchTrainingAccessRepository,
    resolve_people_schema,
)
from seebx.adapters.lifeswitch_training_conditioning_postgres import (
    PostgresLifeSwitchTrainingConditioningRepository,
    lifeswitch_training_conditioning_repository,
    resolve_training_schema,
)


OWNER = "11111111-1111-4111-8111-111111111111"
SESSION = "22222222-2222-4222-8222-222222222222"


class FakeTransaction:
    def __init__(self, connection) -> None:
        self.connection = connection

    async def __aenter__(self):
        self.connection.events.append("transaction_enter")

    async def __aexit__(self, exc_type, exc, traceback):
        self.connection.events.append("transaction_exit")


class FakeConnection:
    def __init__(self, *, rows=None, row_values=None, scalar_values=None) -> None:
        self.rows = list(rows or [])
        self.row_values = list(row_values or [])
        self.scalar_values = list(scalar_values or [])
        self.fetch_calls = []
        self.fetchrow_calls = []
        self.fetchval_calls = []
        self.events = []
        self.closed = False

    async def fetch(self, query, *arguments):
        self.fetch_calls.append((query, arguments))
        self.events.append("fetch")
        return self.rows.pop(0) if self.rows else []

    async def fetchrow(self, query, *arguments):
        self.fetchrow_calls.append((query, arguments))
        self.events.append("fetchrow")
        return self.row_values.pop(0) if self.row_values else None

    async def fetchval(self, query, *arguments):
        self.fetchval_calls.append((query, arguments))
        self.events.append("fetchval")
        return self.scalar_values.pop(0) if self.scalar_values else None

    def transaction(self):
        return FakeTransaction(self)

    async def close(self):
        self.closed = True


class TrainingConditioningPostgresTests(unittest.IsolatedAsyncioTestCase):
    def repository(self, connection):
        return PostgresLifeSwitchTrainingConditioningRepository(
            connection, training_schema="lifeswitch_training"
        )

    def test_schema_identifiers_fail_closed(self) -> None:
        self.assertEqual(resolve_training_schema("lifeswitch_training"), "lifeswitch_training")
        self.assertEqual(resolve_people_schema("lifeswitch_people"), "lifeswitch_people")
        with self.assertRaisesRegex(RuntimeError, "invalid LIFESWITCH_TRAINING_SCHEMA"):
            resolve_training_schema("training; drop schema public")
        with self.assertRaisesRegex(RuntimeError, "invalid LIFESWITCH_PEOPLE_SCHEMA"):
            resolve_people_schema("people; drop schema public")

    async def test_context_closes_on_success_and_failure(self) -> None:
        request = SimpleNamespace()
        first = FakeConnection()

        async def first_factory(_request):
            return first

        async with lifeswitch_training_conditioning_repository(
            request, connection_factory=first_factory
        ):
            self.assertFalse(first.closed)
        self.assertTrue(first.closed)

        second = FakeConnection()

        async def second_factory(_request):
            return second

        with self.assertRaisesRegex(RuntimeError, "boom"):
            async with lifeswitch_training_conditioning_repository(
                request, connection_factory=second_factory
            ):
                raise RuntimeError("boom")
        self.assertTrue(second.closed)

    async def test_permission_query_is_owner_pair_and_scope_bound(self) -> None:
        connection = FakeConnection(row_values=[{"relationship_permission_id": 1}])
        repository = PostgresLifeSwitchTrainingAccessRepository(
            connection, people_schema="lifeswitch_people"
        )
        allowed = await repository.has_people_permission(
            grantor_user_id=OWNER,
            grantee_user_id=SESSION,
            scope="training:view",
        )
        self.assertTrue(allowed)
        query, arguments = connection.fetchrow_calls[0]
        self.assertIn("rp.grantor_user_id=$1::uuid", query)
        self.assertIn("r.status='accepted'", query)
        self.assertEqual(arguments, (OWNER, SESSION, "training:view"))

    async def test_library_and_sessions_preserve_filters(self) -> None:
        connection = FakeConnection(rows=[[{"slug": "walk"}], [{"day": "2026-08-21"}]])
        repository = self.repository(connection)
        library = await repository.list_library(include_inactive=False)
        sessions = await repository.list_sessions(
            owner_user_id=OWNER,
            delegated=True,
            day="2026-08-21",
            include_inactive=False,
            limit=25,
        )
        self.assertEqual(library, ({"slug": "walk"},))
        self.assertEqual(sessions, ({"day": "2026-08-21"},))
        self.assertIn("where is_active=true", connection.fetch_calls[0][0])
        self.assertIn("conditioning_session_current_v", connection.fetch_calls[1][0])
        self.assertIn("c.day=$3::date", connection.fetch_calls[1][0])
        self.assertEqual(connection.fetch_calls[1][1], (OWNER, True, "2026-08-21"))

    async def test_create_session_preserves_atomic_writer_and_projection(self) -> None:
        row = {"conditioning_session_log_id": SESSION, "owner_user_id": OWNER}
        connection = FakeConnection(row_values=[row], scalar_values=[OWNER, SESSION])
        result = await self.repository(connection).create_session(
            owner_user_id=OWNER,
            intent={"day": "2026-08-21", "name": "Walk"},
            idempotency_key="conditioning-create-1",
        )
        self.assertEqual(result, row)
        self.assertEqual(
            connection.events,
            [
                "transaction_enter",
                "fetchval",
                "fetchval",
                "fetchrow",
                "transaction_exit",
            ],
        )
        self.assertIn("set_config('app.user_id'", connection.fetchval_calls[0][0])
        self.assertIn(".create_conditioning_session(", connection.fetchval_calls[1][0])

    def test_conditioning_handlers_have_no_direct_database_calls(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "seebx/capabilities/training/routes.py").read_text()
        tree = ast.parse(source)
        names = {
            "list_conditioning_library",
            "list_my_conditioning_prescriptions",
            "upsert_my_conditioning_prescription",
            "deactivate_my_conditioning_prescription",
            "create_conditioning_session",
            "list_conditioning_sessions",
            "get_conditioning_session",
            "deactivate_conditioning_session",
            "correct_conditioning_session",
        }
        forbidden = {"fetch", "fetchrow", "fetchval", "execute", "close", "transaction"}
        visited = set()
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name not in names:
                continue
            visited.add(node.name)
            calls = [
                child.func.attr
                for child in ast.walk(node)
                if isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr in forbidden
            ]
            self.assertEqual(calls, [], node.name)
        self.assertEqual(visited, names)


if __name__ == "__main__":
    unittest.main()
