from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rag_engine.active_thread_selection_v1 import (
    ActiveThreadSelectionV1Error,
    clear_active_thread_v1,
    get_active_thread_v1,
    select_active_thread_v1,
)


OWNER = uuid.UUID("10000000-0000-4000-8000-000000000001")
THREAD = uuid.UUID("20000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 7, 26, tzinfo=timezone.utc)
THREAD_ROW = {"id": THREAD, "title": "Cross-device state", "updated_at": NOW}
ABSENT = object()


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


class FakeConnection:
    def __init__(
        self,
        *,
        selection: object = ABSENT,
        selected_thread: dict[str, Any] | None = THREAD_ROW,
        fallback_thread: dict[str, Any] | None = THREAD_ROW,
    ) -> None:
        self.selection = selection
        self.selected_thread = selected_thread
        self.fallback_thread = fallback_thread
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append((query, args))
        return "SELECT 1"

    async def fetchrow(self, query: str, *args: Any) -> Any:
        normalized = " ".join(query.split())
        if "FROM public.active_thread_selection" in normalized:
            if self.selection is ABSENT:
                return None
            return self.selection
        if "FROM public.threads" in normalized and "AND id=$2" in normalized:
            return self.selected_thread
        if "FROM public.threads" in normalized and "ORDER BY updated_at" in normalized:
            return self.fallback_thread
        raise AssertionError(f"unexpected fetchrow: {normalized}")


class ActiveThreadSelectionV1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_selection_is_returned_without_rewrite(self) -> None:
        conn = FakeConnection(selection={"thread_id": THREAD})
        result = await get_active_thread_v1(conn, owner_user_id=OWNER)
        self.assertEqual(result["thread_id"], str(THREAD))
        self.assertEqual(conn.execute_calls[0][1], (str(OWNER),))
        self.assertNotIn(
            "INSERT INTO public.active_thread_selection",
            "\n".join(query for query, _ in conn.execute_calls),
        )

    async def test_explicit_clear_does_not_fallback(self) -> None:
        conn = FakeConnection(selection={"thread_id": None})
        result = await get_active_thread_v1(conn, owner_user_id=OWNER)
        self.assertIsNone(result)
        self.assertNotIn(
            "INSERT INTO public.active_thread_selection",
            "\n".join(query for query, _ in conn.execute_calls),
        )

    async def test_absent_selection_migrates_to_latest_visible_thread(self) -> None:
        conn = FakeConnection()
        result = await get_active_thread_v1(conn, owner_user_id=OWNER)
        self.assertEqual(result["thread_id"], str(THREAD))
        write = next(
            args
            for query, args in conn.execute_calls
            if "INSERT INTO public.active_thread_selection" in query
        )
        self.assertEqual(write, (OWNER, THREAD))

    async def test_stale_selection_repairs_to_latest_visible_thread(self) -> None:
        conn = FakeConnection(
            selection={"thread_id": uuid.UUID(int=9)},
            selected_thread=None,
        )
        result = await get_active_thread_v1(conn, owner_user_id=OWNER)
        self.assertEqual(result["thread_id"], str(THREAD))

    async def test_select_rejects_non_visible_thread(self) -> None:
        conn = FakeConnection(selected_thread=None)
        with self.assertRaises(ActiveThreadSelectionV1Error) as raised:
            await select_active_thread_v1(
                conn,
                owner_user_id=OWNER,
                thread_id=THREAD,
            )
        self.assertEqual(raised.exception.code, "thread_not_found")

    async def test_clear_writes_explicit_null(self) -> None:
        conn = FakeConnection()
        await clear_active_thread_v1(conn, owner_user_id=OWNER)
        write = next(
            args
            for query, args in conn.execute_calls
            if "INSERT INTO public.active_thread_selection" in query
        )
        self.assertEqual(write, (OWNER, None))


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
MIGRATION = (
    ROOT / "ops/sql/20260726_active_thread_selection_v1.sql"
).read_text(encoding="utf-8")


class ActiveThreadSelectionContractTests(unittest.TestCase):
    def test_api_requires_actor_owner_verification(self) -> None:
        self.assertIn('@app.get("/threads/active/{user_id}")', APP_SOURCE)
        self.assertIn('@app.post("/threads/active")', APP_SOURCE)
        self.assertIn('@app.delete("/threads/active/{user_id}")', APP_SOURCE)
        active_handlers = APP_SOURCE.split(
            '@app.get("/threads/active/{user_id}")',
            1,
        )[1].split('@app.get("/threads/{thread_id}/messages")', 1)[0]
        self.assertEqual(
            active_handlers.count("_require_actor_for_user("),
            3,
        )
        self.assertIn('"/threads/active"', APP_SOURCE)

    def test_new_thread_selects_itself_in_same_transaction(self) -> None:
        handler = APP_SOURCE.split('@app.post("/threads/new")', 1)[1]
        handler = handler.split('@app.get("/threads/list/{user_id}")', 1)[0]
        self.assertIn("async with conn.transaction():", handler)
        self.assertIn("select_active_thread_v1(", handler)

    def test_migration_is_owner_scoped_and_force_rls(self) -> None:
        self.assertIn(
            "FOREIGN KEY (owner_user_id, thread_id)",
            MIGRATION,
        )
        self.assertIn("REFERENCES public.threads(owner_user_id, id)", MIGRATION)
        self.assertIn("ENABLE ROW LEVEL SECURITY", MIGRATION)
        self.assertIn("FORCE ROW LEVEL SECURITY", MIGRATION)
        self.assertIn("active_thread_selection_state_ck", MIGRATION)
        self.assertIn(
            "active_thread_selection_owner_thread_fk", MIGRATION
        )
        self.assertIn(
            "owner_user_id = memory.current_actor_user_id()",
            MIGRATION,
        )
        self.assertIn(
            "REVOKE ALL ON public.active_thread_selection FROM PUBLIC, brains_app",
            MIGRATION,
        )
