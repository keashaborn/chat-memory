from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from scripts.memory_v1_v5_stage_batch import (
    StageBatchError,
    apply_once,
    expected_schema_hashes,
    load_manifest,
    stable_json,
)


OWNER = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER = uuid.UUID("22222222-2222-4222-8222-222222222222")
EVIDENCE = uuid.UUID("aeeeeeee-1111-4111-8111-111111111111")
REQUEST = uuid.UUID("10000000-0000-4000-8000-000000000001")
SOURCE = uuid.UUID("aaaaaaaa-1111-4111-8111-111111111111")


def digest(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode()
    return hashlib.sha256(value).hexdigest()


def write_secure(path: Path, value: dict) -> str:
    raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    return digest(raw)


def empty_bundle(owner: uuid.UUID = OWNER) -> dict:
    source = {
        "job_id": "unit-empty",
        "source_system": "public.chat_log",
        "source_external_id": str(SOURCE),
        "source_sha256": "a" * 64,
        "source_recorded_at": "2026-07-16T00:00:00Z",
    }
    extraction = {
        "contract_version": "memory_v1_relational_extraction_v5",
        "source_envelope": source,
        "predicate_registry_version": "memory_predicate_registry_v5",
        "entity_mentions": [],
        "observations": [],
        "comparison_hints": [],
        "deferrals": [],
        "packet_findings": [],
    }
    resolution_body = {
        "contract_version": "memory_v1_entity_resolution_review_v5",
        "source_envelope": source,
        "predicate_registry_version": "memory_predicate_registry_v5",
        "entity_normalization_version": "memory_entity_normalization_v5",
        "resolver": "unit_test",
        "resolver_version": "v5",
        "resolutions": [],
    }
    resolution = {
        **resolution_body,
        "packet_sha256": digest(stable_json(resolution_body)),
    }
    extraction_text = stable_json(extraction)
    resolution_text = stable_json(resolution)
    return {
        "contract_version": "memory_v1_v5_stage_preflight_v1",
        "generated_at": "2026-07-16T00:00:00Z",
        "mode": "preflight_only_zero_write",
        "server": "seebx",
        "owner_user_id": str(owner),
        "case_id": "unit-empty",
        "evidence_id": str(EVIDENCE),
        "request_id": str(REQUEST),
        "extractor": "unit_test",
        "extractor_version": "v5",
        "source_report": {"path": "/tmp/source.json", "sha256": "b" * 64},
        "schemas": {
            "extraction_sha256": "c" * 64,
            "resolution_sha256": "d" * 64,
        },
        "extraction_packet_text": extraction_text,
        "resolution_packet_text": resolution_text,
        "extraction_packet_sha256": digest(extraction_text),
        "resolution_packet_sha256": digest(resolution_text),
        "resolution_summary": {
            "auto_link_eligible": 0,
            "manual_review_required": 0,
            "deferred": 0,
            "rejected": 0,
        },
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
        "authorized_stage": False,
    }


class Transaction:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        self.connection.transaction_entries += 1
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.connection.transaction_exits.append(exc_type)
        return False


class Connection:
    def __init__(self, row):
        self.row = row
        self.calls = []
        self.transaction_entries = 0
        self.transaction_exits = []

    def transaction(self):
        return Transaction(self)

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        return self.row


class StageBatchTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        os.chmod(self.root, 0o700)

    def manifest(self, *, owner=OWNER, bundle_owner=OWNER, counts=None):
        bundle_path = self.root / "bundle.json"
        report_path = self.root / "source-review.json"
        report_sha = write_secure(
            report_path, {"contract_version": "unit_source_review_v1"}
        )
        bundle = empty_bundle(bundle_owner)
        bundle["source_report"] = {
            "path": str(report_path),
            "sha256": report_sha,
        }
        bundle["schemas"] = expected_schema_hashes()
        bundle_sha = write_secure(bundle_path, bundle)
        manifest = {
            "contract_version": "memory_v1_v5_stage_batch_manifest_v1",
            "target_server": "seebx",
            "owner_user_id": str(owner),
            "bundles": [
                {
                    "path": str(bundle_path),
                    "sha256": bundle_sha,
                    "expected_outcome": "applied",
                    "expected_counts": counts
                    or {
                        "mentions": 0,
                        "resolutions": 0,
                        "candidates": 0,
                        "observations": 0,
                        "temporals": 0,
                    },
                }
            ],
        }
        manifest_path = self.root / "manifest.json"
        write_secure(manifest_path, manifest)
        return manifest_path

    def rewrite_bundle(self, manifest_path, mutator):
        manifest = json.loads(manifest_path.read_text())
        bundle_path = Path(manifest["bundles"][0]["path"])
        bundle = json.loads(bundle_path.read_text())
        mutator(bundle)
        manifest["bundles"][0]["sha256"] = write_secure(bundle_path, bundle)
        write_secure(manifest_path, manifest)

    def test_manifest_rejects_changed_source_report(self):
        manifest = self.manifest()
        report_path = self.root / "source-review.json"
        write_secure(report_path, {"contract_version": "tampered"})
        with self.assertRaisesRegex(StageBatchError, "source report content hash"):
            load_manifest(str(manifest), root=self.root)

    def test_manifest_accepts_empty_audited_outcome(self):
        metadata, bundles, _, _ = load_manifest(
            str(self.manifest()), root=self.root
        )
        self.assertEqual(metadata["owner_user_id"], OWNER)
        self.assertEqual(metadata["bundle_count"], 1)
        self.assertEqual(metadata["expected_new_rows"], 2)
        self.assertEqual(bundles[0]["expected_counts"]["observations"], 0)

    def test_manifest_rejects_cross_owner_bundle(self):
        with self.assertRaisesRegex(StageBatchError, "bundle owner"):
            load_manifest(
                str(self.manifest(bundle_owner=OTHER)),
                root=self.root,
            )

    def test_manifest_rejects_false_row_budget(self):
        counts = {
            "mentions": 1,
            "resolutions": 0,
            "candidates": 0,
            "observations": 0,
            "temporals": 0,
        }
        with self.assertRaisesRegex(StageBatchError, "packet structure"):
            load_manifest(
                str(self.manifest(counts=counts)),
                root=self.root,
            )

    def test_manifest_rejects_bundle_outside_review_root(self):
        outside_directory = tempfile.TemporaryDirectory()
        self.addCleanup(outside_directory.cleanup)
        outside = Path(outside_directory.name)
        os.chmod(outside, 0o700)
        bundle_path = outside / "bundle.json"
        bundle_sha = write_secure(bundle_path, empty_bundle())
        manifest = {
            "contract_version": "memory_v1_v5_stage_batch_manifest_v1",
            "target_server": "seebx",
            "owner_user_id": str(OWNER),
            "bundles": [
                {
                    "path": str(bundle_path),
                    "sha256": bundle_sha,
                    "expected_outcome": "applied",
                    "expected_counts": {key: 0 for key in (
                        "mentions",
                        "resolutions",
                        "candidates",
                        "observations",
                        "temporals",
                    )},
                }
            ],
        }
        manifest_path = self.root / "manifest.json"
        write_secure(manifest_path, manifest)
        with self.assertRaisesRegex(StageBatchError, "outside"):
            load_manifest(str(manifest_path), root=self.root)

    def test_manifest_rejects_unbound_schema_hash(self):
        manifest = self.manifest()
        self.rewrite_bundle(
            manifest,
            lambda bundle: bundle["schemas"].__setitem__(
                "extraction_sha256", "0" * 64
            ),
        )
        with self.assertRaisesRegex(StageBatchError, "checked-in contracts"):
            load_manifest(str(manifest), root=self.root)

    def test_manifest_rejects_false_resolution_summary(self):
        manifest = self.manifest()
        self.rewrite_bundle(
            manifest,
            lambda bundle: bundle["resolution_summary"].__setitem__(
                "deferred", 1
            ),
        )
        with self.assertRaisesRegex(StageBatchError, "differs from resolution packet"):
            load_manifest(str(manifest), root=self.root)

    def test_manifest_rejects_false_internal_packet_hash(self):
        manifest = self.manifest()

        def mutate(bundle):
            resolution = json.loads(bundle["resolution_packet_text"])
            resolution["packet_sha256"] = "0" * 64
            bundle["resolution_packet_text"] = stable_json(resolution)
            bundle["resolution_packet_sha256"] = digest(
                bundle["resolution_packet_text"]
            )

        self.rewrite_bundle(manifest, mutate)
        with self.assertRaisesRegex(StageBatchError, "internal hash"):
            load_manifest(str(manifest), root=self.root)

    def test_apply_sets_actor_and_owner_lock(self):
        row = {
            "batch_id": uuid.UUID("30000000-0000-4000-8000-000000000001"),
            "outcome": "applied",
            "mentions_inserted": 0,
            "resolutions_inserted": 0,
            "candidates_inserted": 0,
            "observations_inserted": 0,
            "temporals_inserted": 0,
            "result": {},
        }
        connection = Connection(row)
        bundle = {
            "case_id": "unit-empty",
            "request_id": REQUEST,
            "evidence_id": EVIDENCE,
            "extractor": "unit_test",
            "extractor_version": "v5",
            "extraction_packet_text": "{}",
            "resolution_packet_text": "{}",
            "extraction_packet_sha256": "a" * 64,
            "resolution_packet_sha256": "b" * 64,
            "expected_outcome": "applied",
            "expected_counts": {key: 0 for key in (
                "mentions",
                "resolutions",
                "candidates",
                "observations",
                "temporals",
            )},
        }
        result = asyncio.run(
            apply_once(connection, OWNER, [bundle], replay=False)
        )
        self.assertEqual(result[0]["outcome"], "applied")
        self.assertEqual(connection.transaction_entries, 1)
        self.assertIsNone(connection.transaction_exits[0])
        self.assertEqual(connection.calls[0][2], (str(OWNER),))
        self.assertIn("pg_advisory_xact_lock", connection.calls[1][1])
        self.assertEqual(connection.calls[1][2], (f"{OWNER}|relational_stage_v5",))


if __name__ == "__main__":
    unittest.main()
