from __future__ import annotations

import ast
import datetime as dt
import unittest
from pathlib import Path
from types import SimpleNamespace

from seebx.adapters.lifeswitch_training_sharing_postgres import (
    PostgresLifeSwitchTrainingSharingRepository,
    WorkoutShareImportError,
    lifeswitch_training_sharing_repository,
    resolve_training_schema,
)


OWNER = "11111111-1111-4111-8111-111111111111"
TEMPLATE = "22222222-2222-4222-8222-222222222222"
SHARE = "33333333-3333-4333-8333-333333333333"
NOW = dt.datetime(2026, 8, 21, tzinfo=dt.timezone.utc)


class FakeTransaction:
    def __init__(self, connection) -> None:
        self.connection = connection

    async def __aenter__(self):
        self.connection.events.append("transaction_enter")

    async def __aexit__(self, exc_type, exc, traceback):
        self.connection.events.append(
            "transaction_rollback" if exc_type is not None else "transaction_commit"
        )


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
        return "UPDATE 1"

    def transaction(self):
        return FakeTransaction(self)

    async def close(self):
        self.closed = True


class TrainingSharingPostgresTests(unittest.IsolatedAsyncioTestCase):
    def repository(self, connection):
        return PostgresLifeSwitchTrainingSharingRepository(
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

        async with lifeswitch_training_sharing_repository(
            request, connection_factory=first_factory
        ):
            self.assertFalse(first.closed)
        self.assertTrue(first.closed)

        second = FakeConnection()

        async def second_factory(_request):
            return second

        with self.assertRaisesRegex(RuntimeError, "boom"):
            async with lifeswitch_training_sharing_repository(
                request, connection_factory=second_factory
            ):
                raise RuntimeError("boom")
        self.assertTrue(second.closed)

    async def test_create_and_list_preserve_owner_and_active_filters(self) -> None:
        template = {"workout_template_id": TEMPLATE, "name": "Strength"}
        share = {"workout_template_share_id": SHARE}
        connection = FakeConnection(
            rows=[[share]], row_values=[template, share]
        )
        repository = self.repository(connection)
        created = await repository.create_share(
            owner_user_id=OWNER,
            workout_template_id=TEMPLATE,
            token_hash="abc",
            label="Coach",
            notes="Notes",
        )
        listed = await repository.list_shares(
            owner_user_id=OWNER, include_inactive=False
        )
        self.assertEqual(created, (template, share))
        self.assertEqual(listed, (share,))
        self.assertIn("owner_user_id=$2::uuid", connection.fetchrow_calls[0][0])
        self.assertIn("and s.status='active'", connection.fetch_calls[0][0])

    async def test_preview_expiration_is_persisted_without_loading_children(self) -> None:
        share = {
            "workout_template_share_id": SHARE,
            "workout_template_id": TEMPLATE,
            "status": "active",
            "expires_at": NOW - dt.timedelta(seconds=1),
        }
        connection = FakeConnection(row_values=[share])
        result = await self.repository(connection).preview_share(
            token_hash="abc", now=NOW
        )
        self.assertTrue(result.expired)
        self.assertEqual(result.exercises, ())
        self.assertEqual(len(connection.execute_calls), 1)
        self.assertEqual(connection.fetch_calls, [])

    async def test_preview_loads_exercises_and_segments(self) -> None:
        exercise = {"workout_template_exercise_id": TEMPLATE}
        segment = {"workout_template_exercise_id": TEMPLATE, "segment_index": 1}
        share = {
            "workout_template_share_id": SHARE,
            "workout_template_id": TEMPLATE,
            "status": "active",
            "expires_at": NOW + dt.timedelta(days=1),
        }
        connection = FakeConnection(rows=[[exercise], [segment]], row_values=[share])
        result = await self.repository(connection).preview_share(
            token_hash="abc", now=NOW
        )
        self.assertFalse(result.expired)
        self.assertEqual(result.exercises, (exercise,))
        self.assertEqual(result.segments, (segment,))
        self.assertEqual(connection.fetch_calls[1][1], ([TEMPLATE],))

    async def test_expired_import_rolls_back_status_update(self) -> None:
        share = {
            "workout_template_share_id": SHARE,
            "status": "active",
            "expires_at": NOW - dt.timedelta(seconds=1),
            "workout_is_active": True,
        }
        connection = FakeConnection(row_values=[share])
        with self.assertRaisesRegex(WorkoutShareImportError, "share expired") as caught:
            await self.repository(connection).import_share(
                importer_user_id=OWNER, token_hash="abc", now=NOW
            )
        self.assertEqual(caught.exception.code, "expired")
        self.assertEqual(
            connection.events,
            ["transaction_enter", "fetchrow", "execute", "transaction_rollback"],
        )

    async def test_import_preserves_single_transaction(self) -> None:
        share = {
            "workout_template_share_id": SHARE,
            "workout_template_id": TEMPLATE,
            "status": "active",
            "expires_at": NOW + dt.timedelta(days=1),
            "workout_is_active": True,
            "workout_name": "Strength",
            "workout_notes": "Notes",
        }
        imported = {"workout_template_id": "44444444-4444-4444-8444-444444444444"}
        connection = FakeConnection(rows=[[]], row_values=[share, None, imported])
        result = await self.repository(connection).import_share(
            importer_user_id=OWNER, token_hash="abc", now=NOW
        )
        self.assertEqual(result.imported_workout, imported)
        self.assertEqual(result.copied_exercises, ())
        self.assertEqual(connection.events[0], "transaction_enter")
        self.assertEqual(connection.events[-1], "transaction_commit")

    def test_sharing_handlers_have_no_direct_database_calls(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "seebx/capabilities/training/sharing.py").read_text()
        tree = ast.parse(source)
        names = {
            "create_workout_template_share",
            "list_workout_template_shares",
            "preview_workout_template_share",
            "import_workout_template_share",
            "revoke_workout_template_share",
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
