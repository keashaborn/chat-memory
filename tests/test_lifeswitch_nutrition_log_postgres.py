from __future__ import annotations

import datetime as dt
import json
import os
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from seebx.adapters.lifeswitch_nutrition_log_postgres import (
    NutritionLogDayRows,
    NutritionLogRangeRows,
    PostgresLifeSwitchNutritionLogReadRepository,
    lifeswitch_nutrition_log_read_repository,
    resolve_nutrition_schema,
    resolve_people_schema,
)
from seebx.capabilities.nutrition import logs as nutrition_logs


OWNER = "11111111-1111-4111-8111-111111111111"
VIEWER = "22222222-2222-4222-8222-222222222222"


class FakeConnection:
    def __init__(self, *, rows=None, row=None) -> None:
        self.rows = list(rows or [])
        self.row = row
        self.fetch_calls = []
        self.fetchrow_calls = []
        self.closed = False

    async def fetch(self, query, *arguments):
        self.fetch_calls.append((query, arguments))
        return self.rows.pop(0) if self.rows else []

    async def fetchrow(self, query, *arguments):
        self.fetchrow_calls.append((query, arguments))
        return self.row

    async def close(self):
        self.closed = True


class NutritionLogPostgresAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_schema_names_are_fail_closed(self) -> None:
        self.assertEqual(resolve_nutrition_schema("lifeswitch_nutrition"), "lifeswitch_nutrition")
        self.assertEqual(resolve_people_schema("lifeswitch_people"), "lifeswitch_people")
        with self.assertRaisesRegex(RuntimeError, "invalid LIFESWITCH_NUTRITION_SCHEMA"):
            resolve_nutrition_schema("lifeswitch_nutrition; drop schema public")
        with self.assertRaisesRegex(RuntimeError, "invalid LIFESWITCH_PEOPLE_SCHEMA"):
            resolve_people_schema("People")

    async def test_context_owns_connection_close_on_success_and_failure(self) -> None:
        request = SimpleNamespace()
        first = FakeConnection()

        async def first_factory(_request):
            return first

        async with lifeswitch_nutrition_log_read_repository(
            request,
            connection_factory=first_factory,
        ) as repository:
            self.assertIsInstance(repository, PostgresLifeSwitchNutritionLogReadRepository)
            self.assertFalse(first.closed)
        self.assertTrue(first.closed)

        second = FakeConnection()

        async def second_factory(_request):
            return second

        with self.assertRaisesRegex(RuntimeError, "boom"):
            async with lifeswitch_nutrition_log_read_repository(
                request,
                connection_factory=second_factory,
            ):
                raise RuntimeError("boom")
        self.assertTrue(second.closed)

    async def test_permission_lookup_is_owner_and_scope_bound(self) -> None:
        connection = FakeConnection(row={"relationship_permission_id": "ok"})
        repository = PostgresLifeSwitchNutritionLogReadRepository(
            connection,
            nutrition_schema="lifeswitch_nutrition",
            people_schema="lifeswitch_people",
        )
        allowed = await repository.has_people_permission(
            grantor_user_id=OWNER,
            grantee_user_id=VIEWER,
            scope="nutrition:view",
        )
        self.assertTrue(allowed)
        query, arguments = connection.fetchrow_calls[0]
        self.assertIn("from lifeswitch_people.relationship_permission", query)
        self.assertIn("r.status='accepted'", query)
        self.assertEqual(arguments, (OWNER, VIEWER, "nutrition:view"))

    async def test_range_read_uses_one_owner_and_exact_date_window(self) -> None:
        start = dt.date(2026, 8, 1)
        end = dt.date(2026, 8, 21)
        day_rows = [{"nutrition_day_id": "day"}]
        entry_rows = [{"nutrition_entry_id": "entry"}]
        connection = FakeConnection(rows=[day_rows, entry_rows])
        repository = PostgresLifeSwitchNutritionLogReadRepository(
            connection,
            nutrition_schema="lifeswitch_nutrition",
            people_schema="lifeswitch_people",
        )
        snapshot = await repository.read_range(
            owner_user_id=OWNER,
            start_day=start,
            end_day=end,
        )
        self.assertEqual(snapshot.day_rows, tuple(day_rows))
        self.assertEqual(snapshot.entry_rows, tuple(entry_rows))
        self.assertEqual(len(connection.fetch_calls), 2)
        for query, arguments in connection.fetch_calls:
            self.assertIn("owner_user_id=$1::uuid", query)
            self.assertEqual(arguments, (OWNER, start, end))
        self.assertIn("order by day desc", connection.fetch_calls[0][0])
        self.assertIn("order by nd.day desc, e.sort_order, e.created_at", connection.fetch_calls[1][0])

    async def test_missing_day_does_not_read_entries(self) -> None:
        connection = FakeConnection(row=None)
        repository = PostgresLifeSwitchNutritionLogReadRepository(
            connection,
            nutrition_schema="lifeswitch_nutrition",
            people_schema="lifeswitch_people",
        )
        snapshot = await repository.read_day(
            owner_user_id=OWNER,
            day=dt.date(2026, 8, 21),
        )
        self.assertIsNone(snapshot.day_row)
        self.assertEqual(snapshot.entry_rows, ())
        self.assertEqual(connection.fetch_calls, [])

    async def test_day_entries_are_bound_to_selected_day_id(self) -> None:
        day_row = {"nutrition_day_id": "33333333-3333-4333-8333-333333333333"}
        entries = [{"nutrition_entry_id": "entry"}]
        connection = FakeConnection(rows=[entries], row=day_row)
        repository = PostgresLifeSwitchNutritionLogReadRepository(
            connection,
            nutrition_schema="lifeswitch_nutrition",
            people_schema="lifeswitch_people",
        )
        snapshot = await repository.read_day(
            owner_user_id=OWNER,
            day=dt.date(2026, 8, 21),
        )
        self.assertEqual(snapshot.day_row, day_row)
        self.assertEqual(snapshot.entry_rows, tuple(entries))
        query, arguments = connection.fetch_calls[0]
        self.assertIn("where e.nutrition_day_id = $1::uuid", query)
        self.assertEqual(arguments, (day_row["nutrition_day_id"],))


