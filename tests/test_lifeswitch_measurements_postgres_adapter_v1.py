from __future__ import annotations

import ast
import os
import unittest
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

from seebx.adapters.lifeswitch_measurements_postgres import (
    MeasurementEntryWrite,
    PostgresLifeSwitchMeasurementsRepository,
    lifeswitch_measurements_repository,
    resolve_people_schema,
)


ROOT = Path(__file__).resolve().parents[1]
OWNER = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
ENTRY = "33333333-3333-3333-3333-333333333333"


class FakeConnection:
    def __init__(self, *, fetchrows=None, fetches=None):
        self.fetchrows = list(fetchrows or [])
        self.fetches = list(fetches or [])
        self.calls = []
        self.closed = False

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        return self.fetchrows.pop(0)

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        return self.fetches.pop(0)

    async def close(self):
        self.closed = True


def sample_write() -> MeasurementEntryWrite:
    return MeasurementEntryWrite(
        owner_user_id=OWNER,
        local_date=date(2026, 8, 20),
        measured_at=datetime(2026, 8, 20, 12, tzinfo=timezone.utc),
        weight_value=205,
        weight_unit="lb",
        waist_value=34,
        abdomen_value=None,
        neck_value=16,
        chest_value=44,
        hip_value=None,
        left_arm_value=16,
        right_arm_value=16,
        left_thigh_value=24,
        right_thigh_value=24,
        left_calf_value=15,
        right_calf_value=15,
        body_fat_percent=14,
        body_fat_method="scan",
        measurement_unit="in",
        source="manual",
        entry_kind="general",
        notes="stable",
        skinfolds_json='{"chest":10}',
        scan_json=None,
    )


class MeasurementsPostgresAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_schema_identifier_is_defaulted_and_fail_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_people_schema(), "lifeswitch_people")
        self.assertEqual(resolve_people_schema("people_v2"), "people_v2")
        for value in ("People", "public.people", "people;drop schema public", ""):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "invalid LIFESWITCH_PEOPLE_SCHEMA",
                ):
                    resolve_people_schema(value)

    def test_adapter_is_the_only_measurements_database_effect_owner(self):
        methods = {"execute", "fetch", "fetchrow", "fetchval", "transaction"}

        def effects(relative: str) -> list[tuple[str, int]]:
            tree = ast.parse((ROOT / relative).read_text())
            return [
                (node.func.attr, node.lineno)
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in methods
            ]

        self.assertEqual(effects("seebx/capabilities/measurements/routes.py"), [])
        self.assertEqual(
            len(effects("seebx/adapters/lifeswitch_measurements_postgres.py")),
            4,
        )

    async def test_people_permission_uses_only_validated_schema_and_exact_binding(self):
        connection = FakeConnection(fetchrows=[{"relationship_permission_id": 1}])
        repository = PostgresLifeSwitchMeasurementsRepository(
            connection,
            people_schema="lifeswitch_people",
        )
        allowed = await repository.has_people_permission(
            grantor_user_id=OTHER,
            grantee_user_id=OWNER,
            scope="measurements:view",
        )
        self.assertTrue(allowed)
        call = connection.calls[0]
        self.assertIn("from lifeswitch_people.relationship_permission", call[1])
        self.assertEqual(call[2], (OTHER, OWNER, "measurements:view"))

    async def test_list_entries_keeps_owner_limit_and_active_filter(self):
        connection = FakeConnection(fetches=[[], []])
        repository = PostgresLifeSwitchMeasurementsRepository(
            connection,
            people_schema="lifeswitch_people",
        )
        self.assertEqual(
            await repository.list_entries(
                owner_user_id=OWNER,
                limit=25,
                include_inactive=False,
            ),
            [],
        )
        self.assertIn("and is_active=true", connection.calls[-1][1])
        self.assertEqual(connection.calls[-1][2], (OWNER, 25))
        await repository.list_entries(
            owner_user_id=OWNER,
            limit=50,
            include_inactive=True,
        )
        self.assertNotIn("and is_active=true", connection.calls[-1][1])

    async def test_create_entry_preserves_upsert_and_all_24_parameters(self):
        row = {"measurement_entry_id": uuid.UUID(ENTRY)}
        connection = FakeConnection(fetchrows=[row])
        repository = PostgresLifeSwitchMeasurementsRepository(
            connection,
            people_schema="lifeswitch_people",
        )
        result = await repository.create_entry(sample_write())
        self.assertIs(result, row)
        call = connection.calls[0]
        self.assertIn(
            "on conflict (owner_user_id, local_date, source, entry_kind)",
            call[1],
        )
        self.assertIn("where is_active=true", call[1])
        self.assertEqual(len(call[2]), 24)
        self.assertEqual(call[2][0], OWNER)
        self.assertEqual(call[2][22], '{"chest":10}')

    async def test_deactivate_is_owner_bound_and_returns_optional_row(self):
        row = {"measurement_entry_id": uuid.UUID(ENTRY), "is_active": False}
        connection = FakeConnection(fetchrows=[row, None])
        repository = PostgresLifeSwitchMeasurementsRepository(
            connection,
            people_schema="lifeswitch_people",
        )
        self.assertIs(
            await repository.deactivate_entry(
                measurement_entry_id=ENTRY,
                owner_user_id=OWNER,
            ),
            row,
        )
        call = connection.calls[-1]
        self.assertIn("owner_user_id=$2", call[1])
        self.assertEqual(call[2], (ENTRY, OWNER))
        self.assertIsNone(
            await repository.deactivate_entry(
                measurement_entry_id=ENTRY,
                owner_user_id=OWNER,
            )
        )

    async def test_context_validates_schema_before_connect_and_always_closes(self):
        request = Request({"type": "http", "headers": []})
        connect = AsyncMock()
        with patch.dict(
            os.environ,
            {"LIFESWITCH_PEOPLE_SCHEMA": "people;drop"},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "invalid LIFESWITCH_PEOPLE_SCHEMA"):
                async with lifeswitch_measurements_repository(
                    request,
                    connection_factory=connect,
                ):
                    pass
        connect.assert_not_awaited()

        connection = FakeConnection()
        connect = AsyncMock(return_value=connection)
        with patch.dict(
            os.environ,
            {"LIFESWITCH_PEOPLE_SCHEMA": "lifeswitch_people"},
            clear=False,
        ):
            async with lifeswitch_measurements_repository(
                request,
                connection_factory=connect,
            ) as repository:
                self.assertIsInstance(
                    repository,
                    PostgresLifeSwitchMeasurementsRepository,
                )
                self.assertFalse(connection.closed)
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
