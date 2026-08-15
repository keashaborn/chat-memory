from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest
from uuid import UUID

from rag_engine.governed_memory.contracts import ContractViolation
from rag_engine.governed_memory.projection import (
    DISTANCE as PROJECTION_DISTANCE,
    build_projection_delete,
    build_projection_point,
)
from rag_engine.governed_memory.runtime.calibration import CalibrationDecision
from rag_engine.governed_memory.runtime.qdrant_adapter import (
    ExactQdrantAdapter,
    QDRANT_ALIAS,
    QDRANT_DISTANCE,
    QDRANT_PHYSICAL_COLLECTION,
    QDRANT_REQUIRED_PAYLOAD_INDEXES,
    QDRANT_SEARCH_PAYLOAD_FIELDS,
    QDRANT_VECTOR_SIZE,
    QdrantWriteOutcomeUnknown,
)
from tests.memory._fixtures import (
    OWNER_A,
    deterministic_vector,
    make_claim_row,
    make_projection_outbox,
)


ROOT = Path(__file__).resolve().parents[2]


class FakeQdrantTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.alias_target = QDRANT_PHYSICAL_COLLECTION
        self.alias_result: object | None = None
        self.distance = QDRANT_DISTANCE
        self.vector_size = QDRANT_VECTOR_SIZE
        self.indexes = dict(QDRANT_REQUIRED_PAYLOAD_INDEXES)
        self.search_results: list[dict[str, object]] = []
        self.stored_point: dict[str, object] | None = None
        self.ambiguous_upsert = False
        self.ambiguous_delete = False
        self.keep_after_delete = False

    async def request(self, method: str, path: str, body: object = None) -> dict[str, object]:
        self.calls.append((method, path, body))
        if method == "GET" and path == (
            f"/collections/{QDRANT_PHYSICAL_COLLECTION}/aliases"
        ):
            result = self.alias_result
            if result is None:
                result = {
                    "aliases": [
                        {
                            "alias_name": QDRANT_ALIAS,
                            "collection_name": self.alias_target,
                        }
                    ]
                }
            return {"result": deepcopy(result)}
        if method == "GET" and path == f"/collections/{QDRANT_PHYSICAL_COLLECTION}":
            return {
                "result": {
                    "config": {
                        "params": {
                            "vectors": {
                                "distance": self.distance,
                                "size": self.vector_size,
                            }
                        }
                    },
                    "payload_schema": {
                        key: {"data_type": value}
                        for key, value in self.indexes.items()
                    },
                }
            }
        if method == "POST" and path == f"/collections/{QDRANT_ALIAS}/points/search":
            return {"result": deepcopy(self.search_results)}
        if method == "PUT" and path.endswith("/points?wait=true"):
            point = body["points"][0]  # type: ignore[index]
            if self.stored_point is None:
                self.stored_point = {
                    "id": point["id"],
                    "payload": deepcopy(point["payload"]),
                    "vector": list(point["vector"]),
                }
            if self.ambiguous_upsert:
                raise QdrantWriteOutcomeUnknown()
            return {"result": {"status": "completed"}}
        if method == "POST" and path.endswith("/points/delete?wait=true"):
            if not self.keep_after_delete:
                self.stored_point = None
            if self.ambiguous_delete:
                raise QdrantWriteOutcomeUnknown()
            return {"result": {"status": "completed"}}
        if method == "POST" and path.endswith("/points"):
            return {
                "result": []
                if self.stored_point is None
                else [deepcopy(self.stored_point)]
            }
        raise AssertionError(f"unexpected fake Qdrant request: {method} {path}")


def projection_point() -> dict[str, object]:
    claim = make_claim_row()
    outbox = make_projection_outbox(claim)
    return build_projection_point(claim, outbox, deterministic_vector())


def approved_calibration(threshold: int = 800_000) -> CalibrationDecision:
    return CalibrationDecision(
        artifact_sha256="a" * 64,
        reason_code="calibration_approved",
        retrieval_enabled=True,
        threshold_micros=threshold,
    )


