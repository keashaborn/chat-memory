from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import unittest
from typing import Any, Mapping
from uuid import UUID

from rag_engine.governed_memory.contracts import (
    ContractViolation,
    canonical_sha256,
    sha256_text,
)
from rag_engine.governed_memory.postgres_adapter import PROJECTION_REBUILD_ROW_FIELDS
from rag_engine.governed_memory.projection import (
    EMBEDDING_MODEL,
    build_rebuild_projection_point,
)
from rag_engine.governed_memory.retrieval import AUTHORITATIVE_RETRIEVAL_ROW_FIELDS
from rag_engine.governed_memory.runtime.qdrant_adapter import (
    QDRANT_ALIAS,
    QDRANT_PHYSICAL_COLLECTION,
)
from rag_engine.governed_memory.runtime.rebuild_controller import (
    RebuildEmbedding,
    RebuildTargetReceipt,
    SuccessorRebuildController,
)
from tests.memory._fixtures import (
    deterministic_vector,
    make_claim_row,
    make_projection_outbox,
)


TARGET = "governed_memory_9a54cf123493_000002"


def rebuild_row() -> dict[str, Any]:
    claim = make_claim_row()
    outbox = make_projection_outbox(claim)
    outbox["state"] = "applied"
    point = build_rebuild_projection_point(claim, outbox, deterministic_vector())
    row = {field: claim[field] for field in AUTHORITATIVE_RETRIEVAL_ROW_FIELDS}
    for field in ("owner_user_id", "claim_id", "revision_id"):
        row[field] = UUID(str(row[field]))
    row.update(
        {
            "outbox_id": UUID("00000000-0000-4000-8000-000000000099"),
            "projection_operation_id": UUID(str(outbox["operation_id"])),
            "projection_operation": outbox["operation"],
            "outbox_sequence_number": outbox["sequence_number"],
            "point_id": UUID(str(claim["claim_id"])),
            "collection_alias": QDRANT_ALIAS,
            "projection_contract_sha256": outbox["projection_contract_sha256"],
            "dimensions": 3_072,
            "embedding_model": EMBEDDING_MODEL,
            "renderer_sha256": point["payload"]["renderer_sha256"],
            "projection_manifest_sha256": outbox["projection_manifest_sha256"],
            "embedding_input_sha256": outbox["embedding_input_sha256"],
            "applied_vector_sha256": point["payload"]["vector_sha256"],
            "applied_physical_collection": QDRANT_PHYSICAL_COLLECTION,
            "applied_at": datetime(2026, 8, 14, tzinfo=timezone.utc),
        }
    )
    return {field: row[field] for field in PROJECTION_REBUILD_ROW_FIELDS}


