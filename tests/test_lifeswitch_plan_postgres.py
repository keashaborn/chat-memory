from __future__ import annotations

import datetime as dt
import json
import unittest
import uuid
from typing import Any

from seebx.adapters.lifeswitch_plan_postgres import (
    PlanProfileWrite,
    PostgresLifeSwitchPlanRepository,
    lifeswitch_plan_repository,
    resolve_plan_schema,
)


OWNER = "11111111-1111-4111-8111-111111111111"
PROFILE = uuid.UUID("22222222-2222-4222-8222-222222222222")


class FakeTransaction:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection

    async def __aenter__(self):
        self.connection.events.append("transaction_enter")

    async def __aexit__(self, exc_type, exc, tb):
        self.connection.events.append("transaction_exit")


class FakeConnection:
    def __init__(self, profile: dict[str, Any] | None = None) -> None:
        self.profile = profile
        self.events: list[Any] = []
        self.closed = False

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)

    async def fetchrow(self, query: str, *args: Any):
        self.events.append(("fetchrow", query, args))
        if "select\n              plan_profile_id" in query:
            return self.profile
        if "insert into lifeswitch_plan.plan_profile" in query:
            return self.profile or {"plan_profile_id": PROFILE, "owner_user_id": OWNER}
        if "insert into lifeswitch_plan.plan_comment" in query:
            return {"plan_comment_id": uuid.uuid4(), "plan_profile_id": PROFILE}
        raise AssertionError(query)

    async def fetch(self, query: str, *args: Any):
        self.events.append(("fetch", query, args))
        return []

    async def execute(self, query: str, *args: Any):
        self.events.append(("execute", query, args))
        return "INSERT 0 1"

    async def close(self):
        self.closed = True


def profile_write() -> PlanProfileWrite:
    return PlanProfileWrite(
        owner_user_id=OWNER,
        phase="maintenance",
        phase_label="Maintain",
        primary_goal="Maintain",
        start_date=dt.date(2026, 8, 1),
        review_date=None,
        review_cadence="weekly",
        body_state={},
        nutrition_targets={"calories": "1800-2100", "protein_g": "180"},
        training_targets={},
        conditioning_targets={},
        activity_targets={},
        recovery_targets={},
        monitoring_rules={},
        coach_notes="",
    )


class PostgresLifeSwitchPlanRepositoryTests(unittest.IsolatedAsyncioTestCase):
    def test_schema_identifier_is_fail_closed(self) -> None:
        self.assertEqual(resolve_plan_schema("lifeswitch_plan"), "lifeswitch_plan")
        with self.assertRaises(RuntimeError):
            resolve_plan_schema("lifeswitch_plan; drop schema public")

    async def test_get_without_create_is_read_only(self) -> None:
        connection = FakeConnection()
        repository = PostgresLifeSwitchPlanRepository(
            connection, plan_schema="lifeswitch_plan"
        )
        result = await repository.get_or_create_profile(
            owner_user_id=OWNER,
            create_if_missing=False,
        )
        self.assertIsNone(result)
        self.assertEqual([event[0] for event in connection.events], ["fetchrow"])

    async def test_upsert_snapshots_existing_profile_in_one_transaction(self) -> None:
        existing = {
            "plan_profile_id": PROFILE,
            "owner_user_id": uuid.UUID(OWNER),
            "nutrition_targets": {"protein_g": "180"},
        }
        connection = FakeConnection(existing)
        repository = PostgresLifeSwitchPlanRepository(
            connection, plan_schema="lifeswitch_plan"
        )
        await repository.upsert_profile(profile_write(), snapshot_reason="manual_update")
        self.assertEqual(connection.events[0], "transaction_enter")
        self.assertEqual(connection.events[-1], "transaction_exit")
        history = next(event for event in connection.events if event[0] == "execute")
        self.assertIn("plan_profile_history", history[1])
        snapshot = json.loads(history[2][3])
        self.assertEqual(snapshot["owner_user_id"], OWNER)
        upsert = [event for event in connection.events if event[0] == "fetchrow"][-1]
        self.assertIn("on conflict (owner_user_id) do update", upsert[1])
        self.assertEqual(json.loads(upsert[2][8])["protein_g"], "180")

    async def test_context_manager_closes_owner_bound_connection(self) -> None:
        connection = FakeConnection()

        async def factory(_request):
            return connection

        async with lifeswitch_plan_repository(
            object(), connection_factory=factory
        ) as repository:
            self.assertIsInstance(repository, PostgresLifeSwitchPlanRepository)
            self.assertFalse(connection.closed)
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
