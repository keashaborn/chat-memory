from __future__ import annotations

import datetime as dt
import importlib
import os
import unittest
import uuid
from pathlib import Path
from typing import Any

os.environ.setdefault("POSTGRES_DSN", "postgresql://unused")

router_module = importlib.import_module("rag_engine.lifeswitch_nutrition_log_router")


class FakeConnection:
    def __init__(self, *, completed: bool) -> None:
        self.owner = str(uuid.uuid4())
        self.day_id = uuid.uuid4()
        self.day = dt.date(2026, 7, 20)
        self.completed_at = (
            dt.datetime(2026, 7, 20, 21, 0, tzinfo=dt.timezone.utc)
            if completed
            else None
        )
        self.events: list[tuple[Any, ...]] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []

    def row(self) -> dict[str, Any]:
        now = dt.datetime(2026, 7, 20, 21, 0, tzinfo=dt.timezone.utc)
        return {
            "nutrition_day_id": self.day_id,
            "owner_user_id": uuid.UUID(self.owner),
            "day": self.day,
            "notes": None,
            "completed_at": self.completed_at,
            "created_at": now,
            "updated_at": now,
        }

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrow_calls.append((query, args))
        if args[:2] not in (
            (self.owner, self.day),
            (self.day_id, self.owner),
        ):
            return None
        if query.lstrip().lower().startswith("update"):
            self.completed_at = (
                dt.datetime(2026, 7, 20, 21, 5, tzinfo=dt.timezone.utc)
                if bool(args[2])
                else None
            )
        return self.row()

    async def execute(self, query: str, *args: Any) -> str:
        self.events.append(args)
        return "INSERT 0 1"


class NutritionDayCompletionTest(unittest.IsolatedAsyncioTestCase):
    async def test_complete_is_owner_scoped_and_audited(self) -> None:
        conn = FakeConnection(completed=False)
        result = await router_module._set_nutrition_day_completion(
            conn,
            owner_user_id=conn.owner,
            day=conn.day,
            completed=True,
        )

        self.assertTrue(result["changed"])
        self.assertIsNotNone(result["day"]["completed_at"])
        self.assertEqual(conn.fetchrow_calls[0][1], (conn.owner, conn.day))
        self.assertIn("owner_user_id=$1::uuid", conn.fetchrow_calls[0][0])
        self.assertEqual(conn.events[0][1:], (conn.owner, "completed"))

    async def test_reopen_is_idempotent(self) -> None:
        conn = FakeConnection(completed=False)
        result = await router_module._set_nutrition_day_completion(
            conn,
            owner_user_id=conn.owner,
            day=conn.day,
            completed=False,
        )

        self.assertFalse(result["changed"])
        self.assertEqual(len(conn.fetchrow_calls), 1)
        self.assertEqual(conn.events, [])

    def test_migration_reopens_after_entry_changes_and_keeps_audit_append_only(self) -> None:
        sql = (
            Path(__file__).resolve().parents[1]
            / "ops/sql/20260720_lifeswitch_nutrition_day_completion.sql"
        ).read_text(encoding="utf-8")
        self.assertIn("AFTER INSERT OR UPDATE OR DELETE", sql)
        self.assertIn("reopened_after_entry_change", sql)
        self.assertIn("BEFORE UPDATE OR DELETE", sql)
        self.assertIn("completion events are append-only", sql)


if __name__ == "__main__":
    unittest.main()
