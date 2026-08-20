from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal
import json
import os
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID

os.environ.setdefault("POSTGRES_DSN", "postgresql://test-only")

from seebx.capabilities.catalog import routes as catalog_routes
from seebx.adapters.lifeswitch_catalog_postgres import (
    EXERCISE_BROWSE_SQL,
    EXERCISE_SEARCH_SQL,
    PostgresLifeSwitchCatalogReader,
    lifeswitch_catalog_reader,
)


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeConnection:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.transactions = []
        self.fetched = []
        self.closed = False

    def transaction(self, **options):
        self.transactions.append(options)
        return Transaction()

    async def fetch(self, sql, *arguments):
        self.fetched.append((sql, arguments))
        return self.rows

    async def close(self):
        self.closed = True


class FakeReader:
    def __init__(self, *, search_rows=(), browse_rows=()):
        self.search_rows = list(search_rows)
        self.browse_rows = list(browse_rows)
        self.search_arguments = None
        self.browse_arguments = None

    async def search_exercises(self, *arguments):
        self.search_arguments = arguments
        return self.search_rows

    async def browse_exercises(self, *arguments):
        self.browse_arguments = arguments
        return self.browse_rows


class LifeSwitchCatalogPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_uses_isolated_catalog_in_readonly_transaction(self):
        row = {"exercise_id": "squat"}
        connection = FakeConnection([row])
        reader = PostgresLifeSwitchCatalogReader(connection)

        result = await reader.search_exercises("squat", 25, "en")

        self.assertEqual(result, [row])
        self.assertEqual(connection.transactions, [{"readonly": True}])
        self.assertEqual(
            connection.fetched,
            [(EXERCISE_SEARCH_SQL, ("squat", 25, "en"))],
        )
        self.assertIn("catalog_dev.search_exercises", EXERCISE_SEARCH_SQL)

    async def test_browse_preserves_query_arguments_and_public_filters(self):
        row = {"exercise_family_id": "family-1"}
        connection = FakeConnection([row])
        reader = PostgresLifeSwitchCatalogReader(connection)

        result = await reader.browse_exercises("press", "push", "strength", 50)

        self.assertEqual(result, [row])
        self.assertEqual(connection.transactions, [{"readonly": True}])
        self.assertEqual(
            connection.fetched,
            [(EXERCISE_BROWSE_SQL, ("press", "push", "strength", 50))],
        )
        lowered = EXERCISE_BROWSE_SQL.lower()
        self.assertIn("f.is_active=true", lowered)
        self.assertIn("fm.is_active=true", lowered)
        self.assertIn("e.is_active=true", lowered)
        self.assertIn("e.is_public=true", lowered)

    async def test_context_owns_connection_lifecycle(self):
        connection = FakeConnection()

        async def connect():
            return connection

        async with lifeswitch_catalog_reader(
            connection_factory=connect,
        ) as reader:
            self.assertIsInstance(reader, PostgresLifeSwitchCatalogReader)
            self.assertFalse(connection.closed)
        self.assertTrue(connection.closed)

    async def test_default_connection_uses_only_lifeswitch_dsn(self):
        connection = FakeConnection()
        connect = AsyncMock(return_value=connection)
        environment = {
            "POSTGRES_DSN": "postgresql://platform.invalid/memory",
            "LIFESWITCH_POSTGRES_DSN": (
                "postgresql://isolated.invalid/lifeswitch"
            ),
        }
        with patch.dict(os.environ, environment, clear=True):
            with patch(
                "seebx.adapters.lifeswitch_catalog_postgres.asyncpg.connect",
                connect,
            ):
                async with lifeswitch_catalog_reader():
                    pass

        connect.assert_awaited_once_with(
            "postgresql://isolated.invalid/lifeswitch"
        )
        self.assertTrue(connection.closed)

    async def test_search_route_preserves_arguments_and_json_shape(self):
        exercise_id = UUID("a6ac2d36-4f4d-4a6a-9914-50356c1031e9")
        reader = FakeReader(
            search_rows=[
                {
                    "exercise_id": exercise_id,
                    "display_name": "Back Squat",
                    "kind": "strength",
                    "modality": "barbell",
                    "score": Decimal("0.875"),
                    "matched_text": "squat",
                    "matched_source": "display_name",
                    "brand_name": None,
                    "model_name": None,
                }
            ]
        )

        @asynccontextmanager
        async def context():
            yield reader

        with patch.object(catalog_routes, "lifeswitch_catalog_reader", context):
            response = await catalog_routes.search_exercises(
                q="squat",
                limit=25,
                locale="en",
            )

        self.assertEqual(reader.search_arguments, ("squat", 25, "en"))
        self.assertEqual(
            json.loads(response.body),
            [
                {
                    "exercise_id": str(exercise_id),
                    "display_name": "Back Squat",
                    "kind": "strength",
                    "modality": "barbell",
                    "score": 0.875,
                    "matched_text": "squat",
                    "matched_source": "display_name",
                    "brand_name": None,
                    "model_name": None,
                }
            ],
        )

    async def test_browse_route_preserves_normalization_and_aggregation(self):
        family_id = UUID("b125521c-a1de-4612-bc68-0ff55dfbfead")
        base = {
            "exercise_family_id": family_id,
            "family_slug": "bench_press",
            "family_name": "Bench Press",
            "kind": "strength",
            "movement_group": "push",
            "movement_pattern": "horizontal_push",
            "family_primary_muscles": ["chest"],
            "description": "Press variations",
            "family_sort_order": 10,
            "modality": "barbell",
            "primary_muscles": ["chest"],
            "equipment_required": ["barbell"],
            "unilateral": False,
        }
        first = dict(
            base,
            exercise_family_member_id=UUID(
                "eea5aad2-41d2-44bb-aa56-d3e60c188284"
            ),
            exercise_id=UUID("51b3a3ec-613c-41b4-a4ab-1273b6e1dcf6"),
            exercise_slug="barbell_bench_press",
            display_name="Barbell Bench Press",
            variant_label="barbell",
            is_default=True,
            variant_sort_order=1,
        )
        second = dict(
            base,
            exercise_family_member_id=UUID(
                "ce211aa2-cfa8-4b23-a4bc-a3e1f4c8d0b6"
            ),
            exercise_id=UUID("a5eb00c8-5a7a-4029-a38c-ff00dfba86be"),
            exercise_slug="dumbbell_bench_press",
            display_name="Dumbbell Bench Press",
            variant_label="dumbbell",
            is_default=False,
            variant_sort_order=2,
        )
        reader = FakeReader(browse_rows=[first, second])

        @asynccontextmanager
        async def context():
            yield reader

        with patch.object(catalog_routes, "lifeswitch_catalog_reader", context):
            response = await catalog_routes.browse_exercises(
                q=" Press ", movement_group=" PUSH ", kind=" Strength ", limit=100
            )

        self.assertEqual(reader.browse_arguments, ("Press", "push", "strength", 100))
        payload = json.loads(response.body)
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["exercise_family_id"], str(family_id))
        self.assertEqual(payload[0]["slug"], "bench_press")
        self.assertEqual(len(payload[0]["variants"]), 2)
        self.assertTrue(payload[0]["variants"][0]["is_default"])
        self.assertEqual(payload[0]["variants"][1]["variant_label"], "dumbbell")

    def test_queries_expose_no_mutation_surface(self):
        sql = f"{EXERCISE_SEARCH_SQL}\n{EXERCISE_BROWSE_SQL}".lower()
        for forbidden in (
            "insert into",
            "update ",
            "delete from",
            "truncate ",
            "alter table",
            "drop table",
        ):
            self.assertNotIn(forbidden, sql)


if __name__ == "__main__":
    unittest.main()
