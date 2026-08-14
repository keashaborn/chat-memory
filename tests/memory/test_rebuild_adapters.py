from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import unittest
from typing import Any, Mapping
from uuid import UUID

from rag_engine.governed_memory.contracts import ContractViolation
from rag_engine.governed_memory.postgres_adapter import PROJECTION_REBUILD_ROW_FIELDS
from rag_engine.governed_memory.projection import (
    EMBEDDING_MODEL,
    build_rebuild_projection_point,
)
from rag_engine.governed_memory.retrieval import AUTHORITATIVE_RETRIEVAL_ROW_FIELDS
from rag_engine.governed_memory.runtime.https_transport import HttpsResponse
from rag_engine.governed_memory.runtime.openai_adapters import (
    OpenAIEmbeddingAdapter,
    OpenAIEndpointConfig,
)
from rag_engine.governed_memory.runtime.qdrant_adapter import (
    QDRANT_ALIAS,
    QDRANT_DISTANCE,
    QDRANT_PHYSICAL_COLLECTION,
    QDRANT_REQUIRED_PAYLOAD_INDEXES,
    QDRANT_VECTOR_SIZE,
)
from rag_engine.governed_memory.runtime.rebuild_adapters import (
    ExactRebuildQdrantStore,
    OpenAIRebuildEmbeddingProvider,
    PostgresProjectionRebuildRepository,
)
from rag_engine.governed_memory.runtime.rebuild_controller import (
    REBUILD_INITIAL_MANIFEST_SHA256,
    rebuild_manifest_step_sha256,
)
from rag_engine.governed_memory.runtime.rebuild_qdrant_transport import (
    RebuildQdrantResponse,
)
from tests.memory._fixtures import (
    deterministic_vector,
    make_claim_row,
    make_projection_outbox,
)


TARGET = "governed_memory_9a54cf123493_000002"


def projection_point() -> dict[str, Any]:
    claim = make_claim_row()
    outbox = make_projection_outbox(claim)
    outbox["state"] = "applied"
    return build_rebuild_projection_point(claim, outbox, deterministic_vector())


def postgres_row() -> dict[str, Any]:
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
            "projection_operation": "upsert",
            "outbox_sequence_number": outbox["sequence_number"],
            "point_id": row["claim_id"],
            "collection_alias": QDRANT_ALIAS,
            "projection_contract_sha256": outbox["projection_contract_sha256"],
            "dimensions": QDRANT_VECTOR_SIZE,
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


def collection_result(indexes: Mapping[str, str], point_count: int) -> dict[str, Any]:
    return {
        "config": {
            "params": {
                "vectors": {"distance": QDRANT_DISTANCE, "size": QDRANT_VECTOR_SIZE}
            }
        },
        "payload_schema": {
            field: {"data_type": kind} for field, kind in indexes.items()
        },
        "points_count": point_count,
    }


