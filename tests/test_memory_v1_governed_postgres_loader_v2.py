from __future__ import annotations

import unittest
from typing import Any
from uuid import UUID

from rag_engine.memory_v1_governed_postgres_loaders_v1 import (
    GovernedPostgresLoaderError,
    load_governed_entity_scope_snapshot_v2,
    load_governed_v5_claim_rows_v2,
)
from tests.test_memory_v1_governed_postgres_loaders_v1 import (
    CLAIM,
    OTHER,
    OWNER,
    FakeConn,
    claim_row,
)


SUBJECT = UUID("00000000-0000-4000-8000-000000000701")
OBJECT = UUID("00000000-0000-4000-8000-000000000702")


class V2FakeConn(FakeConn):
    def __init__(
        self,
        *,
        entity_rows: list[dict[str, Any]] | None = None,
        edge_rows: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.entity_rows = entity_rows or []
        self.edge_rows = edge_rows or []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetches.append((query, args))
        if "read_governed_entity_scope_entities_v1" in query:
            return self.entity_rows
        if "read_governed_entity_scope_edges_v1" in query:
            return self.edge_rows
        if "read_governed_claims_v2" in query:
            return self.claim_rows
        return await super().fetch(query, *args)


def v2_claim_row(*, owner: UUID = OWNER) -> dict[str, Any]:
    value = claim_row(owner=owner)
    value.update(
        {
            "subject_entity_id": SUBJECT,
            "subject_entity_type": "self",
            "object_entity_id": OBJECT,
            "object_entity_type": "animal",
        }
    )
    return value


def entity_row(
    entity_id: UUID,
    entity_type: str,
    name: str,
    *,
    owner: UUID = OWNER,
    identity_state: str = "named",
) -> dict[str, Any]:
    return {
        "owner_user_id": owner,
        "entity_id": entity_id,
        "entity_type": entity_type,
        "canonical_name": name,
        "normalized_name": name.casefold(),
        "identity_state": identity_state,
        "relationship_role": None,
        "normalized_aliases": [],
    }


def edge_row(*, owner: UUID = OWNER) -> dict[str, Any]:
    return {
        "owner_user_id": owner,
        "claim_id": CLAIM,
        "predicate": "relationship.has_pet",
        "subject_entity_id": SUBJECT,
        "subject_entity_type": "self",
        "object_entity_id": OBJECT,
        "object_entity_type": "animal",
    }


class GovernedPostgresLoaderV2Test(unittest.IsolatedAsyncioTestCase):
    async def test_entity_scope_loader_builds_hash_bound_snapshot(self) -> None:
        conn = V2FakeConn(
            entity_rows=[
                entity_row(
                    SUBJECT,
                    "self",
                    "Self",
                    identity_state="trusted_owner_self",
                ),
                entity_row(OBJECT, "animal", "Dahlia"),
            ],
            edge_rows=[edge_row()],
        )

        batch = await load_governed_entity_scope_snapshot_v2(conn, OWNER)

        self.assertEqual(batch["owner_user_id"], OWNER)
        self.assertEqual(len(batch["snapshot"].entities), 2)
        self.assertEqual(len(batch["snapshot"].edges), 1)
        self.assertEqual(batch["database_writes"], 0)
        self.assertTrue(
            batch["controls"]["restricted_entity_scope_contract"]
        )
        self.assertEqual(
            conn.transaction_entries,
            [{"isolation": "repeatable_read", "readonly": True}],
        )

    async def test_entity_scope_loader_rejects_cross_owner_entity(self) -> None:
        conn = V2FakeConn(
            entity_rows=[
                entity_row(
                    SUBJECT,
                    "self",
                    "Self",
                    owner=OTHER,
                    identity_state="trusted_owner_self",
                )
            ],
        )
        with self.assertRaisesRegex(GovernedPostgresLoaderError, "cross-owner"):
            await load_governed_entity_scope_snapshot_v2(conn, OWNER)

    async def test_entity_scope_loader_rejects_missing_edge_endpoint(self) -> None:
        conn = V2FakeConn(
            entity_rows=[
                entity_row(
                    SUBJECT,
                    "self",
                    "Self",
                    identity_state="trusted_owner_self",
                )
            ],
            edge_rows=[edge_row()],
        )
        with self.assertRaisesRegex(
            GovernedPostgresLoaderError,
            "failed reconciliation",
        ):
            await load_governed_entity_scope_snapshot_v2(conn, OWNER)

    async def test_v2_loader_preserves_typed_entity_endpoints(self) -> None:
        conn = V2FakeConn(claim_rows=[v2_claim_row()])
        batch = await load_governed_v5_claim_rows_v2(conn, OWNER, [CLAIM])
        row = batch["records"][0]
        self.assertEqual(row["subject_entity_id"], SUBJECT)
        self.assertEqual(row["subject_entity_type"], "self")
        self.assertEqual(row["object_entity_id"], OBJECT)
        self.assertEqual(row["object_entity_type"], "animal")
        self.assertTrue(batch["controls"]["restricted_read_contract"])

    async def test_v2_loader_rejects_cross_owner_rows(self) -> None:
        with self.assertRaisesRegex(GovernedPostgresLoaderError, "cross-owner"):
            await load_governed_v5_claim_rows_v2(
                V2FakeConn(claim_rows=[v2_claim_row(owner=OTHER)]),
                OWNER,
                [CLAIM],
            )

    async def test_v2_loader_rejects_missing_subject(self) -> None:
        row = v2_claim_row()
        row["subject_entity_id"] = None
        with self.assertRaisesRegex(GovernedPostgresLoaderError, "subject_entity_id"):
            await load_governed_v5_claim_rows_v2(
                V2FakeConn(claim_rows=[row]), OWNER, [CLAIM]
            )

    async def test_v2_loader_rejects_incoherent_literal_endpoint(self) -> None:
        row = v2_claim_row()
        row["object_entity_id"] = None
        with self.assertRaisesRegex(GovernedPostgresLoaderError, "literal claim"):
            await load_governed_v5_claim_rows_v2(
                V2FakeConn(claim_rows=[row]), OWNER, [CLAIM]
            )

    async def test_v2_loader_rejects_unknown_entity_type(self) -> None:
        row = v2_claim_row()
        row["subject_entity_type"] = "account"
        with self.assertRaisesRegex(GovernedPostgresLoaderError, "closed enum"):
            await load_governed_v5_claim_rows_v2(
                V2FakeConn(claim_rows=[row]), OWNER, [CLAIM]
            )


if __name__ == "__main__":
    unittest.main()
