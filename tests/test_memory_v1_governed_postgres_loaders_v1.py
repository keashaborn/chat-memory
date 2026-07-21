from __future__ import annotations

import unittest
from typing import Any
from uuid import UUID

from rag_engine.memory_v1_governed_postgres_loaders_v1 import (
    GovernedPostgresLoaderError,
    GovernedV5ProjectRowLoaderV1,
    load_governed_preference_snapshot_v1,
    load_governed_v5_claim_rows_v1,
)


OWNER = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
OTHER = UUID("557ea042-cb82-48f8-9429-472e96c957ef")
THREAD = UUID("d776c8ef-7f3d-45b2-8820-4be87b7ca19d")
CLAIM = UUID("00000000-0000-4000-8000-000000000001")


class FakeTransaction:
    def __init__(self, conn: "FakeConn", kwargs: dict[str, Any]) -> None:
        self.conn = conn
        self.kwargs = kwargs

    async def __aenter__(self) -> None:
        self.conn.transaction_entries.append(self.kwargs)

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


class FakeConn:
    def __init__(
        self,
        *,
        claim_rows: list[dict[str, Any]] | None = None,
        project_rows: list[dict[str, Any]] | None = None,
        preference_rows: list[dict[str, Any]] | None = None,
        owns_thread: bool = True,
        role: str = "brains_app",
        read_only: str = "on",
    ) -> None:
        self.claim_rows = claim_rows or []
        self.project_rows = project_rows or []
        self.preference_rows = preference_rows or []
        self.owns_thread = owns_thread
        self.role = role
        self.read_only = read_only
        self.transaction_entries: list[dict[str, Any]] = []
        self.executions: list[tuple[str, tuple[Any, ...]]] = []
        self.fetches: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self, **kwargs: Any) -> FakeTransaction:
        return FakeTransaction(self, kwargs)

    async def execute(self, query: str, *args: Any) -> str:
        self.executions.append((query, args))
        return "SELECT 1"

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "current_user" in query:
            return self.role
        if "transaction_read_only" in query:
            return self.read_only
        if "SELECT EXISTS" in query:
            return self.owns_thread
        raise AssertionError(f"unexpected fetchval: {query}")

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        if "FROM pg_proc" in query:
            return {
                "prosecdef": True,
                "provolatile": "s",
                "owner_name": "memory_v5_reader",
                "settings": "search_path=",
            }
        raise AssertionError(f"unexpected fetchrow: {query}")

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetches.append((query, args))
        if "read_v5_shadow_claims" in query:
            return self.claim_rows
        if "read_v5_shadow_project_knowledge" in query:
            return self.project_rows
        if "pg_class" in query:
            return [
                {
                    "relname": name,
                    "relrowsecurity": True,
                    "relforcerowsecurity": True,
                }
                for name in args[0]
            ]
        if "memory.user_preference" in query:
            return self.preference_rows
        raise AssertionError(f"unexpected fetch: {query}")


def claim_row(*, owner: UUID = OWNER) -> dict[str, Any]:
    return {
        "owner_user_id": owner,
        "claim_id": CLAIM,
        "metadata": {"memory_contract": "memory_projection_v5"},
        "retrieval_policy": {"surface_policy": "direct_or_relevant"},
        "evidence_by_stance": {
            "context": [],
            "opposes": [],
            "qualifies": [],
            "supports": [],
        },
    }


def project_row(*, owner: UUID = OWNER) -> dict[str, Any]:
    return {
        "owner_user_id": owner,
        "thread_id": THREAD,
        "project_key": "verbal-sage",
        "component_key": "memory-v1",
        "knowledge_id": UUID("00000000-0000-4000-8000-000000000005"),
    }


class GovernedPostgresLoadersV1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_claim_loader_proves_read_only_controls_and_preserves_source_shape(self) -> None:
        conn = FakeConn(claim_rows=[claim_row()])

        batch = await load_governed_v5_claim_rows_v1(conn, OWNER, [CLAIM])

        self.assertEqual(batch["owner_user_id"], OWNER)
        self.assertEqual(batch["claim_ids"], (CLAIM,))
        self.assertEqual(batch["database_writes"], 0)
        self.assertTrue(batch["controls"]["restricted_read_contract"])
        self.assertEqual(
            conn.transaction_entries,
            [{"isolation": "repeatable_read", "readonly": True}],
        )
        self.assertNotIn("revision_id", batch["records"][0])

    async def test_claim_loader_rejects_cross_owner_rows(self) -> None:
        with self.assertRaises(GovernedPostgresLoaderError):
            await load_governed_v5_claim_rows_v1(
                FakeConn(claim_rows=[claim_row(owner=OTHER)]),
                OWNER,
                [CLAIM],
            )

    async def test_project_loader_binds_owner_thread_and_project_scope(self) -> None:
        conn = FakeConn(project_rows=[project_row()])
        loader = GovernedV5ProjectRowLoaderV1(
            conn,
            project_key="verbal-sage",
            component_key="memory-v1",
        )

        batch = await loader(OWNER, THREAD, limit=8)

        self.assertEqual(batch["thread_id"], THREAD)
        self.assertEqual(batch["project_key"], "verbal-sage")
        self.assertEqual(batch["component_key"], "memory-v1")
        self.assertEqual(batch["database_writes"], 0)

    async def test_project_loader_rejects_absent_owner_thread(self) -> None:
        loader = GovernedV5ProjectRowLoaderV1(
            FakeConn(owns_thread=False),
            project_key="verbal-sage",
            component_key="memory-v1",
        )
        with self.assertRaises(GovernedPostgresLoaderError):
            await loader(OWNER, THREAD, limit=4)

    async def test_preference_loader_requires_forced_rls_and_returns_no_prompt(self) -> None:
        preference_id = UUID("00000000-0000-4000-8000-000000000003")
        conn = FakeConn(preference_rows=[{"preference_id": preference_id}])

        batch = await load_governed_preference_snapshot_v1(conn, OWNER)

        self.assertTrue(batch["controls"]["forced_rls"])
        self.assertEqual(batch["database_writes"], 0)
        self.assertNotIn("prompt_block", batch)
        self.assertNotIn("memory_chunks", batch)

    async def test_wrong_effective_role_fails_closed(self) -> None:
        with self.assertRaises(GovernedPostgresLoaderError):
            await load_governed_v5_claim_rows_v1(
                FakeConn(role="postgres", claim_rows=[claim_row()]),
                OWNER,
                [CLAIM],
            )


if __name__ == "__main__":
    unittest.main()
