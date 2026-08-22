from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

import asyncpg

from seebx.adapters.lifeswitch_foods_postgres import (
    FoodsRepositoryError,
    PostgresLifeSwitchFoodsRepository,
    lifeswitch_foods_repository,
    resolve_catalog_schema,
    resolve_nutrition_schema,
)


OWNER = "11111111-1111-4111-8111-111111111111"
FOOD = "22222222-2222-4222-8222-222222222222"
SERVING = "33333333-3333-4333-8333-333333333333"


class FakeTransaction:
    def __init__(self, connection) -> None:
        self.connection = connection

    async def __aenter__(self):
        self.connection.events.append("transaction_enter")

    async def __aexit__(self, exc_type, exc, traceback):
        self.connection.events.append("transaction_exit")


class FakeConnection:
    def __init__(self, *, rows=None, row_values=None, value_values=None) -> None:
        self.rows = list(rows or [])
        self.row_values = list(row_values or [])
        self.value_values = list(value_values or [])
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
        return self.value_values.pop(0) if self.value_values else None

    async def execute(self, query, *arguments):
        self.execute_calls.append((query, arguments))
        self.events.append("execute")
        return "UPDATE 1"

    def transaction(self):
        return FakeTransaction(self)

    async def close(self):
        self.closed = True


class LifeSwitchFoodsPostgresTests(unittest.IsolatedAsyncioTestCase):
    def repository(self, connection):
        return PostgresLifeSwitchFoodsRepository(
            connection,
            nutrition_schema="lifeswitch_nutrition",
            catalog_schema="catalog_dev",
        )

    def test_schema_identifiers_fail_closed(self) -> None:
        self.assertEqual(resolve_nutrition_schema("lifeswitch_nutrition"), "lifeswitch_nutrition")
        self.assertEqual(resolve_catalog_schema("catalog_dev"), "catalog_dev")
        with self.assertRaisesRegex(RuntimeError, "invalid LIFESWITCH_NUTRITION_SCHEMA"):
            resolve_nutrition_schema("nutrition; drop schema public")
        with self.assertRaisesRegex(RuntimeError, "invalid CATALOG_SCHEMA"):
            resolve_catalog_schema("catalog-dev")

    async def test_context_closes_on_success_and_failure(self) -> None:
        request = SimpleNamespace()
        first = FakeConnection()

        async def first_factory(_request):
            return first

        async with lifeswitch_foods_repository(request, connection_factory=first_factory):
            self.assertFalse(first.closed)
        self.assertTrue(first.closed)

        second = FakeConnection()

        async def second_factory(_request):
            return second

        with self.assertRaisesRegex(RuntimeError, "boom"):
            async with lifeswitch_foods_repository(request, connection_factory=second_factory):
                raise RuntimeError("boom")
        self.assertTrue(second.closed)

    async def test_repository_transaction_is_exactly_one_underlying_transaction(self) -> None:
        connection = FakeConnection()
        async with self.repository(connection).transaction():
            connection.events.append("body")
        self.assertEqual(connection.events, ["transaction_enter", "body", "transaction_exit"])

    async def test_serving_transaction_translates_unique_violation_only_when_requested(self) -> None:
        connection = FakeConnection()
        repository = self.repository(connection)
        with self.assertRaises(FoodsRepositoryError) as raised:
            async with repository.transaction(translate_unique_violation=True):
                raise asyncpg.UniqueViolationError("duplicate")
        self.assertEqual(raised.exception.code, "serving_name_conflict")

        with self.assertRaises(asyncpg.UniqueViolationError):
            async with repository.transaction():
                raise asyncpg.UniqueViolationError("duplicate")

    async def test_list_foods_keeps_owner_filter_and_search_arguments(self) -> None:
        connection = FakeConnection(rows=[[{"my_food_id": FOOD}]])
        rows = await self.repository(connection).list_foods(
            owner_user_id=OWNER,
            query="rice",
            include_inactive=False,
        )
        self.assertEqual(rows, ({"my_food_id": FOOD},))
        query, arguments = connection.fetch_calls[0]
        self.assertIn("f.owner_user_id = $1::uuid", query)
        self.assertIn("and f.is_active", query)
        self.assertIn("ilike $2", query)
        self.assertEqual(arguments, (OWNER, "%rice%"))

    async def test_usda_upsert_preserves_exact_argument_order(self) -> None:
        connection = FakeConnection(row_values=[{"my_food_id": FOOD}])
        row = await self.repository(connection).upsert_usda_food(
            owner_user_id=OWNER,
            display_name="Food",
            brand="Brand",
            variant="Large",
            source_id="123",
            barcode="0099",
            kcal=100,
            protein_g=10,
            carbs_g=20,
            fat_g=3,
            fiber_g=4,
            sugar_g=5,
            sodium_mg=6,
        )
        self.assertEqual(row, {"my_food_id": FOOD})
        self.assertEqual(
            connection.fetchrow_calls[0][1],
            (OWNER, "Food", "Brand", "Large", "123", "0099", 100, 10, 20, 3, 4, 5, 6),
        )

    async def test_default_and_preference_updates_remain_separate_effects(self) -> None:
        connection = FakeConnection()
        repository = self.repository(connection)
        await repository.clear_default_servings(my_food_id=FOOD)
        await repository.set_default_serving(serving_id=SERVING)
        await repository.set_preferred_serving(my_food_id=FOOD, serving_id=SERVING)
        self.assertEqual(
            [arguments for _, arguments in connection.execute_calls],
            [(FOOD,), (SERVING,), (FOOD, SERVING)],
        )

    def test_capability_has_no_direct_database_effects(self) -> None:
        root = Path(__file__).resolve().parents[1]
        path = root / "seebx/capabilities/nutrition/foods.py"
        tree = ast.parse(path.read_text())
        forbidden = {"fetch", "fetchrow", "fetchval", "execute", "close"}
        calls = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in forbidden
        ]
        self.assertEqual(calls, [])
        source = path.read_text()
        self.assertNotIn("connect_lifeswitch", source)
        self.assertNotIn("asyncpg", source)
        self.assertNotIn("insert into", source.lower())
        self.assertNotIn("update ", source.lower())
        self.assertNotIn("delete from", source.lower())


if __name__ == "__main__":
    unittest.main()