class FakeReadRepository:
    def __init__(self, *, allowed=True, day=None, entries=(), days=()) -> None:
        self.allowed = allowed
        self.day = day
        self.entries = tuple(entries)
        self.days = tuple(days)
        self.permission_calls = []
        self.day_calls = []
        self.range_calls = []

    async def has_people_permission(self, **values):
        self.permission_calls.append(values)
        return self.allowed

    async def read_day(self, **values):
        self.day_calls.append(values)
        return NutritionLogDayRows(self.day, self.entries)

    async def read_range(self, **values):
        self.range_calls.append(values)
        return NutritionLogRangeRows(self.days, self.entries)


def repository_context(repository):
    @asynccontextmanager
    async def context(_request):
        yield repository

    return context


class NutritionLogReadRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_day_response_keeps_existing_shape(self) -> None:
        day = dt.date(2026, 8, 21)
        repository = FakeReadRepository(
            day={
                "nutrition_day_id": "33333333-3333-4333-8333-333333333333",
                "owner_user_id": OWNER,
                "day": day,
            },
            entries=({
                "nutrition_entry_id": "entry",
                "meal_id": None,
                "qty_g": 100,
            },),
        )
        with (
            patch.object(nutrition_logs, "require_actor_matches_owner", return_value=OWNER),
            patch.object(
                nutrition_logs,
                "lifeswitch_nutrition_log_read_repository",
                repository_context(repository),
            ),
        ):
            response = await nutrition_logs.get_log_day(
                SimpleNamespace(),
                OWNER,
                day.isoformat(),
                "",
            )

        payload = json.loads(response.body)
        self.assertEqual(payload["day"]["day"], day.isoformat())
        self.assertEqual(payload["entries"][0]["nutrition_entry_id"], "entry")
        self.assertEqual(payload["_target_user_id"], OWNER)
        self.assertFalse(payload["_delegated_view"])
        self.assertEqual(
            repository.day_calls,
            [{"owner_user_id": OWNER, "day": day}],
        )

    async def test_range_response_keeps_totals_and_optional_entries(self) -> None:
        day = dt.date(2026, 8, 21)
        entry = {
            "nutrition_day_date": day,
            "nutrition_entry_id": "entry",
            "meal_id": None,
            "qty_g": 100,
            "food_kcal_100g": 200,
            "food_protein_100g": 20,
            "food_carbs_100g": 10,
            "food_fat_100g": 5,
        }
        repository = FakeReadRepository(
            days=({"nutrition_day_id": "day", "owner_user_id": OWNER, "day": day},),
            entries=(entry,),
        )
        with (
            patch.object(nutrition_logs, "require_actor_matches_owner", return_value=OWNER),
            patch.object(
                nutrition_logs,
                "lifeswitch_nutrition_log_read_repository",
                repository_context(repository),
            ),
        ):
            response = await nutrition_logs.get_log_range(
                SimpleNamespace(),
                OWNER,
                day.isoformat(),
                day.isoformat(),
                1,
                "",
            )

        payload = json.loads(response.body)
        self.assertEqual(payload["days"][0]["totals"]["kcal"], 200.0)
        self.assertEqual(payload["days"][0]["totals"]["protein_g"], 20.0)
        self.assertEqual(payload["days"][0]["entries"][0]["nutrition_entry_id"], "entry")
        self.assertNotIn("nutrition_day_date", payload["days"][0]["entries"][0])

    async def test_delegated_read_fails_before_projection_when_disabled(self) -> None:
        repository = FakeReadRepository()
        with (
            patch.object(nutrition_logs, "require_actor_matches_owner", return_value=VIEWER),
            patch.object(
                nutrition_logs,
                "lifeswitch_nutrition_log_read_repository",
                repository_context(repository),
            ),
            patch.dict(os.environ, {"LIFESWITCH_DELEGATED_READS_ENABLED": "0"}),
        ):
            with self.assertRaises(HTTPException) as raised:
                await nutrition_logs.get_log_day(
                    SimpleNamespace(),
                    VIEWER,
                    "2026-08-21",
                    OWNER,
                )

        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(raised.exception.detail, "delegated_access_disabled")
        self.assertEqual(repository.permission_calls, [])
        self.assertEqual(repository.day_calls, [])

    async def test_delegated_read_uses_exact_permission_tuple(self) -> None:
        repository = FakeReadRepository(allowed=False)
        with (
            patch.object(nutrition_logs, "require_actor_matches_owner", return_value=VIEWER),
            patch.object(
                nutrition_logs,
                "lifeswitch_nutrition_log_read_repository",
                repository_context(repository),
            ),
            patch.dict(os.environ, {"LIFESWITCH_DELEGATED_READS_ENABLED": "1"}),
        ):
            with self.assertRaises(HTTPException) as raised:
                await nutrition_logs.get_log_day(
                    SimpleNamespace(),
                    VIEWER,
                    "2026-08-21",
                    OWNER,
                )

        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(raised.exception.detail, "nutrition:view permission required")
        self.assertEqual(
            repository.permission_calls,
            [{
                "grantor_user_id": OWNER,
                "grantee_user_id": VIEWER,
                "scope": "nutrition:view",
            }],
        )
        self.assertEqual(repository.day_calls, [])


if __name__ == "__main__":
    unittest.main()