class FakeAdminTransport:
    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = {
            QDRANT_PHYSICAL_COLLECTION: {
                "indexes": dict(QDRANT_REQUIRED_PAYLOAD_INDEXES),
                "points": {},
            }
        }
        self.aliases = {QDRANT_ALIAS: QDRANT_PHYSICAL_COLLECTION}
        self.calls: list[tuple[str, str, object]] = []

    async def request(
        self,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
        *,
        accepted_statuses: frozenset[int] = frozenset({200}),
    ) -> RebuildQdrantResponse:
        self.calls.append((method, path, body))
        if method == "POST" and path == "/collections/aliases":
            actions = body["actions"]
            for action in actions:
                if "delete_alias" in action:
                    self.aliases.pop(action["delete_alias"]["alias_name"])
                else:
                    create = action["create_alias"]
                    self.aliases[create["alias_name"]] = create["collection_name"]
            return RebuildQdrantResponse(
                status=200, body={"result": True, "status": "ok"}
            )
        collection = path.split("/")[2]
        if method == "GET" and path.endswith("/aliases"):
            rows = [
                {"alias_name": alias, "collection_name": target}
                for alias, target in self.aliases.items()
                if target == collection
            ]
            return RebuildQdrantResponse(status=200, body={"result": {"aliases": rows}})
        if method == "GET":
            if collection not in self.collections:
                if 404 not in accepted_statuses:
                    raise AssertionError("unexpected missing collection")
                return RebuildQdrantResponse(status=404, body={"status": "not_found"})
            current = self.collections[collection]
            return RebuildQdrantResponse(
                status=200,
                body={
                    "result": collection_result(
                        current["indexes"], len(current["points"])
                    )
                },
            )
        if method == "PUT" and path == f"/collections/{collection}":
            self.collections[collection] = {"indexes": {}, "points": {}}
            return RebuildQdrantResponse(status=200, body={"result": True, "status": "ok"})
        if method == "PUT" and path.endswith("/index?wait=true"):
            self.collections[collection]["indexes"][body["field_name"]] = body["field_schema"]
            return RebuildQdrantResponse(
                status=200,
                body={"result": {"status": "completed"}, "status": "ok"},
            )
        if method == "PUT" and path.endswith("/points?wait=true"):
            for point in body["points"]:
                self.collections[collection]["points"][str(point["id"])] = deepcopy(point)
            return RebuildQdrantResponse(
                status=200,
                body={"result": {"status": "completed"}, "status": "ok"},
            )
        if method == "POST" and path.endswith("/points/scroll"):
            points = list(self.collections[collection]["points"].values())
            return RebuildQdrantResponse(
                status=200,
                body={
                    "result": {
                        "points": [
                            {
                                "id": point["id"],
                                "payload": deepcopy(point["payload"]),
                                "vector": list(point["vector"]),
                            }
                            for point in points
                        ],
                        "next_page_offset": None,
                    }
                },
            )
        if method == "POST" and path.endswith("/points"):
            rows = []
            for point_id in body["ids"]:
                point = self.collections[collection]["points"].get(str(point_id))
                if point is not None:
                    rows.append(
                        {
                            "id": point["id"],
                            "payload": deepcopy(point["payload"]),
                            "vector": list(point["vector"]),
                        }
                    )
            return RebuildQdrantResponse(status=200, body={"result": rows})
        raise AssertionError(f"unexpected request {method} {path}")


class FakePostgresConnection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[object, ...]] = []

    async def fetch(self, *args: object) -> list[dict[str, Any]]:
        self.calls.append(args)
        return deepcopy(self.rows)


class FakeHttpsTransport:
    def post(self, request: Any) -> HttpsResponse:
        vector = list(deterministic_vector())
        body = json.dumps(
            {
                "object": "list",
                "model": EMBEDDING_MODEL,
                "data": [{"object": "embedding", "index": 0, "embedding": vector}],
                "usage": {"prompt_tokens": 3, "total_tokens": 3},
            }
        ).encode("utf-8")
        return HttpsResponse(
            status=200,
            headers=(("content-type", "application/json"),),
            body=body,
        )


class RebuildAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_postgres_repository_uses_exact_bounded_rpc(self) -> None:
        row = postgres_row()
        connection = FakePostgresConnection([row])
        repository = PostgresProjectionRebuildRepository(connection)
        rows = await repository.read_projection_rebuild_batch(
            after_owner_user_id=None,
            after_claim_id=None,
            limit=100,
        )
        self.assertEqual(rows, (row,))
        self.assertIn("read_projection_rebuild_batch", connection.calls[0][0])
        self.assertEqual(connection.calls[0][1:], (None, None, 100))

    async def test_openai_adapter_is_bound_to_exact_text_model_and_dimensions(self) -> None:
        adapter = OpenAIEmbeddingAdapter(
            config=OpenAIEndpointConfig(
                api_key="synthetic-test-key",
                extraction_model="gpt-5-mini",
            ),
            transport=FakeHttpsTransport(),
        )
        provider = OpenAIRebuildEmbeddingProvider(adapter)
        result = await provider.embed_rebuild_text("synthetic rebuild text")
        self.assertEqual(result.model, EMBEDDING_MODEL)
        self.assertEqual(len(result.vector), QDRANT_VECTOR_SIZE)

    async def test_exact_store_creates_indexes_upserts_verifies_and_swaps_both_ways(self) -> None:
        transport = FakeAdminTransport()
        store = ExactRebuildQdrantStore(transport)
        await store.create_empty_target(
            alias=QDRANT_ALIAS,
            source_collection=QDRANT_PHYSICAL_COLLECTION,
            target_collection=TARGET,
            vector_size=QDRANT_VECTOR_SIZE,
            distance=QDRANT_DISTANCE,
            payload_indexes=QDRANT_REQUIRED_PAYLOAD_INDEXES,
        )
        point = projection_point()
        await store.upsert_rebuild_point(target_collection=TARGET, point=point)
        manifest = rebuild_manifest_step_sha256(
            REBUILD_INITIAL_MANIFEST_SHA256, 1, point
        )
        receipt = await store.verify_target(
            target_collection=TARGET,
            expected_point_count=1,
            expected_manifest_sha256=manifest,
        )
        self.assertEqual(receipt.point_count, 1)
        await store.swap_alias(
            alias=QDRANT_ALIAS,
            expected_current_collection=QDRANT_PHYSICAL_COLLECTION,
            target_collection=TARGET,
            point_count=1,
            manifest_sha256=manifest,
        )
        self.assertEqual(transport.aliases[QDRANT_ALIAS], TARGET)
        await store.swap_alias(
            alias=QDRANT_ALIAS,
            expected_current_collection=TARGET,
            target_collection=QDRANT_PHYSICAL_COLLECTION,
            point_count=1,
            manifest_sha256=manifest,
        )
        self.assertEqual(transport.aliases[QDRANT_ALIAS], QDRANT_PHYSICAL_COLLECTION)
        self.assertIn(TARGET, transport.collections)

    async def test_verification_refuses_an_unexpected_target_point(self) -> None:
        transport = FakeAdminTransport()
        store = ExactRebuildQdrantStore(transport)
        await store.create_empty_target(
            alias=QDRANT_ALIAS,
            source_collection=QDRANT_PHYSICAL_COLLECTION,
            target_collection=TARGET,
            vector_size=QDRANT_VECTOR_SIZE,
            distance=QDRANT_DISTANCE,
            payload_indexes=QDRANT_REQUIRED_PAYLOAD_INDEXES,
        )
        point = projection_point()
        await store.upsert_rebuild_point(target_collection=TARGET, point=point)
        transport.collections[TARGET]["points"]["unexpected"] = deepcopy(
            next(iter(transport.collections[TARGET]["points"].values()))
        )
        manifest = rebuild_manifest_step_sha256(
            REBUILD_INITIAL_MANIFEST_SHA256, 1, point
        )
        with self.assertRaisesRegex(
            ContractViolation, "rebuild_qdrant_target_count_mismatch"
        ):
            await store.verify_target(
                target_collection=TARGET,
                expected_point_count=1,
                expected_manifest_sha256=manifest,
            )

    async def test_bind_existing_generations_requires_one_exact_active_alias(self) -> None:
        transport = FakeAdminTransport()
        transport.collections[TARGET] = {
            "indexes": dict(QDRANT_REQUIRED_PAYLOAD_INDEXES),
            "points": {},
        }
        store = ExactRebuildQdrantStore(transport)
        await store.bind_existing_generations(
            source_collection=QDRANT_PHYSICAL_COLLECTION,
            target_collection=TARGET,
            expected_active_collection=QDRANT_PHYSICAL_COLLECTION,
        )
        transport.aliases[QDRANT_ALIAS] = TARGET
        with self.assertRaisesRegex(
            ContractViolation, "rebuild_qdrant_active_alias_mismatch"
        ):
            await store.bind_existing_generations(
                source_collection=QDRANT_PHYSICAL_COLLECTION,
                target_collection=TARGET,
                expected_active_collection=QDRANT_PHYSICAL_COLLECTION,
            )


if __name__ == "__main__":
    unittest.main()