class QdrantPreflightTests(unittest.IsolatedAsyncioTestCase):
    def test_distance_and_size_bind_checked_in_projection_and_create_contracts(
        self,
    ) -> None:
        create_contract = json.loads(
            (
                ROOT / "ops" / "governed_memory" / "qdrant_collection.create.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(PROJECTION_DISTANCE, "dot")
        self.assertEqual(
            create_contract["vectors"],
            {"distance": QDRANT_DISTANCE, "size": QDRANT_VECTOR_SIZE},
        )

    async def test_preflight_reads_only_exact_configured_resources(self) -> None:
        transport = FakeQdrantTransport()
        receipt = await ExactQdrantAdapter(transport).preflight()
        self.assertEqual(receipt.alias, QDRANT_ALIAS)
        self.assertEqual(receipt.physical_collection, QDRANT_PHYSICAL_COLLECTION)
        self.assertEqual(receipt.vector_size, 3_072)
        self.assertEqual(receipt.distance, "Dot")
        self.assertEqual(
            transport.calls,
            [
                (
                    "GET",
                    f"/collections/{QDRANT_PHYSICAL_COLLECTION}/aliases",
                    None,
                ),
                ("GET", f"/collections/{QDRANT_PHYSICAL_COLLECTION}", None),
            ],
        )
        self.assertFalse(any(path in {"/aliases", "/collections"} for _, path, _ in transport.calls))
        self.assertFalse(
            any(path == f"/aliases/{QDRANT_ALIAS}" for _, path, _ in transport.calls)
        )
        self.assertFalse(any(method in {"PUT", "DELETE"} for method, _, _ in transport.calls))

    async def test_preflight_refuses_non_closed_ambiguous_or_wrong_alias_rows(
        self,
    ) -> None:
        exact = {
            "alias_name": QDRANT_ALIAS,
            "collection_name": QDRANT_PHYSICAL_COLLECTION,
        }
        cases = (
            ({"aliases": []}, "qdrant_alias_missing_or_ambiguous"),
            (
                {"aliases": [exact, exact]},
                "qdrant_alias_missing_or_ambiguous",
            ),
            (
                {"aliases": [{**exact, "unexpected": True}]},
                "qdrant_alias_response_invalid",
            ),
            (
                {"aliases": [{**exact, "alias_name": "other_alias"}]},
                "qdrant_alias_identity_mismatch",
            ),
            (
                {"aliases": [exact], "unexpected": True},
                "qdrant_alias_response_invalid",
            ),
            ({"aliases": "not-a-list"}, "qdrant_alias_missing_or_ambiguous"),
        )
        for result, code in cases:
            transport = FakeQdrantTransport()
            transport.alias_result = result
            with self.subTest(result=result), self.assertRaisesRegex(
                ContractViolation,
                code,
            ):
                await ExactQdrantAdapter(transport).preflight()

    async def test_preflight_refuses_alias_distance_dimension_and_index_drift(self) -> None:
        cases = (
            ("alias_target", "other_collection", "qdrant_alias_target_mismatch"),
            ("distance", "Cosine", "qdrant_distance_mismatch"),
            ("vector_size", 1_536, "qdrant_dimension_mismatch"),
            ("missing_index", "owner_user_id", "qdrant_required_payload_index_missing"),
        )
        for field, value, code in cases:
            transport = FakeQdrantTransport()
            if field == "missing_index":
                transport.indexes.pop(str(value))
            else:
                setattr(transport, field, value)
            with self.subTest(field=field), self.assertRaisesRegex(
                ContractViolation,
                code,
            ):
                await ExactQdrantAdapter(transport).preflight()


class QdrantSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_unapproved_calibration_returns_no_candidates_and_performs_no_io(self) -> None:
        transport = FakeQdrantTransport()
        disabled = CalibrationDecision(
            artifact_sha256=None,
            reason_code="calibration_absent",
            retrieval_enabled=False,
            threshold_micros=None,
        )
        result = await ExactQdrantAdapter(transport).search_owner_candidates(
            allowed_predicates=("preference.personal",),
            calibration=disabled,
            limit=8,
            owner_user_id=OWNER_A,
            query_vector=deterministic_vector(),
        )
        self.assertEqual(result, ())
        self.assertEqual(transport.calls, [])

    async def test_search_is_owner_filtered_bounded_payload_only_and_thresholded(self) -> None:
        point = projection_point()
        payload = {
            field: point["payload"][field]  # type: ignore[index]
            for field in QDRANT_SEARCH_PAYLOAD_FIELDS
        }
        transport = FakeQdrantTransport()
        transport.search_results = [
            {
                "id": point["point_id"],
                "payload": payload,
                "score": 0.91,
            }
        ]
        result = await ExactQdrantAdapter(transport).search_owner_candidates(
            allowed_predicates=("preference.personal",),
            calibration=approved_calibration(),
            limit=8,
            owner_user_id=OWNER_A,
            query_vector=deterministic_vector(),
        )
        self.assertEqual(len(result), 1)
        search_call = transport.calls[-1]
        self.assertEqual(search_call[:2], ("POST", f"/collections/{QDRANT_ALIAS}/points/search"))
        body = search_call[2]
        self.assertEqual(body["limit"], 8)  # type: ignore[index]
        self.assertFalse(body["with_vector"])  # type: ignore[index]
        self.assertEqual(body["score_threshold"], 0.8)  # type: ignore[index]
        self.assertEqual(
            body["with_payload"],  # type: ignore[index]
            {"include": list(QDRANT_SEARCH_PAYLOAD_FIELDS)},
        )
        self.assertIn(
            {"key": "owner_user_id", "match": {"value": str(OWNER_A)}},
            body["filter"]["must"],  # type: ignore[index]
        )

    async def test_explicit_recall_is_owner_filtered_and_uses_minimum_relevance_gate(
        self,
    ) -> None:
        point = projection_point()
        payload = {
            field: point["payload"][field]  # type: ignore[index]
            for field in QDRANT_SEARCH_PAYLOAD_FIELDS
        }
        transport = FakeQdrantTransport()
        transport.search_results = [
            {
                "id": point["point_id"],
                "payload": payload,
                "score": 0.20,
            }
        ]
        result = await ExactQdrantAdapter(transport).search_owner_candidates(
            allowed_predicates=("preference.personal",),
            calibration=approved_calibration(),
            explicit_recall=True,
            limit=8,
            owner_user_id=OWNER_A,
            query_vector=deterministic_vector(),
        )
        self.assertEqual(len(result), 1)
        body = transport.calls[-1][2]
        self.assertEqual(body["score_threshold"], 0.2)  # type: ignore[index]
        self.assertNotIn(
            {"key": "requires_explicit", "match": {"value": False}},
            body["filter"]["must"],  # type: ignore[index]
        )
        self.assertIn(
            {"key": "owner_user_id", "match": {"value": str(OWNER_A)}},
            body["filter"]["must"],  # type: ignore[index]
        )

    async def test_explicit_recall_refuses_candidates_below_minimum_relevance_gate(
        self,
    ) -> None:
        point = projection_point()
        payload = {
            field: point["payload"][field]  # type: ignore[index]
            for field in QDRANT_SEARCH_PAYLOAD_FIELDS
        }
        transport = FakeQdrantTransport()
        transport.search_results = [
            {
                "id": point["point_id"],
                "payload": payload,
                "score": 0.199999,
            }
        ]
        with self.assertRaisesRegex(
            ContractViolation,
            "qdrant_search_score_below_threshold",
        ):
            await ExactQdrantAdapter(transport).search_owner_candidates(
                allowed_predicates=("preference.personal",),
                calibration=approved_calibration(),
                explicit_recall=True,
                limit=8,
                owner_user_id=OWNER_A,
                query_vector=deterministic_vector(),
            )

    async def test_search_refuses_vector_payload_expansion_and_scores_below_gate(self) -> None:
        point = projection_point()
        payload = {
            field: point["payload"][field]  # type: ignore[index]
            for field in QDRANT_SEARCH_PAYLOAD_FIELDS
        }
        cases = (
            ({"id": point["point_id"], "payload": {**payload, "object_literal": "leak"}, "score": 0.9}, "qdrant_search_payload_not_allowlisted"),
            ({"id": point["point_id"], "payload": payload, "score": 0.9, "vector": [0.0]}, "qdrant_search_vector_returned"),
            ({"id": point["point_id"], "payload": payload, "score": 0.79}, "qdrant_search_score_below_threshold"),
        )
        for hit, code in cases:
            transport = FakeQdrantTransport()
            transport.search_results = [hit]
            with self.subTest(code=code), self.assertRaisesRegex(
                ContractViolation,
                code,
            ):
                await ExactQdrantAdapter(transport).search_owner_candidates(
                    allowed_predicates=("preference.personal",),
                    calibration=approved_calibration(),
                    limit=8,
                    owner_user_id=OWNER_A,
                    query_vector=deterministic_vector(),
                )


class QdrantWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_ambiguous_upsert_is_resolved_only_by_exact_physical_readback_hashes(self) -> None:
        point = projection_point()
        transport = FakeQdrantTransport()
        transport.ambiguous_upsert = True
        transport.stored_point = {
            "id": point["point_id"],
            "payload": point["payload"],
            "vector": point["vector"],
        }
        receipt = await ExactQdrantAdapter(transport).upsert_projection_point(point)
        self.assertTrue(receipt.resolved_by_readback)
        self.assertEqual(receipt.physical_collection, QDRANT_PHYSICAL_COLLECTION)
        retrieve_paths = [
            path for method, path, _ in transport.calls if method == "POST" and path.endswith("/points")
        ]
        self.assertEqual(retrieve_paths, [f"/collections/{QDRANT_PHYSICAL_COLLECTION}/points"])

        mismatched = deepcopy(transport.stored_point)
        mismatched["payload"] = {**mismatched["payload"], "revision_number": 2}  # type: ignore[index]
        transport = FakeQdrantTransport()
        transport.ambiguous_upsert = True
        transport.stored_point = mismatched
        with self.assertRaisesRegex(
            ContractViolation,
            "qdrant_upsert_readback_mismatch",
        ):
            await ExactQdrantAdapter(transport).upsert_projection_point(point)

    async def test_completed_upsert_is_verified_by_exact_physical_readback(self) -> None:
        point = projection_point()
        transport = FakeQdrantTransport()
        receipt = await ExactQdrantAdapter(transport).upsert_projection_point(point)
        self.assertFalse(receipt.resolved_by_readback)
        self.assertEqual(receipt.physical_collection, QDRANT_PHYSICAL_COLLECTION)
        self.assertEqual(
            [
                path
                for method, path, _body in transport.calls
                if method == "POST" and path.endswith("/points")
            ],
            [f"/collections/{QDRANT_PHYSICAL_COLLECTION}/points"],
        )

    async def test_delete_verifies_absence_through_alias_and_physical_collection(self) -> None:
        claim = make_claim_row(lifecycle_state="retracted", projection_sequence=2)
        outbox = make_projection_outbox(claim, operation="delete")
        command = build_projection_delete(
            outbox,
            collection_alias=QDRANT_ALIAS,
            physical_collection=QDRANT_PHYSICAL_COLLECTION,
        )
        transport = FakeQdrantTransport()
        transport.ambiguous_delete = True
        receipt = await ExactQdrantAdapter(transport).delete_projection_point(command)
        self.assertEqual(receipt.collection_alias, QDRANT_ALIAS)
        self.assertEqual(receipt.physical_collection, QDRANT_PHYSICAL_COLLECTION)
        paths = [path for method, path, _ in transport.calls if method == "POST" and path.endswith("/points")]
        self.assertEqual(
            paths,
            [
                f"/collections/{QDRANT_ALIAS}/points",
                f"/collections/{QDRANT_PHYSICAL_COLLECTION}/points",
            ],
        )

        transport = FakeQdrantTransport()
        transport.keep_after_delete = True
        transport.stored_point = {"id": str(UUID(str(command["point_id"])))}
        with self.assertRaisesRegex(
            ContractViolation,
            "qdrant_absence_not_verified",
        ):
            await ExactQdrantAdapter(transport).delete_projection_point(command)


if __name__ == "__main__":
    unittest.main()
