from __future__ import annotations

import unittest
from types import SimpleNamespace

from seebx.adapters.lifeswitch_meal_plans_postgres import (
    MealPlansRepositoryError,
    PostgresLifeSwitchMealPlansRepository,
    lifeswitch_meal_plans_repository,
    resolve_nutrition_schema,
)


OWNER = "11111111-1111-4111-8111-111111111111"
PLAN = "22222222-2222-4222-8222-222222222222"
ITEM = "33333333-3333-4333-8333-333333333333"
FOOD = "44444444-4444-4444-8444-444444444444"
SERVING = "55555555-5555-4555-8555-555555555555"


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

    def transaction(self):
        return FakeTransaction(self)

    async def close(self):
        self.closed = True


class LifeSwitchMealPlansPostgresTests(unittest.IsolatedAsyncioTestCase):
    def repository(self, connection):
        return PostgresLifeSwitchMealPlansRepository(
            connection,
            nutrition_schema="lifeswitch_nutrition",
        )

    def test_schema_identifier_is_fail_closed(self) -> None:
        self.assertEqual(resolve_nutrition_schema("lifeswitch_nutrition"), "lifeswitch_nutrition")
        with self.assertRaisesRegex(RuntimeError, "invalid LIFESWITCH_NUTRITION_SCHEMA"):
            resolve_nutrition_schema("lifeswitch_nutrition; drop schema public")

    async def test_context_closes_on_success_and_failure(self) -> None:
        request = SimpleNamespace()
        first = FakeConnection()

        async def first_factory(_request):
            return first

        async with lifeswitch_meal_plans_repository(request, connection_factory=first_factory):
            self.assertFalse(first.closed)
        self.assertTrue(first.closed)

        second = FakeConnection()

        async def second_factory(_request):
            return second

        with self.assertRaisesRegex(RuntimeError, "boom"):
            async with lifeswitch_meal_plans_repository(request, connection_factory=second_factory):
                raise RuntimeError("boom")
        self.assertTrue(second.closed)

    async def test_list_plans_is_owner_bound(self) -> None:
        connection = FakeConnection(rows=[[{"meal_plan_id": PLAN}]])
        rows = await self.repository(connection).list_plans(owner_user_id=OWNER)
        self.assertEqual(rows, ({"meal_plan_id": PLAN},))
        self.assertIn("where owner_user_id = $1::uuid", connection.fetch_calls[0][0])
        self.assertEqual(connection.fetch_calls[0][1], (OWNER,))

    async def test_create_item_preserves_exact_arguments(self) -> None:
        row = {"meal_plan_item_id": ITEM}
        connection = FakeConnection(row_values=[row])
        result = await self.repository(connection).create_item(
            meal_plan_id=PLAN,
            meal_label="dinner",
            sort_order=2,
            my_food_id=FOOD,
            food_id=None,
            qty_g=125,
            serving_id=None,
            qty_servings=None,
            notes="test",
        )
        self.assertEqual(result, row)
        self.assertEqual(
            connection.fetchrow_calls[0][1],
            (PLAN, "dinner", 2, FOOD, None, 125, None, None, "test"),
        )

    async def test_update_keeps_authority_and_identifier_parse_inside_transaction(self) -> None:
        current = {"my_food_id": FOOD, "food_id": None, "owner_user_id": OWNER}
        updated = {"meal_plan_item_id": ITEM}
        connection = FakeConnection(row_values=[current, updated], value_values=[1])

        def authorize(item):
            self.assertEqual(item["owner_user_id"], OWNER)
            connection.events.append("authorize")

        def parse(value):
            self.assertEqual(value, SERVING)
            connection.events.append("parse")
            return value

        result = await self.repository(connection).update_item(
            meal_plan_id=PLAN,
            item_id=ITEM,
            meal_label="dinner",
            qty_g=None,
            raw_serving_id=SERVING,
            qty_servings=2,
            use_grams=False,
            use_serving=True,
            authorize_item=authorize,
            parse_serving_id=parse,
        )
        self.assertEqual(result, updated)
        self.assertEqual(
            connection.events,
            ["transaction_enter", "fetchrow", "authorize", "parse", "fetchval", "fetchrow", "transaction_exit"],
        )

    async def test_update_missing_item_uses_stable_repository_error(self) -> None:
        connection = FakeConnection(row_values=[None])
        with self.assertRaises(MealPlansRepositoryError) as raised:
            await self.repository(connection).update_item(
                meal_plan_id=PLAN,
                item_id=ITEM,
                meal_label="dinner",
                qty_g=100,
                raw_serving_id=None,
                qty_servings=None,
                use_grams=True,
                use_serving=False,
                authorize_item=lambda _item: None,
                parse_serving_id=lambda value: value,
            )
        self.assertEqual(raised.exception.code, "item_not_found")
        self.assertEqual(connection.events, ["transaction_enter", "fetchrow", "transaction_exit"])

    async def test_catalog_approval_is_exactly_public_and_active(self) -> None:
        connection = FakeConnection(value_values=[True])
        approved = await self.repository(connection).catalog_food_is_approved(food_id=FOOD)
        self.assertTrue(approved)
        self.assertIn("select (is_public and is_active)", connection.fetchval_calls[0][0])
        self.assertEqual(connection.fetchval_calls[0][1], (FOOD,))

    async def test_delete_is_plan_bound_and_nontransactional(self) -> None:
        deleted = {"meal_plan_item_id": ITEM}
        connection = FakeConnection(row_values=[deleted])
        result = await self.repository(connection).delete_item(meal_plan_id=PLAN, item_id=ITEM)
        self.assertEqual(result, deleted)
        self.assertEqual(connection.fetchrow_calls[0][1], (PLAN, ITEM))
        self.assertNotIn("transaction_enter", connection.events)


if __name__ == "__main__":
    unittest.main()
