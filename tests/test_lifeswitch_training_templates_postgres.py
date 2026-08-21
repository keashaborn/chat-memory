from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

from seebx.adapters.lifeswitch_training_templates_postgres import (
    PostgresLifeSwitchTrainingTemplatesRepository,
    TemplateUpsertError,
    lifeswitch_training_templates_repository,
    resolve_training_schema,
)


OWNER = "11111111-1111-4111-8111-111111111111"
TEMPLATE = "22222222-2222-4222-8222-222222222222"
EXERCISE = "33333333-3333-4333-8333-333333333333"


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

    async def fetchval(self, query, *arguments):
        self.fetchval_calls.append((query, arguments))
        self.events.append("fetchval")
        return self.scalar_values.pop(0) if self.scalar_values else None

    async def execute(self, query, *arguments):
        self.execute_calls.append((query, arguments))
        self.events.append("execute")
        return "DELETE 1"

    def transaction(self):
        return FakeTransaction(self)

    async def close(self):
        self.closed = True


class TrainingTemplatesPostgresTests(unittest.IsolatedAsyncioTestCase):
    def repository(self, connection):
        return PostgresLifeSwitchTrainingTemplatesRepository(
            connection, training_schema="lifeswitch_training"
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

        async with lifeswitch_training_templates_repository(
            request, connection_factory=first_factory
        ):
            self.assertFalse(first.closed)
        self.assertTrue(first.closed)

        second = FakeConnection()

        async def second_factory(_request):
            return second

        with self.assertRaisesRegex(RuntimeError, "boom"):
            async with lifeswitch_training_templates_repository(
                request, connection_factory=second_factory
            ):
                raise RuntimeError("boom")
        self.assertTrue(second.closed)

    async def test_list_templates_preserves_active_filter(self) -> None:
        connection = FakeConnection(rows=[[{"workout_template_id": TEMPLATE}]])
        rows = await self.repository(connection).list_templates(
            owner_user_id=OWNER, include_inactive=False
        )
        self.assertEqual(len(rows), 1)
        self.assertIn("and is_active=true", connection.fetch_calls[0][0])
        self.assertEqual(connection.fetch_calls[0][1], (OWNER,))

    async def test_upsert_preserves_transaction_and_owner_callback_position(self) -> None:
        saved = {"workout_template_id": TEMPLATE}
        final = {"workout_template_id": TEMPLATE, "workout_role": "strength"}
        connection = FakeConnection(
            row_values=[saved, final],
            scalar_values=[None, OWNER, "strength"],
        )

        def authorize(existing_owner) -> None:
            self.assertEqual(str(existing_owner), OWNER)
            connection.events.append("owner_authorized")

        result = await self.repository(connection).upsert_template(
            owner_user_id=OWNER,
            workout_template_id=TEMPLATE,
            name="Strength",
            notes="Notes",
            workout_role="strength",
            idempotency_key="template-upsert-1",
            verify_existing_owner=authorize,
        )
        self.assertEqual(result, final)
        self.assertEqual(
            connection.events,
            [
                "transaction_enter",
                "fetchval",
                "fetchval",
                "owner_authorized",
                "fetchrow",
                "fetchval",
                "fetchrow",
                "transaction_commit",
            ],
        )

    async def test_owner_callback_failure_rolls_back_before_write(self) -> None:
        connection = FakeConnection(scalar_values=[None, "other-owner"])

        def reject(_owner) -> None:
            connection.events.append("owner_rejected")
            raise RuntimeError("owner mismatch")

        with self.assertRaisesRegex(RuntimeError, "owner mismatch"):
            await self.repository(connection).upsert_template(
                owner_user_id=OWNER,
                workout_template_id=TEMPLATE,
                name="Strength",
                notes="",
                workout_role="strength",
                idempotency_key="template-upsert-2",
                verify_existing_owner=reject,
            )
        self.assertEqual(connection.fetchrow_calls, [])
        self.assertEqual(connection.events[-1], "transaction_rollback")

    async def test_missing_upsert_row_rolls_back(self) -> None:
        connection = FakeConnection(row_values=[None], scalar_values=[None])
        with self.assertRaisesRegex(TemplateUpsertError, "upsert_failed"):
            await self.repository(connection).upsert_template(
                owner_user_id=OWNER,
                workout_template_id=None,
                name="Strength",
                notes="",
                workout_role="strength",
                idempotency_key="template-upsert-3",
                verify_existing_owner=lambda _owner: None,
            )
        self.assertEqual(connection.events[-1], "transaction_rollback")

    async def test_child_authorization_precedes_data_access(self) -> None:
        row = {"workout_template_exercise_id": EXERCISE}
        connection = FakeConnection(rows=[[row]], scalar_values=[OWNER])

        def authorize(owner) -> None:
            self.assertEqual(owner, OWNER)
            connection.events.append("owner_authorized")

        result = await self.repository(connection).list_template_exercises(
            workout_template_id=TEMPLATE,
            authorize_owner=authorize,
        )
        self.assertEqual(result.value, (row,))
        self.assertEqual(
            connection.events, ["fetchval", "owner_authorized", "fetch"]
        )

    async def test_missing_parent_stops_before_child_access(self) -> None:
        connection = FakeConnection(scalar_values=[None])
        result = await self.repository(connection).delete_template_exercise(
            workout_template_id=TEMPLATE,
            workout_template_exercise_id=EXERCISE,
            authorize_owner=lambda _owner: self.fail("authorization should not run"),
        )
        self.assertIsNone(result)
        self.assertEqual(connection.execute_calls, [])

    def test_template_handlers_have_no_direct_database_calls(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "seebx/capabilities/training/routes.py").read_text()
        tree = ast.parse(source)
        names = {
            "list_workout_templates",
            "upsert_workout_template",
            "classify_historical_workout_sessions",
            "deactivate_workout_template",
            "list_workout_template_exercises",
            "upsert_workout_template_exercise",
            "delete_workout_template_exercise",
            "list_workout_template_exercise_segments",
            "upsert_workout_template_exercise_segment",
            "delete_workout_template_exercise_segment",
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

    def test_adapter_has_no_fastapi_dependency(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (
            root / "seebx/adapters/lifeswitch_training_templates_postgres.py"
        ).read_text()
        self.assertNotIn("from fastapi", source)
        self.assertNotIn("HTTPException", source)


if __name__ == "__main__":
    unittest.main()
