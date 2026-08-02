from __future__ import annotations

import hashlib
import io
import json
import os
import pathlib
import re
import stat
import struct
import tempfile
import types
import unittest
import uuid
from unittest import mock

from scripts import memory_v1_qdrant_alias_cutover_v1 as cutover
from scripts import memory_v1_qdrant_shadow_rebuild_v1 as rebuild
from scripts import memory_v1_projection_worker as projection_worker
from scripts import memory_v1_v5_activation_readiness as readiness
from rag_engine import memory_v1_qdrant_rebuild_contract_v1 as rebuild_contract


HEAD = "a" * 40
TREE = "b" * 40
HASH = "c" * 64
SHADOW = "memory_claim_v1_shadow_rebuild_abcdef123456"
SHADOW_2 = "memory_claim_v1_shadow_rebuild_123456abcdef"
DOT_VECTOR = rebuild_contract.normalize_cosine_vector([0.01] * 3072)
EFFECTIVE_SETTINGS = {
    "MEMORY_V1_COLLECTION": "memory_claim_v1_active",
    "MEMORY_V1_V5_SHADOW": "1",
    "MEMORY_V1_V5_SHADOW_TRACE_PERSISTENCE": "1",
    "MEMORY_V1_V5_SHADOW_ALL_AUTHENTICATED": "0",
    "MEMORY_V1_V5_SHADOW_USER_IDS": "11111111-1111-4111-8111-111111111111",
}


def rebuild_spec(report_path: str) -> dict:
    return {
        "schema_version": rebuild.SPEC_VERSION,
        "run_id": "memory-qdrant-rebuild-20260802T050000Z-abcdef123456",
        "expected_git": {"head": HEAD, "tree": TREE},
        "expected_claim_count": 2,
        "expected_owner_count": 2,
        "expected_source_snapshot_sha256": "1" * 64,
        "lease": {
            "lease_id": "qdrant-rebuild-test-lease",
            "task_id": "qdrant-rebuild-test-task",
            "thread_id": "qdrant-rebuild-test-thread",
            "acquire_event_sha256": "d" * 64,
            "registry_revision": 123,
        },
        "postgres_container": "brains-postgres-1",
        "database": "memory",
        "database_user": "sage",
        "renderer_path": str(rebuild.EXPECTED_RENDERER_PATH),
        "renderer_sha256": HASH,
        "embedding_model": "text-embedding-3-large",
        "dimensions": 3072,
        "max_embedding_requests": 1,
        "max_embedding_input_bytes": 1000000,
        "max_embedding_input_tokens": 100000,
        "qdrant_url": "http://127.0.0.1:6333",
        "shadow_collection": SHADOW,
        "report_path": report_path,
        "quality_thresholds": {
            "top1_ppm": 650000,
            "top5_ppm": 900000,
            "mrr_ppm": 750000,
            "minimum_margin_ppm": 20000,
            "positive_above_threshold_ppm": 900000,
        },
    }


def alias_spec(
    *,
    operation: str,
    source: str | None,
    target: str,
    report_path: str | None,
    target_fingerprint: str = HASH,
    source_fingerprint: str | None = HASH,
) -> dict:
    return {
        "schema_version": cutover.SPEC_VERSION,
        "run_id": "memory-qdrant-alias-20260802T050000Z-abcdef123456",
        "operation": operation,
        "expected_git": {"head": HEAD, "tree": TREE},
        "lease": {
            "lease_id": "qdrant-alias-test-lease",
            "task_id": "qdrant-alias-test-task",
            "thread_id": "qdrant-alias-test-thread",
            "acquire_event_sha256": "d" * 64,
            "registry_revision": 123,
        },
        "qdrant_url": "http://127.0.0.1:6333",
        "alias_name": "memory_claim_v1_active",
        "expected_alias_source": source,
        "expected_alias_source_fingerprint_sha256": (
            None if operation == "bootstrap" else source_fingerprint
        ),
        "expected_postgres_projection_inventory_sha256": HASH,
        "expected_postgres_supported_snapshot_sha256": "1" * 64,
        "target_collection": target,
        "target_collection_fingerprint_sha256": target_fingerprint,
        "rebuild_report_path": report_path,
        "rebuild_report_sha256": HASH if report_path else None,
        "output_path": "/home/ubuntu/brains/snapshots/test-run/alias-event.jsonl",
    }


def load_spec(spec_type, value: dict):
    raw = rebuild.canonical_bytes(value)
    temporary = tempfile.TemporaryDirectory()
    path = pathlib.Path(temporary.name) / "spec.json"
    path.write_bytes(raw)
    loaded = spec_type.load(path, hashlib.sha256(raw).hexdigest())
    return temporary, loaded


def shadow_payload(point_id: str, owner: str) -> dict:
    stored_vector_sha256 = hashlib.sha256(
        struct.pack("<" + "f" * 3072, *DOT_VECTOR)
    ).hexdigest()
    return {
        "claim_id": point_id,
        "dimensions": 3072,
        "domains": ["personal"],
        "embedding_model": "text-embedding-3-large",
        "intents": ["recall"],
        "owner_user_id": owner,
        "predicate": "profile.preference",
        "rebuild_run_id": "memory-qdrant-rebuild-20260802T050000Z-abcdef123456",
        "renderer_sha256": "e" * 64,
        "requires_explicit": False,
        "revision_number": 1,
        "schema_version": "memory_claim_projection_v2",
        "sensitivity": "low",
        "source_sha256": "f" * 64,
        "source_snapshot_sha256": "1" * 64,
        "status": "supported",
        "surface": "support",
        "updated_at": "2026-08-02T00:00:00Z",
        "vector_sha256": stored_vector_sha256,
    }


def incremental_payload(
    point_id: str, owner: str, *, version: str = "memory_claim_projection_v1"
) -> dict:
    payload = {
        "claim_id": point_id,
        "domains": ["personal"],
        "intents": ["recall"],
        "owner_user_id": owner,
        "predicate": "profile.preference",
        "requires_explicit": False,
        "revision_number": 1,
        "schema_version": version,
        "sensitivity": "low",
        "status": "supported",
        "surface": "support",
        "updated_at": "2026-08-02T00:00:00Z",
    }
    if version == "memory_claim_projection_v3":
        payload.update(
            {
                "dimensions": 3072,
                "embedding_model": "text-embedding-3-large",
                "renderer_sha256": "e" * 64,
                "source_sha256": "f" * 64,
                "vector_sha256": hashlib.sha256(
                    struct.pack("<" + "f" * 3072, *DOT_VECTOR)
                ).hexdigest(),
            }
        )
        payload["projection_manifest_sha256"] = hashlib.sha256(
            rebuild.canonical_bytes(payload)
        ).hexdigest()
    return payload


class FakeQdrant:
    def __init__(
        self,
        *,
        aliases: dict[str, str],
        target: str,
        behavior: str = "success",
        target_revision: int = 1,
        unrelated: dict[str, str] | None = None,
    ) -> None:
        self.aliases = {**(unrelated or {}), **aliases}
        self.target = target
        self.behavior = behavior
        self.target_revision = target_revision
        self.update_calls = 0
        self.closed = False
        self.audit_path: pathlib.Path | None = None
        self.point_id = "00000000-0000-4000-8000-000000000001"
        self.owner = "00000000-0000-4000-8000-000000000002"

    def get_aliases(self):
        return types.SimpleNamespace(
            aliases=[
                types.SimpleNamespace(alias_name=name, collection_name=collection)
                for name, collection in sorted(self.aliases.items())
            ]
        )

    def get_collections(self):
        names = {"memory_claim_v1", SHADOW, SHADOW_2, self.target}
        return types.SimpleNamespace(
            collections=[types.SimpleNamespace(name=name) for name in sorted(names)]
        )

    def get_collection(self, *, collection_name: str):
        distance = (
            "Dot"
            if collection_name.startswith("memory_claim_v1_shadow_rebuild_")
            else "Cosine"
        )
        vectors = types.SimpleNamespace(size=3072, distance=distance)
        params = types.SimpleNamespace(vectors=vectors)
        config = types.SimpleNamespace(params=params)
        payload_schema = {
            field: types.SimpleNamespace(data_type="keyword")
            for field in rebuild.EXPECTED_PAYLOAD_INDEX_SCHEMA
        }
        return types.SimpleNamespace(
            config=config, points_count=1, payload_schema=payload_schema
        )

    def scroll(self, **kwargs):
        if kwargs.get("offset") is not None:
            return [], None
        collection = kwargs["collection_name"]
        payload = (
            shadow_payload(self.point_id, self.owner)
            if collection.startswith("memory_claim_v1_shadow_rebuild_")
            else incremental_payload(self.point_id, self.owner)
        )
        if collection.startswith("memory_claim_v1_shadow_rebuild_"):
            payload["revision_number"] = self.target_revision
        record = types.SimpleNamespace(
            id=self.point_id,
            payload=payload,
            vector=(
                DOT_VECTOR
                if collection.startswith("memory_claim_v1_shadow_rebuild_")
                else [0.01] * 3072
            ),
        )
        return [record], None

    def update_collection_aliases(self, *, change_aliases_operations, timeout: int):
        self.update_calls += 1
        if self.audit_path is not None:
            durable = [json.loads(line) for line in self.audit_path.read_text().splitlines()]
            if not durable or durable[-1].get("state") != "prepared":
                raise AssertionError("prepared audit event was not durable before update")
        if self.behavior == "reject":
            raise RuntimeError("synthetic rejection")
        for operation in change_aliases_operations:
            deleted = getattr(operation, "delete_alias", None)
            created = getattr(operation, "create_alias", None)
            if deleted is not None:
                self.aliases.pop(str(deleted.alias_name), None)
            if created is not None:
                self.aliases[str(created.alias_name)] = str(created.collection_name)
        if self.behavior == "unrelated_drift":
            self.aliases["unrelated"] = "changed"
        if self.behavior == "commit_then_timeout":
            raise TimeoutError("synthetic timeout")

    def close(self):
        self.closed = True


