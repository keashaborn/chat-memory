from __future__ import annotations

import ast
import datetime as dt
import unittest
from pathlib import Path
from types import SimpleNamespace

from seebx.adapters.lifeswitch_training_sessions_postgres import (
    PostgresLifeSwitchTrainingSessionsRepository,
    lifeswitch_training_sessions_repository,
    resolve_training_schema,
)


OWNER = "11111111-1111-4111-8111-111111111111"
SESSION = "22222222-2222-4222-8222-222222222222"
SET_LOG = "33333333-3333-4333-8333-333333333333"


class FakeTransaction:
    def __init__(self, connection) -> None:
        self.connection = connection

    async def __aenter__(self):
        self.connection.events.append("transaction_enter")

    async def __aexit__(self, exc_type, exc, traceback):
        self.connection.events.append(
            "transaction_rollback" if exc_type else "transaction_commit"
        )


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


class TrainingSessionsPostgresTests(unittest.IsolatedAsyncioTestCase):
    def repository(self, connection):
        return PostgresLifeSwitchTrainingSessionsRepository(
            connection, training_schema="lifeswitch_training"
        )

    def test_schema_identifier_fails_closed(self) -> None:
        self.assertEqual(
            resolve_training_schema("lifeswitch_training"), "lifeswitch_training"
        )
        with self.assertRaisesRegex(RuntimeError, "invalid LIFESWITCH_TRAINING_SCHEMA"):
            resolve_training_schema("training; drop schema public")

    async def test_context_closes_on_success_and_failure(self) -> None:
        request = SimpleNamespace()
        first = FakeConnection()

        async def first_factory(_request):
            return first

        async with lifeswitch_training_sessions_repository(
            request, connection_factory=first_factory
        ):
            self.assertFalse(first.closed)
        self.assertTrue(first.closed)

        second = FakeConnection()

        async def second_factory(_request):
            return second

        with self.assertRaisesRegex(RuntimeError, "boom"):
            async with lifeswitch_training_sessions_repository(
                request, connection_factory=second_factory
            ):
                raise RuntimeError("boom")
        self.assertTrue(second.closed)

    async def test_complete_session_preserves_atomic_writer_and_projection(self) -> None:
        row = {"training_session_id": SESSION, "owner_user_id": OWNER}
        connection = FakeConnection(
            row_values=[row], scalar_values=[OWNER, SESSION]
        )
        result = await self.repository(connection).complete_session(
            owner=OWNER,
            intent={"day": "2026-08-21", "name": "Strength", "sets": [{}]},
            idempotency_key="strength-complete-1",
        )
        self.assertEqual(result, row)
        self.assertEqual(
            connection.events,
            [
                "transaction_enter",
                "fetchval",
                "fetchval",
                "fetchrow",
                "transaction_commit",
            ],
        )
        self.assertIn("set_config('app.user_id'", connection.fetchval_calls[0][0])
        self.assertIn(".create_training_session(", connection.fetchval_calls[1][0])
        self.assertIn("from lifeswitch_training.training_session", connection.fetchrow_calls[0][0])

    async def test_list_sessions_preserves_filters_and_owner_arguments(self) -> None:
        connection = FakeConnection(rows=[[{"training_session_id": SESSION}]])
        rows = await self.repository(connection).list_sessions(
            owner=OWNER,
            delegated=True,
            day_val=dt.date(2026, 8, 21),
            include_inactive=False,
            limit=25,
        )
        self.assertEqual(rows, ({"training_session_id": SESSION},))
        query, arguments = connection.fetch_calls[0]
        self.assertIn("training_session_current_v", query)
        self.assertIn("s.day=$3::date", query)
        self.assertIn("s.finished_at is not null", query)
        self.assertIn("limit 25", query)
        self.assertEqual(arguments, (OWNER, True, dt.date(2026, 8, 21)))

    async def test_correction_preserves_transaction_and_current_projection(self) -> None:
        row = {"training_session_id": SESSION, "owner_user_id": OWNER}
        connection = FakeConnection(
            row_values=[row], scalar_values=[OWNER, SESSION]
        )
        result = await self.repository(connection).correct_session(
            sid=SESSION,
            owner=OWNER,
            intent={"sets": []},
            idempotency_key="strength-correct-1",
        )
        self.assertEqual(result, row)
        self.assertEqual(connection.events[-1], "transaction_commit")
        self.assertIn(".correct_training_session(", connection.fetchval_calls[1][0])
        self.assertIn("training_session_current_v", connection.fetchrow_calls[0][0])

    async def test_missing_set_parent_stops_before_segment_access(self) -> None:
        connection = FakeConnection(row_values=[None])
        rows = await self.repository(connection).list_set_segments(
            sid=SESSION,
            setid=SET_LOG,
            owner=OWNER,
            delegated=False,
        )
        self.assertIsNone(rows)
        self.assertEqual(connection.fetch_calls, [])
        query, arguments = connection.fetchrow_calls[0]
        self.assertIn("training_session_current_v", query)
        self.assertEqual(arguments, (SET_LOG, SESSION, OWNER))

    def test_session_handlers_have_no_direct_database_calls(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "seebx/capabilities/training/sessions.py").read_text()
        tree = ast.parse(source)
        names = {
            "complete_training_session",
            "list_training_sessions",
            "get_training_session",
            "list_strength_progression",
            "deactivate_training_session",
            "correct_training_session",
            "list_training_session_sets",
            "list_training_set_log_segments",
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
        self.assertNotIn("connect_lifeswitch", source)
        self.assertNotIn("import asyncpg", source)

    def test_adapter_has_no_fastapi_dependency(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (
            root / "seebx/adapters/lifeswitch_training_sessions_postgres.py"
        ).read_text()
        self.assertNotIn("from fastapi", source)
        self.assertNotIn("HTTPException", source)


if __name__ == "__main__":
    unittest.main()
