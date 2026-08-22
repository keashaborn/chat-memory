from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

from seebx.adapters.lifeswitch_training_exercises_postgres import (
    PostgresLifeSwitchTrainingExercisesRepository,
    lifeswitch_training_exercises_repository,
    resolve_training_schema,
)


OWNER = "11111111-1111-4111-8111-111111111111"
MY_EXERCISE = "22222222-2222-4222-8222-222222222222"


class FakeTransaction:
    def __init__(self, connection) -> None:
        self.connection = connection

    async def __aenter__(self):
        self.connection.events.append("transaction_enter")

    async def __aexit__(self, exc_type, exc, traceback):
        self.connection.events.append("transaction_exit")


class FakeConnection:
    def __init__(self, *, rows=None, row_values=None) -> None:
        self.rows = list(rows or [])
        self.row_values = list(row_values or [])
        self.fetch_calls = []
        self.fetchrow_calls = []
        self.execute_calls = []
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

    async def execute(self, query, *arguments):
        self.execute_calls.append((query, arguments))
        self.events.append("execute")
        return "INSERT 1"

    def transaction(self):
        return FakeTransaction(self)

    async def close(self):
        self.closed = True


class TrainingExercisesPostgresTests(unittest.IsolatedAsyncioTestCase):
    def repository(self, connection):
        return PostgresLifeSwitchTrainingExercisesRepository(
            connection,
            training_schema="lifeswitch_training",
        )

    def test_schema_identifier_fails_closed(self) -> None:
        self.assertEqual(resolve_training_schema("lifeswitch_training"), "lifeswitch_training")
        with self.assertRaisesRegex(RuntimeError, "invalid LIFESWITCH_TRAINING_SCHEMA"):
            resolve_training_schema("training; drop schema public")

    async def test_context_closes_on_success_and_failure(self) -> None:
        request = SimpleNamespace()
        first = FakeConnection()

        async def first_factory(_request):
            return first

        async with lifeswitch_training_exercises_repository(
            request, connection_factory=first_factory
        ):
            self.assertFalse(first.closed)
        self.assertTrue(first.closed)

        second = FakeConnection()

        async def second_factory(_request):
            return second

        with self.assertRaisesRegex(RuntimeError, "boom"):
            async with lifeswitch_training_exercises_repository(
                request, connection_factory=second_factory
            ):
                raise RuntimeError("boom")
        self.assertTrue(second.closed)

    async def test_list_is_owner_bound_and_preserves_active_filter(self) -> None:
        connection = FakeConnection(rows=[[{"my_exercise_id": MY_EXERCISE}]])
        rows = await self.repository(connection).list_exercises(
            owner_user_id=OWNER,
            include_inactive=False,
        )
        self.assertEqual(rows, ({"my_exercise_id": MY_EXERCISE},))
        query, arguments = connection.fetch_calls[0]
        self.assertIn("me.owner_user_id=$1::uuid", query)
        self.assertIn("and me.is_active=true", query)
        self.assertEqual(arguments, (OWNER,))

    async def test_upsert_keeps_role_event_in_one_transaction(self) -> None:
        existing = {"my_exercise_id": MY_EXERCISE, "exercise_role": "strength"}
        updated = {
            "my_exercise_id": MY_EXERCISE,
            "exercise_id": "squat",
            "exercise_role": "rehab",
        }
        connection = FakeConnection(row_values=[existing, updated])
        result = await self.repository(connection).upsert_exercise(
            owner_user_id=OWNER,
            exercise_id="squat",
            display_name="Squat",
            kind="strength",
            modality="barbell",
            brand_name=None,
            model_name=None,
            matched_text=None,
            matched_source=None,
            exercise_role="rehab",
        )
        self.assertEqual(result, updated)
        self.assertEqual(
            connection.events,
            ["transaction_enter", "fetchrow", "fetchrow", "execute", "transaction_exit"],
        )
        self.assertEqual(
            connection.execute_calls[0][1],
            (OWNER, MY_EXERCISE, "squat", "strength", "rehab"),
        )

    async def test_unchanged_role_does_not_write_event(self) -> None:
        existing = {"my_exercise_id": MY_EXERCISE, "exercise_role": "strength"}
        updated = {
            "my_exercise_id": MY_EXERCISE,
            "exercise_id": "squat",
            "exercise_role": "strength",
        }
        connection = FakeConnection(row_values=[existing, updated])
        await self.repository(connection).upsert_exercise(
            owner_user_id=OWNER,
            exercise_id="squat",
            display_name="Squat",
            kind="strength",
            modality="barbell",
            brand_name=None,
            model_name=None,
            matched_text=None,
            matched_source=None,
            exercise_role=None,
        )
        self.assertEqual(connection.execute_calls, [])
        self.assertEqual(
            connection.events,
            ["transaction_enter", "fetchrow", "fetchrow", "transaction_exit"],
        )

    async def test_deactivate_is_owner_bound_and_nontransactional(self) -> None:
        row = {"my_exercise_id": MY_EXERCISE, "owner_user_id": OWNER}
        connection = FakeConnection(row_values=[row])
        result = await self.repository(connection).deactivate_exercise(
            my_exercise_id=MY_EXERCISE,
            owner_user_id=OWNER,
        )
        self.assertEqual(result, row)
        self.assertEqual(connection.fetchrow_calls[0][1], (MY_EXERCISE, OWNER))
        self.assertNotIn("transaction_enter", connection.events)

    def test_three_capability_handlers_have_no_direct_database_calls(self) -> None:
        root = Path(__file__).resolve().parents[1]
        path = root / "seebx/capabilities/training/exercises.py"
        source = path.read_text()
        tree = ast.parse(source)
        names = {"list_my_exercises", "upsert_my_exercise", "deactivate_my_exercise"}
        forbidden = {"fetch", "fetchrow", "fetchval", "execute", "close", "transaction"}
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name not in names:
                continue
            calls = [
                child.func.attr
                for child in ast.walk(node)
                if isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr in forbidden
            ]
            self.assertEqual(calls, [], node.name)


if __name__ == "__main__":
    unittest.main()
