from __future__ import annotations

import unittest
from typing import Any
from uuid import UUID

from rag_engine.memory_v1_governed_postgres_loaders_v1 import (
    GovernedPostgresLoaderError,
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
    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetches.append((query, args))
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


class GovernedPostgresLoaderV2Test(unittest.IsolatedAsyncioTestCase):
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
