from __future__ import annotations

import copy
import contextlib
import hashlib
import math
import os
import pathlib
import struct
import tempfile
import types
import unittest
import uuid
from unittest import mock

from rag_engine.memory_v1_qdrant_rebuild_contract_v1 import (
    ACTIVE_ALIAS,
    CONTRACT_VERSION,
    POINT_PROVENANCE_VERSION,
    RebuildContractError,
    SupportedClaimSnapshotV1,
    alias_transition,
    canonical_bytes,
    collection_fingerprint_sha256,
    conversational_quality_report,
    conversational_queries,
    manifest_sha256,
    normalize_cosine_vector,
    point_payload,
    qdrant_mutation_lock,
    require_production_write_guard,
    source_snapshot_sha256,
    validate_lease_acquire_event,
    validate_shadow_collection,
)
from rag_engine.memory_v1_projection import (
    ClaimVectorIndex,
    ProjectionError,
    projection_payload,
    projection_renderer_sha256,
)


OWNER_A = "11111111-1111-4111-8111-111111111111"
OWNER_B = "22222222-2222-4222-8222-222222222222"
CLAIM_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CLAIM_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def source(
    *, owner: str = OWNER_A, claim: str = CLAIM_A, status: str = "supported"
) -> dict:
    return {
        "owner_user_id": owner,
        "claim_id": claim,
        "canonical_text": "I prefer quiet morning walks near the lake.",
        "predicate": "preference.activity",
        "qualifiers": {"time": "morning"},
        "status": status,
        "sensitivity": "low",
        "retrieval_policy": {
            "domains": ["personal"],
            "intents": ["specific_recall"],
            "surface_policy": "support",
        },
        "updated_at": "2026-08-02T04:00:00+00:00",
        "revision_number": 3,
    }