class FakeRepository:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[UUID | None, UUID | None, int]] = []

    async def read_projection_rebuild_batch(
        self,
        *,
        after_owner_user_id: UUID | None,
        after_claim_id: UUID | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        self.calls.append((after_owner_user_id, after_claim_id, limit))
        if after_owner_user_id is None:
            return deepcopy(self.rows[:limit])
        cursor = (after_owner_user_id, after_claim_id)
        return [
            deepcopy(row)
            for row in self.rows
            if (UUID(str(row["owner_user_id"])), UUID(str(row["claim_id"]))) > cursor
        ][:limit]


class FakeEmbeddingProvider:
    def __init__(self, vector: tuple[float, ...] | None = None) -> None:
        self.vector = vector or deterministic_vector()
        self.inputs: list[str] = []

    async def embed_rebuild_text(self, text: str) -> RebuildEmbedding:
        self.inputs.append(text)
        return RebuildEmbedding(
            vector=self.vector,
            model=EMBEDDING_MODEL,
            input_sha256=sha256_text(text),
        )


class FakeQdrant:
    def __init__(self) -> None:
        self.alias_target = QDRANT_PHYSICAL_COLLECTION
        self.created: list[dict[str, Any]] = []
        self.points: list[dict[str, Any]] = []
        self.expected_manifest: str | None = None
        self.swaps: list[tuple[str, str]] = []

    async def create_empty_target(self, **kwargs: Any) -> None:
        self.created.append(kwargs)

    async def upsert_rebuild_point(
        self, *, target_collection: str, point: Mapping[str, Any]
    ) -> None:
        self.points.append(deepcopy(dict(point)))

    async def verify_target(
        self,
        *,
        target_collection: str,
        expected_point_count: int,
        expected_manifest_sha256: str,
    ) -> RebuildTargetReceipt:
        self.expected_manifest = expected_manifest_sha256
        if len(self.points) != expected_point_count:
            raise AssertionError("fake target count mismatch")
        material = {
            "manifest_sha256": expected_manifest_sha256,
            "point_count": expected_point_count,
            "target_collection": target_collection,
        }
        return RebuildTargetReceipt(
            **material,
            verification_receipt_sha256=canonical_sha256(
                "test.rebuild_target_verification", material
            ),
        )

    async def swap_alias(
        self,
        *,
        alias: str,
        expected_current_collection: str,
        target_collection: str,
        point_count: int,
        manifest_sha256: str,
    ) -> str:
        if alias != QDRANT_ALIAS or self.alias_target != expected_current_collection:
            raise ContractViolation("fake_alias_identity_mismatch")
        self.swaps.append((expected_current_collection, target_collection))
        self.alias_target = target_collection
        return canonical_sha256(
            "test.rebuild_alias_verification",
            {
                "alias": alias,
                "manifest_sha256": manifest_sha256,
                "point_count": point_count,
                "target_collection": target_collection,
            },
        )


class SuccessorRebuildControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_nonempty_prepare_cutover_and_rollback_keep_both_generations(self) -> None:
        row = rebuild_row()
        repository = FakeRepository([row])
        embedding = FakeEmbeddingProvider()
        qdrant = FakeQdrant()
        controller = SuccessorRebuildController(
            repository=repository,
            embedding_provider=embedding,
            qdrant=qdrant,
            batch_size=1,
        )

        prepared = await controller.prepare(
            source_collection=QDRANT_PHYSICAL_COLLECTION,
            target_collection=TARGET,
        )
        self.assertEqual(prepared.point_count, 1)
        self.assertEqual(len(repository.calls), 2)
        self.assertEqual(embedding.inputs, [row["retrieval_text"]])
        self.assertEqual(len(qdrant.created), 1)
        self.assertEqual(len(qdrant.points), 1)
        self.assertEqual(qdrant.created[0]["source_collection"], QDRANT_PHYSICAL_COLLECTION)
        self.assertEqual(qdrant.created[0]["target_collection"], TARGET)

        cutover = await controller.cutover(prepared)
        self.assertEqual(cutover.previous_collection, QDRANT_PHYSICAL_COLLECTION)
        self.assertEqual(cutover.active_collection, TARGET)
        self.assertEqual(qdrant.alias_target, TARGET)
        rollback = await controller.rollback(prepared)
        self.assertEqual(rollback.previous_collection, TARGET)
        self.assertEqual(rollback.active_collection, QDRANT_PHYSICAL_COLLECTION)
        self.assertEqual(qdrant.alias_target, QDRANT_PHYSICAL_COLLECTION)
        self.assertEqual(
            qdrant.swaps,
            [(QDRANT_PHYSICAL_COLLECTION, TARGET), (TARGET, QDRANT_PHYSICAL_COLLECTION)],
        )

    async def test_empty_rebuild_is_valid_and_performs_no_embedding(self) -> None:
        embedding = FakeEmbeddingProvider()
        qdrant = FakeQdrant()
        controller = SuccessorRebuildController(
            repository=FakeRepository([]),
            embedding_provider=embedding,
            qdrant=qdrant,
        )
        prepared = await controller.prepare(
            source_collection=QDRANT_PHYSICAL_COLLECTION,
            target_collection=TARGET,
        )
        self.assertEqual(prepared.point_count, 0)
        self.assertEqual(embedding.inputs, [])
        self.assertEqual(qdrant.points, [])

    async def test_refuses_non_next_target_before_any_effect(self) -> None:
        qdrant = FakeQdrant()
        controller = SuccessorRebuildController(
            repository=FakeRepository([]),
            embedding_provider=FakeEmbeddingProvider(),
            qdrant=qdrant,
        )
        with self.assertRaisesRegex(
            ContractViolation, "rebuild_target_generation_not_next"
        ):
            await controller.prepare(
                source_collection=QDRANT_PHYSICAL_COLLECTION,
                target_collection="governed_memory_9a54cf123493_000003",
            )
        self.assertEqual(qdrant.created, [])

    async def test_refuses_row_applied_to_another_source(self) -> None:
        row = rebuild_row()
        row["applied_physical_collection"] = TARGET
        qdrant = FakeQdrant()
        controller = SuccessorRebuildController(
            repository=FakeRepository([row]),
            embedding_provider=FakeEmbeddingProvider(),
            qdrant=qdrant,
        )
        with self.assertRaisesRegex(
            ContractViolation, "rebuild_applied_collection_mismatch"
        ):
            await controller.prepare(
                source_collection=QDRANT_PHYSICAL_COLLECTION,
                target_collection=TARGET,
            )
        self.assertEqual(qdrant.points, [])

    async def test_refuses_embedding_vector_drift(self) -> None:
        row = rebuild_row()
        vector = list(deterministic_vector())
        vector[0], vector[1] = vector[1], vector[0]
        qdrant = FakeQdrant()
        controller = SuccessorRebuildController(
            repository=FakeRepository([row]),
            embedding_provider=FakeEmbeddingProvider(tuple(vector)),
            qdrant=qdrant,
        )
        with self.assertRaisesRegex(
            ContractViolation, "rebuild_vector_sha256_drift"
        ):
            await controller.prepare(
                source_collection=QDRANT_PHYSICAL_COLLECTION,
                target_collection=TARGET,
            )
        self.assertEqual(qdrant.points, [])

    async def test_refuses_duplicate_or_nonadvancing_rows(self) -> None:
        row = rebuild_row()
        qdrant = FakeQdrant()
        controller = SuccessorRebuildController(
            repository=FakeRepository([row, row]),
            embedding_provider=FakeEmbeddingProvider(),
            qdrant=qdrant,
            batch_size=2,
        )
        with self.assertRaisesRegex(
            ContractViolation, "rebuild_rows_not_strictly_ordered"
        ):
            await controller.prepare(
                source_collection=QDRANT_PHYSICAL_COLLECTION,
                target_collection=TARGET,
            )


if __name__ == "__main__":
    unittest.main()
