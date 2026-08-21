from __future__ import annotations

import unittest
from types import SimpleNamespace

from seebx.adapters.lifeswitch_meals_postgres import (
    MealsRepositoryError,
    PostgresLifeSwitchMealsRepository,
    lifeswitch_meals_repository,
    resolve_nutrition_schema,
)


OWNER = "11111111-1111-4111-8111-111111111111"
MEAL = "22222222-2222-4222-8222-222222222222"
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


class LifeSwitchMealsPostgresTests(unittest.IsolatedAsyncioTestCase):
    def repository(self, connection):
        return PostgresLifeSwitchMealsRepository(
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

        async with lifeswitch_meals_repository(request, connection_factory=first_factory):
            self.assertFalse(first.closed)
        self.assertTrue(first.closed)

        second = FakeConnection()

        async def second_factory(_request):
            return second

        with self.assertRaisesRegex(RuntimeError, "boom"):
            async with lifeswitch_meals_repository(request, connection_factory=second_factory):
                raise RuntimeError("boom")
        self.assertTrue(second.closed)

    async def test_list_meals_preserves_active_filter(self) -> None:
        active = FakeConnection(rows=[[{"meal_id": MEAL}]])
        rows = await self.repository(active).list_meals(
            owner_user_id=OWNER,
            include_inactive=False,
        )
        self.assertEqual(rows, ({"meal_id": MEAL},))
        self.assertIn("owner_user_id=$1::uuid and is_active", active.fetch_calls[0][0])
        self.assertEqual(active.fetch_calls[0][1], (OWNER,))

        all_rows = FakeConnection(rows=[[]])
        await self.repository(all_rows).list_meals(
            owner_user_id=OWNER,
            include_inactive=True,
        )
        self.assertNotIn("and is_active", all_rows.fetch_calls[0][0])

    async def test_create_item_preserves_exact_arguments(self) -> None:
        row = {"meal_item_id": ITEM}
        connection = FakeConnection(row_values=[row])
        result = await self.repository(connection).create_item(
            meal_id=MEAL,
            my_food_id=FOOD,
            qty_g=125,
            serving_id=None,
            qty_servings=None,
            sort_order=2,
            notes="dinner",
        )
        self.assertEqual(result, row)
        self.assertEqual(
            connection.fetchrow_calls[0][1],
            (MEAL, FOOD, 125, None, None, 2, "dinner"),
        )

    async def test_update_keeps_authorization_inside_transaction(self) -> None:
        current = {"my_food_id": FOOD, "owner_user_id": OWNER}
        updated = {"meal_item_id": ITEM}
        connection = FakeConnection(row_values=[current, updated])

        def authorize(owner):
            self.assertEqual(owner, OWNER)
            connection.events.append("authorize")

        result = await self.repository(connection).update_item(
            meal_id=MEAL,
            meal_item_id=ITEM,
            qty_g=100,
            serving_id=None,
            qty_servings=None,
            authorize_owner=authorize,
        )
        self.assertEqual(result, updated)
        self.assertEqual(
            connection.events,
            ["transaction_enter", "fetchrow", "authorize", "fetchrow", "transaction_exit"],
        )

    async def test_update_missing_item_uses_stable_repository_error(self) -> None:
        connection = FakeConnection(row_values=[None])
        with self.assertRaises(MealsRepositoryError) as raised:
            await self.repository(connection).update_item(
                meal_id=MEAL,
                meal_item_id=ITEM,
                qty_g=100,
                serving_id=None,
                qty_servings=None,
                authorize_owner=lambda _owner: None,
            )
        self.assertEqual(raised.exception.code, "meal_item_not_found_or_inactive")
        self.assertEqual(connection.events, ["transaction_enter", "fetchrow", "transaction_exit"])

    async def test_delete_is_owner_bound_and_nontransactional(self) -> None:
        deleted = {"meal_item_id": ITEM}
        connection = FakeConnection(row_values=[deleted])
        result = await self.repository(connection).delete_item(
            meal_item_id=ITEM,
            meal_id=MEAL,
            owner_user_id=OWNER,
        )
        self.assertEqual(result, deleted)
        self.assertEqual(connection.fetchrow_calls[0][1], (ITEM, MEAL, OWNER))
        self.assertNotIn("transaction_enter", connection.events)


if __name__ == "__main__":
    unittest.main()