def accepted_rebuild_report(evidence: dict, collection: str) -> dict:
    claim_count = evidence["point_count"]
    owner_count = evidence["owner_count"]
    negative_count = owner_count * 6
    quality = {
        "accepted": True,
        "contract_version": "memory_v1_qdrant_conversational_quality_v1",
        "evaluated_query_count": claim_count * 3,
        "margin_ppm": 100000,
        "mrr_ppm": 1000000,
        "negative_query_count": negative_count,
        "positive_above_threshold_ppm": 1000000,
        "recommended_score_threshold_ppm": 500000,
        "top1_count": claim_count * 3,
        "top1_ppm": 1000000,
        "top5_count": claim_count * 3,
        "top5_ppm": 1000000,
    }
    return {
        "claim_count": claim_count,
        "collection": collection,
        "collection_fingerprint_sha256": evidence[
            "collection_fingerprint_sha256"
        ],
        "contract_version": "memory_v1_qdrant_shadow_rebuild_report_v1",
        "cross_owner_check_count": claim_count * 3 * (owner_count - 1),
        "dimensions": evidence["dimensions"],
        "distance": evidence["distance"],
        "embedding_input_bytes": 100,
        "embedding_input_tokens": 10,
        "embedding_model": evidence["embedding_model"],
        "embedding_request_count": 1,
        "expected_git": {"head": HEAD, "tree": TREE},
        "lifecycle_check_count": 4,
        "negative_check_count": negative_count,
        "owner_count": owner_count,
        "payload_index_schema_sha256": evidence[
            "payload_index_schema_sha256"
        ],
        "point_inventory_sha256": evidence["point_inventory_sha256"],
        "production_postgres_read_only": True,
        "quality": quality,
        "renderer_sha256": evidence["renderer_sha256"],
        "run_id": "memory-qdrant-rebuild-20260802T050000Z-abcdef123456",
        "source_snapshot_sha256": evidence["source_snapshot_sha256"],
        "spec_sha256": "3" * 64,
    }


def parse_audit(path: pathlib.Path) -> list[dict]:
    events = [json.loads(line) for line in path.read_text().splitlines()]
    prior = None
    for sequence, event in enumerate(events, start=1):
        stored = event.pop("event_sha256")
        assert event["audit_sequence"] == sequence
        assert event["prior_event_sha256"] == prior
        assert hashlib.sha256(rebuild.canonical_bytes(event)).hexdigest() == stored
        prior = stored
        event["event_sha256"] = stored
    return events