class SourceContractTests(unittest.TestCase):
    def test_supported_source_is_strict_and_deterministic(self) -> None:
        claim = SupportedClaimSnapshotV1.from_mapping(source())
        self.assertEqual(claim.status, "supported")
        self.assertEqual(len(claim.source_sha256()), 64)
        self.assertEqual(claim.source_sha256(), claim.source_sha256())

    def test_unsupported_retracted_and_extra_content_fail_closed(self) -> None:
        for status in ("retracted", "unsupported", "candidate"):
            with self.subTest(status=status), self.assertRaises(RebuildContractError):
                SupportedClaimSnapshotV1.from_mapping(source(status=status))
        changed = source()
        changed["evidence_text"] = "must never enter rebuild source"
        with self.assertRaises(RebuildContractError):
            SupportedClaimSnapshotV1.from_mapping(changed)

    def test_snapshot_hash_is_order_stable_and_duplicate_rejected(self) -> None:
        first = SupportedClaimSnapshotV1.from_mapping(source())
        second = SupportedClaimSnapshotV1.from_mapping(
            source(owner=OWNER_B, claim=CLAIM_B)
        )
        self.assertEqual(
            source_snapshot_sha256([first, second]),
            source_snapshot_sha256([second, first]),
        )
        with self.assertRaises(RebuildContractError):
            source_snapshot_sha256([first, first])

    def test_conversational_queries_are_sparse_question_shapes(self) -> None:
        claim = SupportedClaimSnapshotV1.from_mapping(source())
        queries = conversational_queries(claim)
        self.assertEqual(len(queries), 3)
        self.assertEqual(len(set(queries)), 3)
        self.assertTrue(all(query.endswith("?") for query in queries))
        self.assertNotIn(claim.canonical_text, queries)
        self.assertEqual(queries, conversational_queries(claim))

    def test_point_payload_binds_source_renderer_model_and_snapshot(self) -> None:
        claim = SupportedClaimSnapshotV1.from_mapping(source())
        payload = point_payload(
            claim,
            renderer_sha256=HASH_A,
            rebuild_run_id="memory-qdrant-rebuild-20260802T050000Z-abcdef123456",
            vector_sha256=HASH_B,
            source_snapshot_sha256_value=HASH_C,
        )
        self.assertEqual(payload["schema_version"], POINT_PROVENANCE_VERSION)
        self.assertEqual(payload["owner_user_id"], OWNER_A)
        self.assertEqual(payload["claim_id"], CLAIM_A)
        self.assertEqual(payload["renderer_sha256"], HASH_A)
        self.assertEqual(payload["vector_sha256"], HASH_B)
        self.assertEqual(payload["source_snapshot_sha256"], HASH_C)
        self.assertEqual(
            payload["rebuild_run_id"],
            "memory-qdrant-rebuild-20260802T050000Z-abcdef123456",
        )

    def test_production_guard_requires_bound_runtime_identity(self) -> None:
        lease = {
            "lease_id": "qdrant-rebuild-test-lease",
            "task_id": "qdrant-rebuild-test-task",
            "thread_id": "qdrant-rebuild-test-thread",
            "acquire_event_sha256": "d" * 64,
            "registry_revision": 123,
        }
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(
            RebuildContractError
        ), mock.patch("subprocess.run") as called:
            require_production_write_guard(lease)
        called.assert_not_called()

    def test_lease_acquisition_event_is_canonical_and_identity_bound(self) -> None:
        lease = {
            "lease_id": "qdrant-rebuild-test-lease",
            "task_id": "qdrant-rebuild-test-task",
            "thread_id": "qdrant-rebuild-test-thread",
            "acquire_event_sha256": "0" * 64,
            "registry_revision": 123,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            root.chmod(0o700)
            event = {
                "actor": {
                    "role": "worker",
                    "task_id": lease["task_id"],
                    "thread_id": lease["thread_id"],
                    "uid": os.getuid(),
                    "username": "tester",
                },
                "event_id": "1" * 32,
                "event_type": "acquire",
                "lease_id": lease["lease_id"],
                "occurred_at": "2026-08-02T00:00:00Z",
                "payload": {
                    "change_types": ["production-write"],
                    "worktree": "/opt/chat-memory",
                },
                "previous_event_sha256": "2" * 64,
                "request_id": "request-123",
                "request_sha256": "3" * 64,
                "schema_version": "chat-memory-change-lease-event-v1",
                "sequence": 123,
            }
            event["event_sha256"] = hashlib.sha256(
                canonical_bytes(event)
            ).hexdigest()
            lease["acquire_event_sha256"] = event["event_sha256"]
            path = root / f"{123:020d}-{'1' * 32}.json"
            path.write_bytes(canonical_bytes(event))
            path.chmod(0o600)
            with mock.patch.object(
                pathlib.Path,
                "read_bytes",
                side_effect=AssertionError("path-based lease read is forbidden"),
            ):
                validated = validate_lease_acquire_event(
                    lease, events_root=root, expected_uid=os.getuid()
                )
            self.assertEqual(validated["event_sha256"], lease["acquire_event_sha256"])
            wrong_revision = {**lease, "registry_revision": 124}
            with self.assertRaises(RebuildContractError):
                validate_lease_acquire_event(
                    wrong_revision, events_root=root, expected_uid=os.getuid()
                )
            wrong_digest = {**lease, "acquire_event_sha256": "f" * 64}
            with self.assertRaises(RebuildContractError):
                validate_lease_acquire_event(
                    wrong_digest, events_root=root, expected_uid=os.getuid()
                )

    def test_lease_event_rejects_symlink_and_oversized_descriptor(self) -> None:
        lease = {
            "lease_id": "qdrant-rebuild-test-lease",
            "task_id": "qdrant-rebuild-test-task",
            "thread_id": "qdrant-rebuild-test-thread",
            "acquire_event_sha256": "0" * 64,
            "registry_revision": 123,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            root.chmod(0o700)
            name = f"{123:020d}-{'1' * 32}.json"
            external = root.parent / f"{root.name}-external"
            external.write_bytes(b"{}")
            external.chmod(0o600)
            try:
                (root / name).symlink_to(external)
                with self.assertRaises(RebuildContractError):
                    validate_lease_acquire_event(
                        lease, events_root=root, expected_uid=os.getuid()
                    )
                (root / name).unlink()
                (root / name).write_bytes(b"x" * 262_145)
                (root / name).chmod(0o600)
                with self.assertRaises(RebuildContractError):
                    validate_lease_acquire_event(
                        lease, events_root=root, expected_uid=os.getuid()
                    )
            finally:
                external.unlink(missing_ok=True)

    def test_exclusive_alias_lock_blocks_incremental_projection_mutation(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.upserts = 0

            def upsert(self, **kwargs):
                self.upserts += 1

        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "qdrant.lock"
            exclusive = qdrant_mutation_lock(
                exclusive=True, timeout_seconds=0.0, path=path
            )
            exclusive.acquire()
            client = Client()
            index = ClaimVectorIndex(client, vector_size=3)
            try:
                with mock.patch(
                    "rag_engine.memory_v1_projection.qdrant_mutation_lock",
                    side_effect=lambda **kwargs: qdrant_mutation_lock(
                        exclusive=False, timeout_seconds=0.0, path=path
                    ),
                ), self.assertRaises(RebuildContractError):
                    index.upsert_claim(OWNER_A, source(), [0.1, 0.2, 0.3])
            finally:
                exclusive.release()
            self.assertEqual(client.upserts, 0)


class QualityAndCutoverTests(unittest.TestCase):
    def test_normalized_dot_preserves_cosine_scores_and_ranking(self) -> None:
        documents = ([3.0, 4.0, 1.0], [1.0, 2.0, 5.0], [-2.0, 1.0, 0.5])
        query = [2.0, 3.0, 1.0]

        def cosine(left, right):
            numerator = math.fsum(a * b for a, b in zip(left, right, strict=True))
            denominator = math.sqrt(math.fsum(a * a for a in left)) * math.sqrt(
                math.fsum(b * b for b in right)
            )
            return numerator / denominator

        cosine_scores = [cosine(query, document) for document in documents]
        normalized_query = normalize_cosine_vector(query, dimensions=3)
        normalized_documents = [
            normalize_cosine_vector(document, dimensions=3)
            for document in documents
        ]
        dot_scores = [
            math.fsum(
                a * b
                for a, b in zip(
                    normalized_query, normalized_document, strict=True
                )
            )
            for normalized_document in normalized_documents
        ]
        self.assertEqual(
            sorted(range(3), key=cosine_scores.__getitem__, reverse=True),
            sorted(range(3), key=dot_scores.__getitem__, reverse=True),
        )
        for cosine_score, dot_score in zip(cosine_scores, dot_scores, strict=True):
            self.assertAlmostEqual(cosine_score, dot_score, places=6)

    def test_incremental_writer_is_metric_bound_inside_shared_lock(self) -> None:
        lock_state = {"held": False}

        class Lock:
            def __enter__(self):
                lock_state["held"] = True
                return self

            def __exit__(self, *_args):
                lock_state["held"] = False

        class Client:
            def __init__(self, distance: str) -> None:
                self.distance = distance
                self.point = None

            def get_collection(self, _collection_name):
                self.assert_locked()
                vectors = types.SimpleNamespace(size=3, distance=self.distance)
                return types.SimpleNamespace(
                    config=types.SimpleNamespace(
                        params=types.SimpleNamespace(vectors=vectors)
                    )
                )

            def upsert(self, **kwargs):
                self.assert_locked()
                self.point = kwargs["points"][0]

            @staticmethod
            def assert_locked():
                if not lock_state["held"]:
                    raise AssertionError("metric and write escaped shared lock")

        raw = [3.0, 4.0, 0.0]
        with mock.patch(
            "rag_engine.memory_v1_projection.qdrant_mutation_lock",
            side_effect=lambda **_kwargs: Lock(),
        ):
            cosine = Client("Cosine")
            ClaimVectorIndex(cosine, vector_size=3).upsert_claim(
                OWNER_A, source(), raw
            )
            self.assertEqual(cosine.point.vector, raw)
            self.assertEqual(
                cosine.point.payload["schema_version"], "memory_claim_projection_v1"
            )
            self.assertNotIn("vector_sha256", cosine.point.payload)

            dot = Client("Dot")
            ClaimVectorIndex(dot, vector_size=3).upsert_claim(OWNER_A, source(), raw)
            self.assertEqual(dot.point.vector, [0.6000000238418579, 0.800000011920929, 0.0])
            self.assertEqual(
                dot.point.payload["schema_version"], "memory_claim_projection_v3"
            )
            self.assertEqual(
                dot.point.payload["vector_sha256"],
                hashlib.sha256(struct.pack("<fff", *dot.point.vector)).hexdigest(),
            )

    def test_search_normalizes_every_query_vector(self) -> None:
        class Client:
            query_vector = None

            def search(self, **kwargs):
                self.query_vector = kwargs["query_vector"]
                return [
                    types.SimpleNamespace(
                        id=CLAIM_A,
                        score=0.75,
                        payload={
                            "owner_user_id": OWNER_A,
                            "claim_id": CLAIM_A,
                            "status": "supported",
                            "schema_version": "memory_claim_projection_v1",
                        },
                    )
                ]

        client = Client()
        ClaimVectorIndex(client, vector_size=3).search_claims(
            OWNER_A, [3.0, 4.0, 0.0]
        )
        self.assertEqual(
            client.query_vector, [0.6000000238418579, 0.800000011920929, 0.0]
        )

    def test_ensure_collection_resolves_alias_and_rejects_wrong_metric(self) -> None:
        class Client:
            def __init__(self, distance: str, *, present: bool = True) -> None:
                self.distance = distance
                self.created = False
                self.present = present

            def get_collections(self):
                return types.SimpleNamespace(collections=[])

            def get_aliases(self):
                return types.SimpleNamespace(
                    aliases=(
                        [
                        types.SimpleNamespace(
                            alias_name="memory_claim_v1_active",
                            collection_name="memory_claim_v1_shadow_rebuild_abcdef123456",
                        )
                        ]
                        if self.present
                        else []
                    )
                )

            def get_collection(self, _collection_name):
                if not self.present:
                    raise AssertionError("absent collection must not be inspected")
                return types.SimpleNamespace(
                    config=types.SimpleNamespace(
                        params=types.SimpleNamespace(
                            vectors=types.SimpleNamespace(
                                size=3, distance=self.distance
                            )
                        )
                    ),
                    payload_schema={
                        field: object()
                        for field in (
                            "owner_user_id",
                            "status",
                            "sensitivity",
                            "domains",
                            "intents",
                        )
                    },
                )

            def create_collection(self, **_kwargs):
                self.created = True

        with mock.patch(
            "rag_engine.memory_v1_projection.qdrant_mutation_lock",
            side_effect=lambda **_kwargs: contextlib.nullcontext(),
        ) as lock_factory:
            dot = Client("Dot")
            self.assertFalse(
                ClaimVectorIndex(
                    dot, collection_name="memory_claim_v1_active", vector_size=3
                ).ensure_collection()
            )
            self.assertFalse(dot.created)
            with self.assertRaisesRegex(ProjectionError, "unsupported distance"):
                ClaimVectorIndex(
                    Client("Euclid"),
                    collection_name="memory_claim_v1_active",
                    vector_size=3,
                ).ensure_collection()
            absent = Client("Cosine", present=False)
            with self.assertRaisesRegex(ProjectionError, "provisioned explicitly"):
                ClaimVectorIndex(
                    absent,
                    collection_name="memory_claim_v1_active",
                    vector_size=3,
                ).ensure_collection()
            self.assertFalse(absent.created)
        self.assertEqual(
            lock_factory.call_args_list,
            [mock.call(exclusive=True), mock.call(exclusive=True), mock.call(exclusive=True)],
        )

    def test_retrieval_accepts_validated_v1_and_v2_points_only(self) -> None:
        class Client:
            def __init__(self, schema_version: str) -> None:
                self.schema_version = schema_version

            def search(self, **kwargs):
                return [
                    types.SimpleNamespace(
                        id=CLAIM_A,
                        score=0.75,
                        payload={
                            "owner_user_id": OWNER_A,
                            "claim_id": CLAIM_A,
                            "status": "supported",
                            "schema_version": self.schema_version,
                        },
                    )
                ]

        for version in (
            "memory_claim_projection_v1",
            "memory_claim_projection_v2",
            "memory_claim_projection_v3",
        ):
            self.assertEqual(
                ClaimVectorIndex(Client(version), vector_size=3).search_claims(
                    OWNER_A, [1.0, 0.0, 0.5]
                )[0]["claim_id"],
                CLAIM_A,
            )

    def test_supported_incremental_upsert_uses_truthful_v3_provenance(self) -> None:
        snapshot = source()
        payload = projection_payload(
            uuid.UUID(OWNER_A),
            snapshot,
            embedding=[0.01] * 3072,
            renderer_sha256=projection_renderer_sha256(),
        )
        self.assertEqual(payload["schema_version"], "memory_claim_projection_v3")
        self.assertEqual(payload["dimensions"], 3072)
        self.assertEqual(payload["embedding_model"], "text-embedding-3-large")
        self.assertRegex(payload["source_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(payload["vector_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("rebuild_run_id", payload)
        self.assertNotIn("source_snapshot_sha256", payload)

    def test_incremental_renderer_digest_is_frozen_at_process_import(self) -> None:
        frozen = projection_renderer_sha256()
        self.assertRegex(frozen, r"^[0-9a-f]{64}$")
        with mock.patch.object(pathlib.Path, "read_bytes", return_value=b"later bytes"):
            self.assertEqual(projection_renderer_sha256(), frozen)

    def test_quality_gate_reports_rank_margin_and_threshold(self) -> None:
        report = conversational_quality_report(
            ranks=[1] * 27 + [2, 3, 4],
            target_scores=[0.82] * 27 + [0.75, 0.74, 0.73],
            negative_scores=[0.31, 0.29, 0.25],
        )
        self.assertTrue(report["accepted"])
        self.assertGreater(report["margin_ppm"], 20_000)
        self.assertGreater(report["recommended_score_threshold_ppm"], 0)

    def test_quality_gate_rejects_weak_or_nonseparating_retrieval(self) -> None:
        report = conversational_quality_report(
            ranks=[1, None, None, 7, 8, None],
            target_scores=[0.55, 0.0, 0.0, 0.42, 0.41, 0.0],
            negative_scores=[0.54, 0.50],
        )
        self.assertFalse(report["accepted"])

    def test_shadow_name_and_atomic_alias_actions_are_narrow(self) -> None:
        shadow = "memory_claim_v1_shadow_rebuild_abcdef123456"
        self.assertEqual(validate_shadow_collection(shadow), shadow)
        cutover = alias_transition(
            alias_name=ACTIVE_ALIAS,
            expected_source="memory_claim_v1",
            target=shadow,
            observed={ACTIVE_ALIAS: "memory_claim_v1"},
        )
        self.assertEqual(cutover[0], {"delete_alias": {"alias_name": ACTIVE_ALIAS}})
        self.assertEqual(cutover[1]["create_alias"]["collection_name"], shadow)
        rollback = alias_transition(
            alias_name=ACTIVE_ALIAS,
            expected_source=shadow,
            target="memory_claim_v1",
            observed={ACTIVE_ALIAS: shadow},
        )
        self.assertEqual(rollback[1]["create_alias"]["collection_name"], "memory_claim_v1")
        with self.assertRaises(RebuildContractError):
            alias_transition(
                alias_name=ACTIVE_ALIAS,
                expected_source="different",
                target=shadow,
                observed={ACTIVE_ALIAS: "different"},
            )

    def test_manifest_hash_requires_contract(self) -> None:
        value = {"contract_version": CONTRACT_VERSION, "claim_count": 2}
        self.assertEqual(len(manifest_sha256(value)), 64)
        with self.assertRaises(RebuildContractError):
            manifest_sha256({"contract_version": "unknown"})

    def test_collection_fingerprint_binds_payload_and_vector_bytes(self) -> None:
        point = {
            "id": CLAIM_A,
            "payload": {"status": "supported", "source_sha256": HASH_A},
            "stored_vector_sha256": HASH_B,
        }
        baseline = collection_fingerprint_sha256(
            collection="memory_claim_v1_shadow_rebuild_abcdef123456",
            dimensions=3072,
            distance="cosine",
            points=[point],
            payload_indexes={"owner_user_id": "keyword"},
        )
        changed = copy.deepcopy(point)
        changed["payload"]["source_sha256"] = HASH_C
        self.assertNotEqual(
            baseline,
            collection_fingerprint_sha256(
                collection="memory_claim_v1_shadow_rebuild_abcdef123456",
                dimensions=3072,
                distance="cosine",
                points=[changed],
                payload_indexes={"owner_user_id": "keyword"},
            ),
        )
        self.assertNotEqual(
            baseline,
            collection_fingerprint_sha256(
                collection="memory_claim_v1_shadow_rebuild_abcdef123456",
                dimensions=3072,
                distance="cosine",
                points=[point],
                payload_indexes={"owner_user_id": "uuid"},
            ),
        )


if __name__ == "__main__":
    unittest.main()