class RebuildScriptContractTests(unittest.TestCase):
    def test_activation_consumers_follow_alias_and_authenticated_owners(self) -> None:
        repository = pathlib.Path(rebuild.__file__).resolve().parents[1]
        worker = (repository / "scripts/memory_v1_projection_worker.py").read_text()
        readiness = (
            repository / "scripts/memory_v1_v5_activation_readiness.py"
        ).read_text()
        projection = (repository / "rag_engine/memory_v1_projection.py").read_text()
        deletion = (repository / "rag_engine/thread_deletion_v1.py").read_text()
        self.assertIn("resolve_authenticated_owners(dsn, [])", worker)
        self.assertIn("{*configured, *discovered}", worker)
        self.assertIn("EFFECTIVE_MEMORY_SETTINGS", readiness)
        self.assertIn("_running_service_memory_settings", readiness)
        self.assertIn("effective_env", readiness)
        self.assertIn("memory_claim_projection_v2", projection)
        self.assertIn("memory_claim_projection_v3", projection)
        self.assertIn('os.environ.get("MEMORY_V1_COLLECTION"', deletion)
        self.assertIn(
            'default=os.environ.get("MEMORY_V1_COLLECTION")', worker
        )
        self.assertNotIn("DEFAULT_COLLECTION", worker)
        self.assertNotIn("_DEFAULT_CLAIM_COLLECTION", deletion)

    def test_runbook_bridges_timer_before_git_and_restart(self) -> None:
        repository = pathlib.Path(rebuild.__file__).resolve().parents[1]
        runbook = (
            repository / "docs/MEMORY_V1_QDRANT_SHADOW_REBUILD_V1.md"
        ).read_text()
        bridge = runbook.index("Before any Git fast-forward")
        timer_proof = runbook.index("observe its next scheduled two-minute invocation")
        integration = runbook.index("Only after that bridge proof, fast-forward")
        restart = runbook.index("Restart Brains exactly once")
        cutover = runbook.index("atomically replace the exact observed alias source")
        self.assertLess(bridge, timer_proof)
        self.assertLess(timer_proof, integration)
        self.assertLess(integration, restart)
        self.assertLess(restart, cutover)
        self.assertIn(
            "first restore the primary environment to its exact prior state with the collection key absent",
            runbook,
        )
        self.assertIn("remove only the newly created", runbook)
        self.assertIn("Do not stop or mask", runbook)
        self.assertIn("No Brains restart is required", runbook)

    def test_guard_and_source_binding_precede_provider_and_qdrant_mutation(self) -> None:
        source_text = pathlib.Path(rebuild.__file__).read_text()
        execute = source_text[source_text.index("def execute(spec: Spec)") :]
        self.assertLess(
            execute.index('snapshot_sha != spec.value["expected_source_snapshot_sha256"]'),
            execute.index("provider = OpenAI"),
        )
        self.assertLess(
            execute.index('require_production_write_guard(spec.value["lease"])'),
            execute.index("provider = OpenAI"),
        )
        self.assertGreaterEqual(
            execute.count('require_production_write_guard(spec.value["lease"])'),
            5,
        )

    def test_real_central_guard_denial_precedes_provider_qdrant_and_report(self) -> None:
        first = rebuild.SupportedClaimSnapshotV1.from_mapping(
            {
                "owner_user_id": "11111111-1111-4111-8111-111111111111",
                "claim_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "canonical_text": "I prefer quiet morning walks near the lake.",
                "predicate": "preference.activity",
                "qualifiers": {"time": "morning"},
                "status": "supported",
                "sensitivity": "low",
                "retrieval_policy": {"domains": ["personal"]},
                "updated_at": "2026-08-02T04:00:00+00:00",
                "revision_number": 1,
            }
        )
        second = rebuild.SupportedClaimSnapshotV1.from_mapping(
            {
                **first.source_record(),
                "owner_user_id": "22222222-2222-4222-8222-222222222222",
                "claim_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            }
        )
        value = rebuild_spec(
            "/home/ubuntu/brains/snapshots/test-run/rebuild.json"
        )
        value["expected_source_snapshot_sha256"] = rebuild.source_snapshot_sha256(
            [first, second]
        )
        value["renderer_sha256"] = hashlib.sha256(
            rebuild.EXPECTED_RENDERER_PATH.read_bytes()
        ).hexdigest()
        spec = rebuild.Spec(value=value, raw=rebuild.canonical_bytes(value))
        runtime_identity = {
            "CHAT_MEMORY_LEASE_ID": value["lease"]["lease_id"],
            "CODEX_TASK_ID": value["lease"]["task_id"],
            "CODEX_THREAD_ID": value["lease"]["thread_id"],
        }
        denied = types.SimpleNamespace(returncode=2, stdout=b"", stderr=b"denied")
        with mock.patch.object(
            rebuild, "git_identity", return_value=value["expected_git"]
        ), mock.patch.object(
            rebuild, "load_supported_claims", return_value=[first, second]
        ), mock.patch.object(
            rebuild_contract, "validate_lease_acquire_event", return_value={}
        ), mock.patch.object(
            pathlib.Path, "is_file", return_value=True
        ), mock.patch.object(
            rebuild_contract.subprocess, "run", return_value=denied
        ) as central_guard, mock.patch.dict(
            os.environ, runtime_identity, clear=True
        ), mock.patch.object(
            rebuild, "OpenAI"
        ) as provider, mock.patch.object(
            rebuild, "make_qdrant_client"
        ) as qdrant, mock.patch.object(
            rebuild, "write_new_private"
        ) as report, self.assertRaises(rebuild.RebuildContractError):
            rebuild.execute(spec)
        central_guard.assert_called_once()
        provider.assert_not_called()
        qdrant.assert_not_called()
        report.assert_not_called()

    def test_source_plan_is_read_only_aggregate_only(self) -> None:
        first = rebuild.SupportedClaimSnapshotV1.from_mapping(
            {
                "owner_user_id": "11111111-1111-4111-8111-111111111111",
                "claim_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "canonical_text": "I prefer quiet morning walks near the lake.",
                "predicate": "preference.activity",
                "qualifiers": {},
                "status": "supported",
                "sensitivity": "low",
                "retrieval_policy": {},
                "updated_at": "2026-08-02T04:00:00+00:00",
                "revision_number": 1,
            }
        )
        with mock.patch.object(
            rebuild, "_load_current_supported_claims", return_value=[first]
        ), mock.patch.object(rebuild, "OpenAI") as provider, mock.patch.object(
            rebuild, "make_qdrant_client"
        ) as qdrant, mock.patch.object(
            rebuild, "require_production_write_guard"
        ) as guard, mock.patch.object(
            rebuild, "write_new_private"
        ) as report:
            plan = rebuild.source_plan()
        self.assertEqual(
            set(plan),
            {
                "claim_count",
                "embedding_input_bytes",
                "owner_count",
                "renderer_sha256",
                "required_embedding_request_count",
                "source_snapshot_sha256",
            },
        )
        self.assertEqual(plan["claim_count"], 1)
        provider.assert_not_called()
        qdrant.assert_not_called()
        guard.assert_not_called()
        report.assert_not_called()

    def test_rebuild_rejects_loaded_renderer_release_byte_mismatch(self) -> None:
        with mock.patch.object(
            rebuild, "projection_renderer_sha256", return_value=HASH
        ), mock.patch.object(
            pathlib.Path, "read_bytes", return_value=b"changed release bytes"
        ), self.assertRaisesRegex(rebuild.ShadowRebuildError, "loaded renderer"):
            rebuild.loaded_renderer_sha256()

    def test_rebuild_spec_is_canonical_exact_and_shadow_only(self) -> None:
        value = rebuild_spec(
            "/home/ubuntu/brains/snapshots/test-run/rebuild.json"
        )
        temporary, loaded = load_spec(rebuild.Spec, value)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(loaded.value, value)
        for key, changed_value in (
            ("qdrant_url", "http://remote:6333"),
            ("shadow_collection", "memory_claim_v1"),
            ("embedding_model", "other"),
        ):
            changed = {**value, key: changed_value}
            raw = rebuild.canonical_bytes(changed)
            path = pathlib.Path(temporary.name) / f"{key}.json"
            path.write_bytes(raw)
            with self.assertRaises(rebuild.ShadowRebuildError):
                rebuild.Spec.load(path, hashlib.sha256(raw).hexdigest())

    def test_source_sql_is_privileged_but_read_only_and_allowlisted(self) -> None:
        sql = rebuild.SOURCE_SQL.lower()
        self.assertIn("repeatable read read only", sql)
        self.assertIn("where claim.status::text='supported'", sql)
        self.assertIn("from memory.claim as claim", sql)
        self.assertIn("memory.claim_revision", sql)
        for forbidden in (
            "memory_raw",
            "chat_log",
            "memory.evidence",
            "memory.observation",
            " update ",
            " insert ",
            " delete ",
        ):
            self.assertNotIn(forbidden, sql)

    def test_embedding_batches_are_exact_and_have_no_automatic_retry(self) -> None:
        class Embeddings:
            def __init__(self) -> None:
                self.calls = 0

            def create(self, *, model: str, input: list[str]):
                self.calls += 1
                data = [
                    types.SimpleNamespace(index=index, embedding=[0.01] * 3072)
                    for index, _ in enumerate(input)
                ]
                return types.SimpleNamespace(
                    data=data, usage=types.SimpleNamespace(prompt_tokens=len(input))
                )

        fake = types.SimpleNamespace(embeddings=Embeddings())
        vectors, requests, tokens = rebuild.embed_inputs(
            fake, [f"query {index}" for index in range(65)], maximum_requests=2
        )
        self.assertEqual(len(vectors), 65)
        self.assertEqual(requests, 2)
        self.assertEqual(tokens, 65)

        class DuplicateEmbeddings:
            def create(self, *, model: str, input: list[str]):
                return types.SimpleNamespace(
                    data=[
                        types.SimpleNamespace(index=0, embedding=[0.01] * 3072),
                        types.SimpleNamespace(index=0, embedding=[0.02] * 3072),
                    ],
                    usage=types.SimpleNamespace(prompt_tokens=2),
                )

        with self.assertRaises(rebuild.ShadowRebuildError):
            rebuild.embed_inputs(
                types.SimpleNamespace(embeddings=DuplicateEmbeddings()),
                ["first", "second"],
                maximum_requests=1,
            )
        source_text = pathlib.Path(rebuild.__file__).read_text()
        self.assertIn("max_retries=0", source_text)

    def test_dot_quality_gate_normalizes_positive_and_negative_queries(self) -> None:
        claim = rebuild.SupportedClaimSnapshotV1.from_mapping(
            {
                "owner_user_id": "11111111-1111-4111-8111-111111111111",
                "claim_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "canonical_text": "I prefer quiet morning walks near the lake.",
                "predicate": "preference.activity",
                "qualifiers": {},
                "status": "supported",
                "sensitivity": "low",
                "retrieval_policy": {"domains": ["personal"]},
                "updated_at": "2026-08-02T04:00:00+00:00",
                "revision_number": 1,
            }
        )

        class Qdrant:
            def __init__(self) -> None:
                self.queries: list[list[float]] = []

            def search(self, **kwargs):
                query = list(kwargs["query_vector"])
                self.queries.append(query)
                self.assert_unit(query)
                if kwargs["with_payload"]:
                    return [
                        types.SimpleNamespace(id=str(claim.claim_id), score=0.8)
                    ]
                return []

            @staticmethod
            def assert_unit(vector: list[float]) -> None:
                squared_norm = sum(value * value for value in vector)
                if abs(squared_norm - 1.0) > 2e-6:
                    raise AssertionError("query was not normalized")

        raw_query = [3.0, 4.0, *([0.0] * 3070)]
        qdrant = Qdrant()
        report, owner_checks, negative_checks = rebuild.evaluate(
            qdrant,
            SHADOW,
            [claim],
            [raw_query, raw_query, raw_query],
            [raw_query],
            rebuild.QualityThresholdsV1(),
        )
        self.assertTrue(report["accepted"])
        self.assertEqual(owner_checks, 0)
        self.assertEqual(negative_checks, 1)
        self.assertEqual(len(qdrant.queries), 4)

    def test_private_report_is_no_clobber_and_mode_0600(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            root.chmod(0o755)
            run = root / "run"
            run.mkdir(mode=0o700)
            path = run / "report.json"
            rebuild.write_new_private(
                path, {"contract_version": "synthetic"}, trusted_root=root
            )
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(rebuild.ShadowRebuildError):
                rebuild.write_new_private(
                    path, {"contract_version": "changed"}, trusted_root=root
                )

    def test_private_report_rejects_intermediate_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as external:
            root = pathlib.Path(temporary)
            root.chmod(0o755)
            external_path = pathlib.Path(external)
            external_path.chmod(0o700)
            (root / "escape").symlink_to(external_path, target_is_directory=True)
            with self.assertRaises(rebuild.ShadowRebuildError):
                rebuild.write_new_private(
                    root / "escape" / "report.json",
                    {"contract_version": "synthetic"},
                    trusted_root=root,
                )
            self.assertFalse((external_path / "report.json").exists())

    def test_build_failure_cleanup_cannot_target_live_collection(self) -> None:
        source_text = pathlib.Path(rebuild.__file__).read_text()
        self.assertIn("validate_shadow_collection", source_text)
        self.assertIn("qdrant.delete_collection(collection_name=collection)", source_text)
        self.assertNotIn('delete_collection(collection_name="memory_claim_v1")', source_text)

    def test_shadow_cleanup_verifies_exact_collection_absence(self) -> None:
        class CleanupQdrant:
            def __init__(
                self,
                *,
                remove: bool,
                aliased: bool = False,
                foreign_run: bool = False,
                payload_overrides: dict | None = None,
                vector_value: float | None = None,
                points_count: int = 1,
            ) -> None:
                self.names = {SHADOW, "memory_claim_v1"}
                self.remove = remove
                self.aliased = aliased
                self.foreign_run = foreign_run
                self.payload_overrides = payload_overrides or {}
                self.vector_value = vector_value
                self.points_count = points_count
                self.deleted: list[str] = []

            def get_collections(self):
                return types.SimpleNamespace(
                    collections=[types.SimpleNamespace(name=name) for name in self.names]
                )

            def get_aliases(self):
                aliases = []
                if self.aliased:
                    aliases.append(
                        types.SimpleNamespace(
                            alias_name="memory_claim_v1_active",
                            collection_name=SHADOW,
                        )
                    )
                return types.SimpleNamespace(aliases=aliases)

            def get_collection(self, *, collection_name: str):
                vectors = types.SimpleNamespace(size=3072, distance="Dot")
                return types.SimpleNamespace(
                    config=types.SimpleNamespace(
                        params=types.SimpleNamespace(vectors=vectors)
                    ),
                    points_count=self.points_count,
                    payload_schema={
                        field: types.SimpleNamespace(data_type="keyword")
                        for field in rebuild.EXPECTED_PAYLOAD_INDEX_SCHEMA
                    },
                )

            def scroll(self, **kwargs):
                payload = shadow_payload(
                    "00000000-0000-4000-8000-000000000001",
                    "00000000-0000-4000-8000-000000000002",
                )
                payload["renderer_sha256"] = HASH
                if self.foreign_run:
                    payload["rebuild_run_id"] = (
                        "memory-qdrant-rebuild-20260802T050000Z-ffffffffffff"
                    )
                payload.update(self.payload_overrides)
                return [
                    types.SimpleNamespace(
                        id="00000000-0000-4000-8000-000000000001",
                        payload=payload,
                        vector=(
                            list(DOT_VECTOR)
                            if self.vector_value is None
                            else [self.vector_value] * 3072
                        ),
                    )
                ], None

            def delete_collection(self, *, collection_name: str):
                self.deleted.append(collection_name)
                if self.remove:
                    self.names.remove(collection_name)
                return self.remove

        spec_value = rebuild_spec(
            "/home/ubuntu/brains/snapshots/test-run/rebuild.json"
        )
        spec = rebuild.Spec(value=spec_value, raw=rebuild.canonical_bytes(spec_value))
        lock_patch = mock.patch.object(
            rebuild, "qdrant_mutation_lock", return_value=mock.Mock()
        )
        lock_patch.start()
        self.addCleanup(lock_patch.stop)
        full_indexes = rebuild.EXPECTED_PAYLOAD_INDEX_SCHEMA
        first_payload = shadow_payload(
            "00000000-0000-4000-8000-000000000001",
            "00000000-0000-4000-8000-000000000002",
        )
        first_payload["renderer_sha256"] = HASH
        second_payload = shadow_payload(
            "00000000-0000-4000-8000-000000000003",
            "00000000-0000-4000-8000-000000000004",
        )
        second_payload["renderer_sha256"] = HASH
        expected_vector_sha256 = rebuild.vector_sha256(DOT_VECTOR)
        expected_manifest = frozenset(
            {
                (
                    "00000000-0000-4000-8000-000000000001",
                    "00000000-0000-4000-8000-000000000002",
                    "f" * 64,
                    hashlib.sha256(rebuild.canonical_bytes(first_payload)).hexdigest(),
                    expected_vector_sha256,
                ),
                (
                    "00000000-0000-4000-8000-000000000003",
                    "00000000-0000-4000-8000-000000000004",
                    "a" * 64,
                    hashlib.sha256(rebuild.canonical_bytes(second_payload)).hexdigest(),
                    expected_vector_sha256,
                ),
            }
        )
        source_manifest = frozenset(
            {
                (
                    "00000000-0000-4000-8000-000000000001",
                    "00000000-0000-4000-8000-000000000002",
                    "f" * 64,
                ),
                (
                    "00000000-0000-4000-8000-000000000003",
                    "00000000-0000-4000-8000-000000000004",
                    "a" * 64,
                ),
            }
        )
        source_patch = mock.patch.object(
            rebuild,
            "current_cleanup_source_manifest",
            return_value=source_manifest,
        )
        source_patch.start()
        self.addCleanup(source_patch.stop)
        successful = CleanupQdrant(remove=True)
        with mock.patch.object(
            rebuild, "require_production_write_guard"
        ) as cleanup_guard:
            rebuild.remove_created_shadow(
                successful,
                SHADOW,
                spec,
                expected_manifest,
                source_manifest,
                full_indexes,
            )
        self.assertEqual(successful.deleted, [SHADOW])
        self.assertEqual(cleanup_guard.call_count, 2)
        self.assertIn("memory_claim_v1", successful.names)

        failed = CleanupQdrant(remove=False)
        with mock.patch.object(
            rebuild, "require_production_write_guard"
        ), self.assertRaises(rebuild.ShadowRebuildError):
            rebuild.remove_created_shadow(
                failed, SHADOW, spec, expected_manifest, source_manifest, full_indexes
            )
        self.assertIn(SHADOW, failed.names)

        for unsafe in (
            CleanupQdrant(remove=True, aliased=True),
            CleanupQdrant(remove=True, foreign_run=True),
            CleanupQdrant(
                remove=True,
                payload_overrides={
                    "owner_user_id": "00000000-0000-4000-8000-000000000099"
                },
            ),
            CleanupQdrant(
                remove=True,
                payload_overrides={"source_sha256": "9" * 64},
            ),
            CleanupQdrant(remove=True, vector_value=0.02),
            CleanupQdrant(
                remove=True,
                vector_value=0.02,
                payload_overrides={
                    "vector_sha256": rebuild.vector_sha256([0.02] * 3072)
                },
            ),
            CleanupQdrant(remove=True, points_count=0),
        ):
            with self.subTest(unsafe=unsafe), mock.patch.object(
                rebuild, "require_production_write_guard"
            ), self.assertRaises(rebuild.ShadowRebuildError):
                rebuild.remove_created_shadow(
                    unsafe,
                    SHADOW,
                    spec,
                    expected_manifest,
                    source_manifest,
                    full_indexes,
                )
            self.assertEqual(unsafe.deleted, [])

        with mock.patch.object(
            rebuild, "require_production_write_guard"
        ), self.assertRaises(rebuild.ShadowRebuildError):
            rebuild.remove_created_shadow(
                successful,
                "memory_claim_v1",
                spec,
                expected_manifest,
                source_manifest,
                full_indexes,
            )

        denied = CleanupQdrant(remove=True)
        with mock.patch.object(
            rebuild,
            "require_production_write_guard",
            side_effect=rebuild.RebuildContractError("central denial"),
        ), self.assertRaises(rebuild.ShadowRebuildError):
            rebuild.remove_created_shadow(
                denied,
                SHADOW,
                spec,
                expected_manifest,
                source_manifest,
                full_indexes,
            )
        self.assertEqual(denied.deleted, [])

        drifted = CleanupQdrant(remove=True)
        with mock.patch.object(
            rebuild,
            "current_cleanup_source_manifest",
            side_effect=[source_manifest, frozenset()],
        ), mock.patch.object(
            rebuild, "require_production_write_guard"
        ), self.assertRaises(rebuild.ShadowRebuildError):
            rebuild.remove_created_shadow(
                drifted,
                SHADOW,
                spec,
                expected_manifest,
                source_manifest,
                full_indexes,
            )
        self.assertEqual(drifted.deleted, [])

    def test_ambiguous_create_reconciliation_accepts_only_exact_empty_state(self) -> None:
        class EmptyQdrant:
            def __init__(self, *, present: bool, point_count: int = 0) -> None:
                self.present = present
                self.point_count = point_count

            def get_collections(self):
                names = [SHADOW] if self.present else []
                return types.SimpleNamespace(
                    collections=[types.SimpleNamespace(name=name) for name in names]
                )

            def get_aliases(self):
                return types.SimpleNamespace(aliases=[])

            def get_collection(self, *, collection_name: str):
                vectors = types.SimpleNamespace(size=3072, distance="Dot")
                return types.SimpleNamespace(
                    config=types.SimpleNamespace(
                        params=types.SimpleNamespace(vectors=vectors)
                    ),
                    points_count=self.point_count,
                    payload_schema={},
                )

            def scroll(self, **kwargs):
                if not self.point_count:
                    return [], None
                point_id = "00000000-0000-4000-8000-000000000001"
                payload = shadow_payload(
                    point_id, "00000000-0000-4000-8000-000000000002"
                )
                return [
                    types.SimpleNamespace(
                        id=point_id, payload=payload, vector=[0.01] * 3072
                    )
                ], None

        value = rebuild_spec("/home/ubuntu/brains/snapshots/test-run/rebuild.json")
        spec = rebuild.Spec(value=value, raw=rebuild.canonical_bytes(value))
        manifest = frozenset(
            {
                (
                    "00000000-0000-4000-8000-000000000001",
                    "00000000-0000-4000-8000-000000000002",
                    "f" * 64,
                )
            }
        )
        with mock.patch.object(
            rebuild, "require_production_write_guard"
        ), mock.patch.object(
            rebuild, "current_cleanup_source_manifest", return_value=manifest
        ):
            self.assertFalse(
                rebuild.reconcile_ambiguous_create(
                    EmptyQdrant(present=False), SHADOW, spec, manifest
                )
            )
            with self.assertRaisesRegex(rebuild.ShadowRetainError, "retain"):
                rebuild.reconcile_ambiguous_create(
                    EmptyQdrant(present=True), SHADOW, spec, manifest
                )
            with self.assertRaises(rebuild.ShadowRebuildError):
                rebuild.reconcile_ambiguous_create(
                    EmptyQdrant(present=True, point_count=1),
                    SHADOW,
                    spec,
                    manifest,
                )

    def test_reversible_real_lifecycle_restores_exact_shadow_identity(self) -> None:
        point_id = "00000000-0000-4000-8000-000000000001"
        owner_id = "00000000-0000-4000-8000-000000000002"
        original_vector = list(DOT_VECTOR)
        original_payload = shadow_payload(point_id, owner_id)
        original_payload["renderer_sha256"] = HASH

        class LifecycleQdrant:
            def __init__(self) -> None:
                self.point = types.SimpleNamespace(
                    id=point_id,
                    payload=dict(original_payload),
                    vector=list(original_vector),
                )
                self.operations: list[str] = []

            def get_collection(self, *, collection_name: str):
                vectors = types.SimpleNamespace(size=3072, distance="Dot")
                return types.SimpleNamespace(
                    config=types.SimpleNamespace(
                        params=types.SimpleNamespace(vectors=vectors)
                    ),
                    points_count=0 if self.point is None else 1,
                    payload_schema={
                        field: types.SimpleNamespace(data_type="keyword")
                        for field in rebuild.EXPECTED_PAYLOAD_INDEX_SCHEMA
                    },
                )

            def scroll(self, **kwargs):
                return ([] if self.point is None else [self.point]), None

            def retrieve(self, **kwargs):
                return [] if self.point is None else [self.point]

            def upsert(self, *, collection_name: str, wait: bool, points: list):
                incoming = points[0]
                self.point = types.SimpleNamespace(
                    id=str(incoming.id),
                    payload=dict(incoming.payload),
                    vector=list(incoming.vector),
                )
                self.operations.append("upsert")
                return True

            def delete(self, **kwargs):
                self.point = None
                self.operations.append("delete")
                return True

        value = rebuild_spec("/home/ubuntu/brains/snapshots/test-run/rebuild.json")
        value["renderer_sha256"] = HASH
        spec = rebuild.Spec(value=value, raw=rebuild.canonical_bytes(value))
        fake = LifecycleQdrant()
        inventory, fingerprint, index_sha = rebuild.exact_point_inventory(
            fake, SHADOW
        )
        with mock.patch.object(rebuild, "require_production_write_guard") as guard:
            checks = rebuild.reversible_lifecycle_gate(
                fake,
                SHADOW,
                fake.point,
                spec,
                original_inventory=inventory,
                original_fingerprint=fingerprint,
                original_payload_index_sha256=index_sha,
            )
        self.assertEqual(checks, 4)
        self.assertEqual(fake.operations, ["upsert", "upsert", "delete", "upsert"])
        self.assertEqual(fake.point.payload, original_payload)
        self.assertEqual(fake.point.vector, original_vector)
        self.assertEqual(guard.call_count, 4)

    def test_payload_index_completion_accepts_qdrant_result_not_only_bool(self) -> None:
        completed = types.SimpleNamespace(
            status=types.SimpleNamespace(value="completed")
        )
        rebuild.require_completed_update(completed, "payload index creation")
        rebuild.require_completed_update(True, "payload index creation")
        for rejected in (
            False,
            None,
            types.SimpleNamespace(status=types.SimpleNamespace(value="acknowledged")),
        ):
            with self.subTest(rejected=rejected), self.assertRaises(
                rebuild.ShadowRebuildError
            ):
                rebuild.require_completed_update(rejected, "payload index creation")

    def test_full_shadow_mutation_window_uses_one_exclusive_lock(self) -> None:
        source = pathlib.Path(rebuild.__file__).read_text(encoding="utf-8")
        execute = source[source.index("def execute(spec: Spec)") :]
        self.assertLess(
            execute.index("mutation_lock.acquire()"),
            execute.index("qdrant.get_collections()"),
        )
        self.assertIn("lock_held=True", execute)
        self.assertIn("mutation_lock.release()", execute)


class ProjectionWorkerContractTests(unittest.IsolatedAsyncioTestCase):
    def test_projection_worker_requires_explicit_collection(self) -> None:
        args = types.SimpleNamespace(
            collection=None,
            lease_seconds=600,
            limit=25,
            max_attempts=8,
            vector_size=3072,
        )
        with self.assertRaisesRegex(RuntimeError, "MEMORY_V1_COLLECTION"):
            projection_worker._validate_limits(args)

    async def test_explicit_cli_owners_are_exact_and_skip_discovery(self) -> None:
        first = "11111111-1111-4111-8111-111111111111"
        second = "22222222-2222-4222-8222-222222222222"
        args = types.SimpleNamespace(owner_user_id=[first])
        resolver = mock.AsyncMock(return_value=[projection_worker.uuid.UUID(second)])
        with mock.patch.dict(
            os.environ,
            {"MEMORY_V1_PROJECTION_OWNER_IDS": second},
            clear=False,
        ), mock.patch.object(
            projection_worker, "resolve_authenticated_owners", new=resolver
        ):
            owners = await projection_worker._owners(args, "postgresql://loopback")
        self.assertEqual([str(owner) for owner in owners], [first])
        resolver.assert_not_awaited()

    async def test_scheduled_configured_and_discovered_owners_are_unioned(self) -> None:
        first = "11111111-1111-4111-8111-111111111111"
        second = "22222222-2222-4222-8222-222222222222"
        third = "33333333-3333-4333-8333-333333333333"
        args = types.SimpleNamespace(owner_user_id=[])
        with mock.patch.dict(
            os.environ,
            {"MEMORY_V1_PROJECTION_OWNER_IDS": f"{first},{second}"},
            clear=False,
        ), mock.patch.object(
            projection_worker,
            "resolve_authenticated_owners",
            new=mock.AsyncMock(return_value=[projection_worker.uuid.UUID(third)]),
        ):
            owners = await projection_worker._owners(args, "postgresql://loopback")
        self.assertEqual([str(owner) for owner in owners], [first, second, third])

    async def test_scheduled_owner_resolution_failure_is_not_downgraded(self) -> None:
        args = types.SimpleNamespace(owner_user_id=[])
        with mock.patch.dict(
            os.environ, {"MEMORY_V1_PROJECTION_OWNER_IDS": ""}, clear=False
        ), mock.patch.object(
            projection_worker,
            "resolve_authenticated_owners",
            new=mock.AsyncMock(side_effect=RuntimeError("resolver failed")),
        ), self.assertRaisesRegex(RuntimeError, "resolver failed"):
            await projection_worker._owners(args, "postgresql://loopback")

    async def test_combined_scheduled_owner_bound_is_enforced(self) -> None:
        configured = "11111111-1111-4111-8111-111111111111"
        discovered = [
            projection_worker.uuid.UUID(int=index + 10_000)
            for index in range(projection_worker.MAX_AUTHENTICATED_OWNERS)
        ]
        args = types.SimpleNamespace(owner_user_id=[])
        with mock.patch.dict(
            os.environ,
            {"MEMORY_V1_PROJECTION_OWNER_IDS": configured},
            clear=False,
        ), mock.patch.object(
            projection_worker,
            "resolve_authenticated_owners",
            new=mock.AsyncMock(return_value=discovered),
        ), self.assertRaisesRegex(RuntimeError, "owner count exceeds"):
            await projection_worker._owners(args, "postgresql://loopback")


class ReadinessCollectionResolutionTests(unittest.TestCase):
    def args(self, collection=None, url=None):
        return types.SimpleNamespace(
            qdrant_collection=collection, qdrant_scroll_url=url
        )

    def settings(self, **overrides):
        return {**EFFECTIVE_SETTINGS, **overrides}

    def test_readiness_rejects_postgres_drift_across_qdrant_scan(self) -> None:
        opening = {
            "retrievable_claims": [
                {
                    "claim_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "owner_user_id": "11111111-1111-4111-8111-111111111111",
                    "revision_number": 1,
                    "status": "supported",
                }
            ]
        }
        readiness._require_stable_database_snapshot(opening, dict(opening))
        closing = json.loads(json.dumps(opening))
        closing["retrievable_claims"][0]["revision_number"] = 2
        with self.assertRaisesRegex(RuntimeError, "changed during Qdrant"):
            readiness._require_stable_database_snapshot(opening, closing)

    def test_env_file_collection_wins_only_when_runtime_sources_agree(self) -> None:
        alias = "memory_claim_v1_active"
        selected, url = readiness._resolve_qdrant_target(
            self.args(),
            self.settings(),
            service_env={"MEMORY_V1_COLLECTION": alias},
            process_env=self.settings(),
        )
        self.assertEqual(selected, alias)
        self.assertEqual(
            url,
            f"http://127.0.0.1:6333/collections/{alias}/points/scroll",
        )
        with self.assertRaisesRegex(RuntimeError, "settings disagree"):
            readiness._resolve_qdrant_target(
                self.args(alias),
                self.settings(),
                service_env={"MEMORY_V1_COLLECTION": "memory_claim_v1"},
                process_env=self.settings(),
            )

    def test_missing_or_conflicting_runtime_collection_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "primary.*missing or blank"):
            readiness._resolve_qdrant_target(
                self.args(), {}, service_env={}, process_env={}
            )
        with self.assertRaisesRegex(RuntimeError, "settings disagree"):
            readiness._resolve_qdrant_target(
                self.args(),
                self.settings(),
                service_env={"MEMORY_V1_COLLECTION": "memory_claim_v1"},
                process_env=self.settings(),
            )
        with self.assertRaisesRegex(RuntimeError, "primary.*missing or blank"):
            readiness._resolve_qdrant_target(
                self.args(),
                self.settings(MEMORY_V1_COLLECTION=""),
                service_env={},
                process_env=self.settings(),
            )

    def test_all_effective_settings_reject_override_and_missing_values(self) -> None:
        self.assertEqual(
            readiness._resolve_effective_memory_settings(
                self.settings(), {}, self.settings()
            ),
            self.settings(),
        )
        with self.assertRaisesRegex(RuntimeError, "settings disagree"):
            readiness._resolve_effective_memory_settings(
                self.settings(),
                {"MEMORY_V1_V5_SHADOW_TRACE_PERSISTENCE": "0"},
                self.settings(),
            )
        missing_primary = self.settings()
        missing_primary.pop("MEMORY_V1_V5_SHADOW_ALL_AUTHENTICATED")
        with self.assertRaisesRegex(RuntimeError, "primary.*missing or blank"):
            readiness._resolve_effective_memory_settings(
                missing_primary, {}, self.settings()
            )
        missing_process = self.settings()
        missing_process.pop("MEMORY_V1_V5_SHADOW_USER_IDS")
        with self.assertRaisesRegex(RuntimeError, "running service.*missing or blank"):
            readiness._resolve_effective_memory_settings(
                self.settings(), {}, missing_process
            )

    def test_final_readiness_rejects_physical_collection_names(self) -> None:
        for physical in ("memory_claim_v1", SHADOW):
            with self.subTest(physical=physical), self.assertRaisesRegex(
                RuntimeError, "requires exact alias"
            ):
                readiness._resolve_qdrant_target(
                    self.args(),
                    self.settings(MEMORY_V1_COLLECTION=physical),
                    service_env={},
                    process_env=self.settings(MEMORY_V1_COLLECTION=physical),
                )

    def test_scroll_url_must_match_selected_collection_exactly(self) -> None:
        alias = "memory_claim_v1_active"
        exact = f"http://127.0.0.1:6333/collections/{alias}/points/scroll"
        self.assertEqual(
            readiness._resolve_qdrant_target(
                self.args(alias, exact),
                self.settings(),
                service_env={},
                process_env=self.settings(),
            ),
            (alias, exact),
        )
        with self.assertRaisesRegex(RuntimeError, "scroll URL differs"):
            readiness._resolve_qdrant_target(
                self.args(alias, exact.replace(alias, "memory_claim_v1")),
                self.settings(),
                service_env={},
                process_env=self.settings(),
            )

    def test_final_env_precedence_and_running_process_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            primary = pathlib.Path(directory) / "primary.env"
            later = pathlib.Path(directory) / "later.env"
            primary.write_text(
                "".join(f"{key}={value}\n" for key, value in self.settings().items()),
                encoding="utf-8",
            )
            later.write_text(
                "MEMORY_V1_COLLECTION=memory_claim_v1_active\n",
                encoding="utf-8",
            )
            readiness._validate_env_setting_occurrences(primary, later)
            self.assertEqual(
                {
                    key: readiness._env_key_occurrences(primary, key)
                    for key in readiness.EFFECTIVE_MEMORY_SETTINGS
                },
                {key: 1 for key in readiness.EFFECTIVE_MEMORY_SETTINGS},
            )
            primary.write_text(
                "".join(f"{key}={value}\n" for key, value in self.settings().items())
                + "MEMORY_V1_V5_SHADOW=1\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "exactly one"):
                readiness._validate_env_setting_occurrences(primary, later)
            primary.write_text(
                "".join(f"{key}={value}\n" for key, value in self.settings().items()),
                encoding="utf-8",
            )
            later.write_text(
                "MEMORY_V1_COLLECTION=memory_claim_v1_active\n"
                "MEMORY_V1_COLLECTION=memory_claim_v1_active\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "at most one"):
                readiness._validate_env_setting_occurrences(primary, later)
        completed = types.SimpleNamespace(returncode=0, stdout="123\n")
        process_bytes = b"A=B\0" + b"\0".join(
            f"{key}={value}".encode("utf-8")
            for key, value in self.settings().items()
        ) + b"\0"
        with mock.patch.object(
            readiness.subprocess, "run", return_value=completed
        ), mock.patch.object(
            pathlib.Path,
            "read_bytes",
            return_value=process_bytes,
        ):
            self.assertEqual(
                readiness._running_service_memory_settings("brains.service"),
                self.settings(),
            )
        with mock.patch.object(
            readiness.subprocess, "run", return_value=completed
        ), mock.patch.object(
            pathlib.Path,
            "read_bytes", return_value=process_bytes.replace(
                b"MEMORY_V1_V5_SHADOW=1\0", b""
            ),
        ), self.assertRaisesRegex(RuntimeError, "exactly one"):
            readiness._running_service_memory_settings("brains.service")
        with mock.patch.object(
            readiness.subprocess, "run", return_value=completed
        ), mock.patch.object(
            pathlib.Path,
            "read_bytes",
            return_value=process_bytes
            + b"MEMORY_V1_V5_SHADOW_ALL_AUTHENTICATED=0\0",
        ), self.assertRaisesRegex(RuntimeError, "exactly one"):
            readiness._running_service_memory_settings("brains.service")

    def test_readiness_accepts_exact_mixed_retrievable_v3_inventory(self) -> None:
        owner_a = "11111111-1111-4111-8111-111111111111"
        owner_b = "22222222-2222-4222-8222-222222222222"
        claim_a = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        claim_b = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        third_claim = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        database = {
            "retrievable_claims": [
                {
                    "claim_id": claim_a,
                    "owner_user_id": owner_a,
                    "revision_number": 1,
                    "status": "supported",
                },
                {
                    "claim_id": claim_b,
                    "owner_user_id": owner_b,
                    "revision_number": 2,
                    "status": "uncertain",
                },
                {
                    "claim_id": third_claim,
                    "owner_user_id": owner_a,
                    "revision_number": 3,
                    "status": "disputed",
                },
            ]
        }
        qdrant = {
            "dimensions": 3072,
            "distance": "dot",
            "points": [
                {
                    "claim_id": item["claim_id"],
                    "owner_user_id": item["owner_user_id"],
                    "point_id": item["claim_id"],
                    "revision_number": item["revision_number"],
                    "schema_version": "memory_claim_projection_v3",
                    "status": item["status"],
                }
                for item in reversed(database["retrievable_claims"])
            ]
        }
        inventory = readiness._projection_inventories(database, qdrant)
        self.assertTrue(inventory["postgres_complete"])
        self.assertTrue(inventory["postgres_unique"])
        self.assertTrue(inventory["qdrant_complete"])
        self.assertTrue(inventory["qdrant_unique"])
        self.assertEqual(
            inventory["qdrant_projection"], inventory["postgres_projection"]
        )

    def test_qdrant_readiness_snapshot_paginates_without_hidden_points(self) -> None:
        def response(value: dict) -> io.BytesIO:
            return io.BytesIO(json.dumps(value).encode("utf-8"))

        first_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        second_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        owner = "11111111-1111-4111-8111-111111111111"
        info = {
            "result": {
                "config": {
                    "params": {"vectors": {"size": 3072, "distance": "Dot"}}
                },
                "points_count": 2,
            }
        }
        def point(point_id: str, status: str, revision: int) -> dict:
            return {
                "id": point_id,
                "payload": {
                    "claim_id": point_id,
                    "owner_user_id": owner,
                    "revision_number": revision,
                    "schema_version": "memory_claim_projection_v3",
                    "status": status,
                },
            }
        pages = [
            {
                "result": {
                    "points": [point(first_id, "supported", 1)],
                    "next_page_offset": first_id,
                }
            },
            {
                "result": {
                    "points": [point(second_id, "disputed", 2)],
                    "next_page_offset": None,
                }
            },
        ]
        with mock.patch.object(
            readiness.urllib.request,
            "urlopen",
            side_effect=[response(info), *(response(page) for page in pages)],
        ):
            snapshot = readiness._qdrant_snapshot(
                "http://127.0.0.1:6333/collections/memory_claim_v1_active/points/scroll"
            )
        self.assertEqual(snapshot["distance"], "dot")
        self.assertEqual(len(snapshot["points"]), 2)

        with mock.patch.object(
            readiness.urllib.request,
            "urlopen",
            side_effect=[
                response(info),
                response(
                    {
                        "result": {
                            "points": [point(first_id, "supported", 1)],
                            "next_page_offset": None,
                        }
                    }
                ),
            ],
        ), self.assertRaisesRegex(RuntimeError, "truncated"):
            readiness._qdrant_snapshot(
                "http://127.0.0.1:6333/collections/memory_claim_v1_active/points/scroll"
            )


class AliasTransitionScriptTests(unittest.TestCase):
    def test_collection_identity_rejects_cosine_shadow_and_v3_legacy(self) -> None:
        class CosineShadow(FakeQdrant):
            def get_collection(self, *, collection_name: str):
                info = super().get_collection(collection_name=collection_name)
                info.config.params.vectors.distance = "Cosine"
                return info

        with self.assertRaisesRegex(cutover.AliasCutoverError, "shadow.*distance"):
            cutover.collection_evidence(
                CosineShadow(aliases={}, target=SHADOW),
                SHADOW,
                require_rebuild_provenance=False,
            )

        class V3Legacy(FakeQdrant):
            def scroll(self, **kwargs):
                if kwargs.get("offset") is not None:
                    return [], None
                payload = incremental_payload(
                    self.point_id,
                    self.owner,
                    version="memory_claim_projection_v3",
                )
                return [
                    types.SimpleNamespace(
                        id=self.point_id,
                        payload=payload,
                        vector=list(DOT_VECTOR),
                    )
                ], None

        with self.assertRaisesRegex(cutover.AliasCutoverError, "provenance"):
            cutover.collection_evidence(
                V3Legacy(aliases={}, target=SHADOW),
                "memory_claim_v1",
                require_rebuild_provenance=False,
            )

    def test_non_supported_current_claim_blocks_supported_only_target(self) -> None:
        supported = (
            "11111111-1111-4111-8111-111111111111",
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "supported",
            1,
            "a" * 64,
        )
        disputed = (
            "22222222-2222-4222-8222-222222222222",
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "disputed",
            2,
            "b" * 64,
        )
        with self.assertRaisesRegex(
            cutover.AliasCutoverError, "projection inventory differs"
        ):
            cutover.require_current_source_parity(
                {"_projection_inventory": [supported]},
                {"_projection_inventory": [supported, disputed]},
            )

    def test_shadow_source_accepts_exact_mixed_v2_v3_but_target_remains_v2(self) -> None:
        class MixedQdrant(FakeQdrant):
            def get_collection(self, *, collection_name: str):
                info = super().get_collection(collection_name=collection_name)
                info.points_count = 2
                return info

            def scroll(self, **kwargs):
                if kwargs.get("offset") is not None:
                    return [], None
                first_id = "00000000-0000-4000-8000-000000000001"
                second_id = "00000000-0000-4000-8000-000000000003"
                return [
                    types.SimpleNamespace(
                        id=first_id,
                        payload=shadow_payload(first_id, self.owner),
                        vector=list(DOT_VECTOR),
                    ),
                    types.SimpleNamespace(
                        id=second_id,
                        payload=incremental_payload(
                            second_id,
                            "00000000-0000-4000-8000-000000000004",
                            version="memory_claim_projection_v3",
                        ),
                        vector=list(DOT_VECTOR),
                    ),
                ], None

        fake = MixedQdrant(aliases={}, target=SHADOW)
        evidence = cutover.collection_evidence(
            fake, SHADOW, require_rebuild_provenance=False
        )
        self.assertEqual(evidence["point_count"], 2)
        self.assertEqual(evidence["supported_point_count"], 2)
        postgres_inventory = [
            tuple(item) for item in evidence["_projection_inventory"]
        ]
        source_state = {"_projection_inventory": postgres_inventory}
        cutover.require_current_source_parity(evidence, source_state)
        with self.assertRaises(cutover.AliasCutoverError):
            cutover.collection_evidence(
                fake, SHADOW, require_rebuild_provenance=True
            )

        original = fake.scroll

        def tampered(**kwargs):
            records, offset = original(**kwargs)
            records[1].payload["projection_manifest_sha256"] = "0" * 64
            return records, offset

        fake.scroll = tampered  # type: ignore[method-assign]
        with self.assertRaises(cutover.AliasCutoverError):
            cutover.collection_evidence(
                fake, SHADOW, require_rebuild_provenance=False
            )

    def test_report_backed_rollback_accepts_only_pg_current_mixed_v2_v3(self) -> None:
        class MixedQdrant(FakeQdrant):
            def get_collection(self, *, collection_name: str):
                info = super().get_collection(collection_name=collection_name)
                info.points_count = 2
                return info

            def scroll(self, **kwargs):
                if kwargs.get("offset") is not None:
                    return [], None
                first_id = "00000000-0000-4000-8000-000000000001"
                second_id = "00000000-0000-4000-8000-000000000003"
                return [
                    types.SimpleNamespace(
                        id=first_id,
                        payload=shadow_payload(first_id, self.owner),
                        vector=list(DOT_VECTOR),
                    ),
                    types.SimpleNamespace(
                        id=second_id,
                        payload=incremental_payload(
                            second_id,
                            "00000000-0000-4000-8000-000000000004",
                            version="memory_claim_projection_v3",
                        ),
                        vector=list(DOT_VECTOR),
                    ),
                ], None

        strict_fake = FakeQdrant(aliases={}, target=SHADOW)
        accepted = cutover.collection_evidence(
            strict_fake, SHADOW, require_rebuild_provenance=True
        )
        report = accepted_rebuild_report(accepted, SHADOW)
        mixed_fake = MixedQdrant(aliases={}, target=SHADOW)
        current = cutover.collection_evidence(
            mixed_fake, SHADOW, require_rebuild_provenance=False
        )
        value = alias_spec(
            operation="rollback",
            source=SHADOW_2,
            target=SHADOW,
            report_path="/home/ubuntu/brains/snapshots/test-run/rebuild.json",
            target_fingerprint=current["collection_fingerprint_sha256"],
        )
        spec = cutover.Spec(value=value, raw=rebuild.canonical_bytes(value))
        cutover.validate_target_evidence(spec, report, current)
        postgres = {"_projection_inventory": current["_projection_inventory"]}
        cutover.require_current_source_parity(current, postgres)
        stale = {
            "_projection_inventory": [
                [*current["_projection_inventory"][0][:3], 99, "f" * 64]
            ]
        }
        with self.assertRaises(cutover.AliasCutoverError):
            cutover.require_current_source_parity(current, stale)

    def test_bootstrap_recurring_cutover_and_rollback_specs_are_distinct(self) -> None:
        bootstrap = alias_spec(
            operation="bootstrap",
            source=None,
            target="memory_claim_v1",
            report_path=None,
        )
        temporary, loaded = load_spec(cutover.Spec, bootstrap)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(loaded.value["operation"], "bootstrap")
        for operation, source, target in (
            ("cutover", "memory_claim_v1", SHADOW),
            ("cutover", SHADOW, SHADOW_2),
            ("rollback", SHADOW_2, SHADOW),
            ("rollback", SHADOW, "memory_claim_v1"),
        ):
            value = alias_spec(
                operation=operation,
                source=source,
                target=target,
                report_path=(
                    None
                    if target == "memory_claim_v1"
                    else "/home/ubuntu/brains/snapshots/test-run/rebuild.json"
                ),
            )
            temp, spec = load_spec(cutover.Spec, value)
            self.addCleanup(temp.cleanup)
            self.assertEqual(spec.value["operation"], operation)

    def test_alias_operations_never_delete_a_collection(self) -> None:
        value = alias_spec(
            operation="cutover",
            source="memory_claim_v1",
            target=SHADOW,
            report_path="/home/ubuntu/brains/snapshots/test-run/rebuild.json",
        )
        temporary, spec = load_spec(cutover.Spec, value)
        self.addCleanup(temporary.cleanup)
        operations = cutover.operations(
            spec, {"memory_claim_v1_active": "memory_claim_v1"}
        )
        self.assertEqual(len(operations), 2)
        self.assertIsNotNone(operations[0].delete_alias)
        self.assertIsNotNone(operations[1].create_alias)
        source_text = pathlib.Path(cutover.__file__).read_text()
        self.assertNotIn("delete_collection", source_text)

    def test_alias_state_drift_fails_before_transition(self) -> None:
        value = alias_spec(
            operation="cutover",
            source="memory_claim_v1",
            target=SHADOW,
            report_path="/home/ubuntu/brains/snapshots/test-run/rebuild.json",
        )
        temporary, spec = load_spec(cutover.Spec, value)
        self.addCleanup(temporary.cleanup)
        with self.assertRaises(cutover.AliasCutoverError):
            cutover.operations(spec, {"memory_claim_v1_active": "unexpected"})

    def test_report_is_exact_and_binds_the_target_not_the_source(self) -> None:
        fake = FakeQdrant(
            aliases={"memory_claim_v1_active": SHADOW_2}, target=SHADOW
        )
        evidence = cutover.collection_evidence(
            fake, SHADOW, require_rebuild_provenance=True
        )
        report = accepted_rebuild_report(evidence, SHADOW)
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            root.chmod(0o755)
            run = root / "run"
            run.mkdir(mode=0o700)
            report_path = run / "rebuild.json"
            raw = rebuild.canonical_bytes(report)
            report_path.write_bytes(raw)
            report_path.chmod(0o600)
            value = alias_spec(
                operation="rollback",
                source=SHADOW_2,
                target=SHADOW,
                report_path=str(report_path),
                target_fingerprint=evidence["collection_fingerprint_sha256"],
            )
            value["rebuild_report_sha256"] = hashlib.sha256(raw).hexdigest()
            spec = cutover.Spec(value=value, raw=rebuild.canonical_bytes(value))
            with mock.patch.object(cutover, "SNAPSHOT_ROOT_PATH", root):
                self.assertEqual(cutover.load_rebuild_report(spec), report)
            changed = {**report, "unexpected": True}
            changed_raw = rebuild.canonical_bytes(changed)
            report_path.write_bytes(changed_raw)
            value["rebuild_report_sha256"] = hashlib.sha256(changed_raw).hexdigest()
            with self.assertRaises(cutover.AliasCutoverError):
                with mock.patch.object(cutover, "SNAPSHOT_ROOT_PATH", root):
                    cutover.load_rebuild_report(
                        cutover.Spec(value=value, raw=rebuild.canonical_bytes(value))
                    )

    def test_rebuild_report_intermediate_symlink_is_rejected(self) -> None:
        fake = FakeQdrant(
            aliases={"memory_claim_v1_active": "memory_claim_v1"}, target=SHADOW
        )
        evidence = cutover.collection_evidence(
            fake, SHADOW, require_rebuild_provenance=True
        )
        report = accepted_rebuild_report(evidence, SHADOW)
        raw = rebuild.canonical_bytes(report)
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as external:
            root = pathlib.Path(temporary)
            root.chmod(0o755)
            external_path = pathlib.Path(external)
            external_path.chmod(0o700)
            report_path = external_path / "rebuild.json"
            report_path.write_bytes(raw)
            report_path.chmod(0o600)
            (root / "escape").symlink_to(external_path, target_is_directory=True)
            value = alias_spec(
                operation="cutover",
                source="memory_claim_v1",
                target=SHADOW,
                report_path=str(root / "escape" / "rebuild.json"),
                target_fingerprint=evidence["collection_fingerprint_sha256"],
            )
            value["rebuild_report_sha256"] = hashlib.sha256(raw).hexdigest()
            spec = cutover.Spec(value=value, raw=rebuild.canonical_bytes(value))
            with mock.patch.object(cutover, "SNAPSHOT_ROOT_PATH", root):
                with self.assertRaises(cutover.AliasCutoverError):
                    cutover.load_rebuild_report(spec)

    def test_target_fingerprint_catches_vector_and_provenance_drift(self) -> None:
        fake = FakeQdrant(
            aliases={"memory_claim_v1_active": "memory_claim_v1"}, target=SHADOW
        )
        evidence = cutover.collection_evidence(
            fake, SHADOW, require_rebuild_provenance=True
        )
        report = accepted_rebuild_report(evidence, SHADOW)
        value = alias_spec(
            operation="cutover",
            source="memory_claim_v1",
            target=SHADOW,
            report_path="/home/ubuntu/brains/snapshots/test-run/rebuild.json",
            target_fingerprint=evidence["collection_fingerprint_sha256"],
        )
        spec = cutover.Spec(value=value, raw=rebuild.canonical_bytes(value))
        cutover.validate_target_evidence(spec, report, evidence)
        drifted = {**evidence, "collection_fingerprint_sha256": "0" * 64}
        with self.assertRaises(cutover.AliasCutoverError):
            cutover.validate_target_evidence(spec, report, drifted)
        original_scroll = fake.scroll

        def changed_vector_hash(**kwargs):
            records, offset = original_scroll(**kwargs)
            for record in records:
                record.payload = {**record.payload, "vector_sha256": "0" * 64}
            return records, offset

        fake.scroll = changed_vector_hash  # type: ignore[method-assign]
        with self.assertRaises(cutover.AliasCutoverError):
            cutover.collection_evidence(
                fake, SHADOW, require_rebuild_provenance=True
            )

    def test_audit_is_no_clobber_private_durable_and_hash_chained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            root.chmod(0o755)
            run = root / "run"
            run.mkdir(mode=0o700)
            path = run / "audit.jsonl"
            audit = cutover.AliasAudit.create(path, trusted_root=root)
            audit.append("prepared", {"contract_version": cutover.REPORT_VERSION})
            audit.append("completed", {"contract_version": cutover.REPORT_VERSION})
            audit.close()
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            events = parse_audit(path)
            self.assertEqual([item["state"] for item in events], ["prepared", "completed"])
            with self.assertRaises(cutover.AliasCutoverError):
                cutover.AliasAudit.create(path, trusted_root=root)

    def test_intermediate_symlink_cannot_escape_audit_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as external:
            root = pathlib.Path(temporary)
            root.chmod(0o755)
            external_path = pathlib.Path(external)
            external_path.chmod(0o700)
            (root / "escape").symlink_to(external_path, target_is_directory=True)
            with self.assertRaises(cutover.AliasCutoverError):
                cutover.AliasAudit.create(
                    root / "escape" / "audit.jsonl", trusted_root=root
                )

    def _execute_case(
        self,
        behavior: str,
        *,
        source_states: list[dict] | None = None,
        target_revision: int = 1,
    ) -> tuple[FakeQdrant, list[dict], Exception | None]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        root.chmod(0o755)
        run = root / "run"
        run.mkdir(mode=0o700)
        fake = FakeQdrant(
            aliases={"memory_claim_v1_active": "memory_claim_v1"},
            target=SHADOW,
            behavior=behavior,
            target_revision=target_revision,
            unrelated={"other_alias": "other_collection"},
        )
        evidence = cutover.collection_evidence(
            fake, SHADOW, require_rebuild_provenance=True
        )
        source_evidence = cutover.collection_evidence(
            fake, "memory_claim_v1", require_rebuild_provenance=False
        )
        report = accepted_rebuild_report(evidence, SHADOW)
        report_raw = rebuild.canonical_bytes(report)
        report_path = run / "rebuild.json"
        report_path.write_bytes(report_raw)
        report_path.chmod(0o600)
        output_path = run / "audit.jsonl"
        fake.audit_path = output_path
        value = alias_spec(
            operation="cutover",
            source="memory_claim_v1",
            target=SHADOW,
            report_path=str(report_path),
            target_fingerprint=evidence["collection_fingerprint_sha256"],
            source_fingerprint=source_evidence[
                "collection_fingerprint_sha256"
            ],
        )
        value["rebuild_report_sha256"] = hashlib.sha256(report_raw).hexdigest()
        value["output_path"] = str(output_path)
        pg_inventory = [
            (*item[:4], "f" * 64)
            for item in source_evidence["_projection_inventory"]
        ]
        source_state = {
            "claim_count": report["claim_count"],
            "owner_count": report["owner_count"],
            "projection_identity_inventory_sha256": hashlib.sha256(
                rebuild.canonical_bytes(sorted(item[:4] for item in pg_inventory))
            ).hexdigest(),
            "projection_inventory_sha256": hashlib.sha256(
                rebuild.canonical_bytes(sorted(pg_inventory))
            ).hexdigest(),
            "_projection_inventory": sorted(pg_inventory),
            "supported_projection_inventory_sha256": source_evidence[
                "supported_projection_inventory_sha256"
            ],
            "source_snapshot_sha256": report["source_snapshot_sha256"],
        }
        value["expected_postgres_projection_inventory_sha256"] = source_state[
            "projection_inventory_sha256"
        ]
        value["expected_postgres_supported_snapshot_sha256"] = source_state[
            "source_snapshot_sha256"
        ]
        spec = cutover.Spec(value=value, raw=rebuild.canonical_bytes(value))
        error: Exception | None = None
        source_state_effect: object = (
            [{**source_state, **item} for item in source_states]
            if source_states is not None
            else source_state
        )
        with mock.patch.object(
            cutover, "git_identity", return_value=value["expected_git"]
        ), mock.patch.object(
            cutover, "make_qdrant_client", return_value=fake
        ), mock.patch.object(
            cutover, "SNAPSHOT_ROOT_PATH", root
        ), mock.patch.object(
            cutover,
            "current_source_state",
            side_effect=(source_state_effect if isinstance(source_state_effect, list) else None),
            return_value=(source_state_effect if isinstance(source_state_effect, dict) else None),
        ), mock.patch.object(cutover, "require_guard"), mock.patch.object(
            cutover, "qdrant_mutation_lock", return_value=mock.Mock()
        ):
            try:
                cutover.execute(spec)
            except Exception as exc:  # asserted by callers
                error = exc
        return fake, (parse_audit(output_path) if output_path.exists() else []), error

    def test_prepared_event_precedes_one_atomic_alias_update(self) -> None:
        fake, events, error = self._execute_case("success")
        self.assertIsNone(error)
        self.assertEqual(fake.update_calls, 1)
        self.assertEqual(fake.aliases["memory_claim_v1_active"], SHADOW)
        self.assertEqual(fake.aliases["other_alias"], "other_collection")
        self.assertEqual([item["state"] for item in events], ["prepared", "completed"])
        self.assertTrue(events[-1]["postcondition_verified"])

    def test_rejected_update_is_recorded_as_failed_no_change(self) -> None:
        fake, events, error = self._execute_case("reject")
        self.assertIsInstance(error, cutover.AliasCutoverError)
        self.assertEqual(fake.update_calls, 1)
        self.assertEqual(
            [item["state"] for item in events], ["prepared", "failed_no_change"]
        )

    def test_source_drift_before_transition_causes_zero_alias_mutations(self) -> None:
        current = {
            "claim_count": 1,
            "owner_count": 1,
            "source_snapshot_sha256": "1" * 64,
        }
        changed = {**current, "source_snapshot_sha256": "2" * 64}
        fake, events, error = self._execute_case(
            "success", source_states=[current, changed]
        )
        self.assertIsInstance(error, cutover.AliasCutoverError)
        self.assertEqual(fake.update_calls, 0)
        self.assertEqual(
            [item["state"] for item in events], ["prepared", "failed_no_change"]
        )

    def test_stale_target_is_rejected_before_any_alias_mutation(self) -> None:
        fake, events, error = self._execute_case(
            "success", target_revision=2
        )
        self.assertIsInstance(error, cutover.AliasCutoverError)
        self.assertEqual(fake.update_calls, 0)
        self.assertEqual(events, [])
        self.assertEqual(fake.aliases["memory_claim_v1_active"], "memory_claim_v1")

    def test_source_drift_after_transition_does_not_restore_stale_prior_alias(self) -> None:
        current = {
            "claim_count": 1,
            "owner_count": 1,
            "source_snapshot_sha256": "1" * 64,
        }
        changed = {**current, "source_snapshot_sha256": "2" * 64}
        fake, events, error = self._execute_case(
            "success", source_states=[current, current, changed]
        )
        self.assertIsInstance(error, cutover.AliasCutoverError)
        self.assertEqual(fake.update_calls, 1)
        self.assertEqual(fake.aliases["memory_claim_v1_active"], SHADOW)
        self.assertEqual(
            [item["state"] for item in events],
            ["prepared", "indeterminate"],
        )

    def test_committed_timeout_is_not_misreported_as_failure(self) -> None:
        fake, events, error = self._execute_case("commit_then_timeout")
        self.assertIsInstance(error, cutover.AliasCutoverError)
        self.assertEqual(fake.aliases["memory_claim_v1_active"], "memory_claim_v1")
        self.assertEqual(fake.update_calls, 2)
        self.assertEqual(
            [item["state"] for item in events],
            ["prepared", "rolled_back_verified"],
        )

    def test_completed_audit_failure_restores_prior_alias_and_records_rollback(self) -> None:
        original_append = cutover.AliasAudit.append

        def fail_completed(audit, state, fields):
            if state == "completed":
                raise cutover.AliasCutoverError("synthetic completed fsync failure")
            return original_append(audit, state, fields)

        with mock.patch.object(cutover.AliasAudit, "append", new=fail_completed):
            fake, events, error = self._execute_case("success")
        self.assertIsInstance(error, cutover.AliasCutoverError)
        self.assertEqual(fake.aliases["memory_claim_v1_active"], "memory_claim_v1")
        self.assertEqual(fake.update_calls, 2)
        self.assertEqual(
            [item["state"] for item in events],
            ["prepared", "rolled_back_verified"],
        )

    def test_terminal_audit_failure_is_explicit_after_verified_rollback(self) -> None:
        original_append = cutover.AliasAudit.append

        def fail_terminal(audit, state, fields):
            if state in {"completed", "rolled_back_verified"}:
                raise cutover.AliasCutoverError("synthetic terminal fsync failure")
            return original_append(audit, state, fields)

        with mock.patch.object(cutover.AliasAudit, "append", new=fail_terminal):
            fake, events, error = self._execute_case("success")
        self.assertIsInstance(error, cutover.AliasCutoverError)
        self.assertIn("audit outcome:indeterminate", str(error))
        self.assertEqual(fake.aliases["memory_claim_v1_active"], "memory_claim_v1")
        self.assertEqual(fake.update_calls, 2)
        self.assertEqual([item["state"] for item in events], ["prepared"])

    def test_unrelated_alias_drift_is_indeterminate(self) -> None:
        fake, events, error = self._execute_case("unrelated_drift")
        self.assertIsInstance(error, cutover.AliasCutoverError)
        self.assertEqual(fake.aliases["memory_claim_v1_active"], SHADOW)
        self.assertEqual([item["state"] for item in events], ["prepared", "indeterminate"])


@unittest.skipUnless(
    os.environ.get("MEMORY_V1_QDRANT_DISPOSABLE") == "1",
    "requires an explicitly disposable real HTTP Qdrant",
)
class RealHttpQdrantMetricTests(unittest.TestCase):
    def test_dot_round_trip_and_cosine_ranking_parity(self) -> None:
        url = os.environ.get("MEMORY_V1_QDRANT_TEST_URL", "")
        matched = re.fullmatch(r"http://127\.0\.0\.1:([0-9]{2,5})", url)
        if matched is None or int(matched.group(1)) in {6333, 6334}:
            self.fail("disposable Qdrant URL must be loopback on a non-production port")
        client = rebuild.make_qdrant_client(url=url, timeout=15.0)
        suffix = uuid.uuid4().hex[:12]
        cosine_collection = f"phase5_cosine_parity_{suffix}"
        dot_collection = f"phase5_dot_parity_{suffix}"
        created: list[str] = []
        dimensions = 3072
        point_ids = [str(uuid.uuid4()) for _ in range(3)]
        raw_documents = [
            [float(((index + phase * 7) % 31) - 15) for index in range(dimensions)]
            for phase in range(3)
        ]
        raw_query = [float(((index * 5 + 3) % 29) - 14) for index in range(dimensions)]
        normalized_documents = [
            rebuild_contract.normalize_cosine_vector(
                document, dimensions=dimensions
            )
            for document in raw_documents
        ]
        normalized_query = rebuild_contract.normalize_cosine_vector(
            raw_query, dimensions=dimensions
        )
        try:
            client.create_collection(
                collection_name=cosine_collection,
                vectors_config=rebuild.qmodels.VectorParams(
                    size=dimensions, distance=rebuild.qmodels.Distance.COSINE
                ),
            )
            created.append(cosine_collection)
            client.create_collection(
                collection_name=dot_collection,
                vectors_config=rebuild.qmodels.VectorParams(
                    size=dimensions, distance=rebuild.qmodels.Distance.DOT
                ),
            )
            created.append(dot_collection)
            client.upsert(
                collection_name=cosine_collection,
                wait=True,
                points=[
                    rebuild.qmodels.PointStruct(id=point_id, vector=vector, payload={})
                    for point_id, vector in zip(
                        point_ids, raw_documents, strict=True
                    )
                ],
            )
            client.upsert(
                collection_name=dot_collection,
                wait=True,
                points=[
                    rebuild.qmodels.PointStruct(id=point_id, vector=vector, payload={})
                    for point_id, vector in zip(
                        point_ids, normalized_documents, strict=True
                    )
                ],
            )
            retrieved = client.retrieve(
                collection_name=dot_collection,
                ids=point_ids,
                with_payload=False,
                with_vectors=True,
            )
            by_id = {str(item.id): item.vector for item in retrieved}
            for point_id, expected in zip(
                point_ids, normalized_documents, strict=True
            ):
                self.assertEqual(
                    rebuild.vector_sha256(by_id[point_id]),
                    rebuild.vector_sha256(expected),
                )
            cosine_hits = client.query_points(
                collection_name=cosine_collection,
                query=raw_query,
                limit=3,
            ).points
            dot_hits = client.query_points(
                collection_name=dot_collection,
                query=normalized_query,
                limit=3,
            ).points
            self.assertEqual(
                [str(item.id) for item in cosine_hits],
                [str(item.id) for item in dot_hits],
            )
            for cosine_hit, dot_hit in zip(cosine_hits, dot_hits, strict=True):
                self.assertAlmostEqual(
                    float(cosine_hit.score), float(dot_hit.score), places=6
                )
        finally:
            for collection in reversed(created):
                client.delete_collection(collection_name=collection)
            client.close()


if __name__ == "__main__":
    unittest.main()
