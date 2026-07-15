#!/usr/bin/env python3
from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from scripts.memory_v1_consolidation_packet_eval import (
    EXTRACTOR_VERSION,
    MANIFEST_VERSION,
    digest_rows,
    qdrant_signature,
    secure_write_json,
    validate_manifest,
)


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"


def manifest() -> dict:
    return {
        "manifest_version": MANIFEST_VERSION,
        "owner_user_id": OWNER,
        "source_pipeline_version": "20260714_v1",
        "evaluator_pipeline_version": EXTRACTOR_VERSION,
        "selection": {
            "algorithm": "newest_pending_source_time_desc_job_id_desc",
            "limit": 2,
        },
        "sources": [
            {
                "job_id": str(uuid.UUID(int=4)),
                "source_external_id": str(uuid.UUID(int=14)),
                "source_sha256": "a" * 64,
                "source_recorded_at": "2026-07-14T17:20:00+00:00",
            },
            {
                "job_id": str(uuid.UUID(int=3)),
                "source_external_id": str(uuid.UUID(int=13)),
                "source_sha256": "b" * 64,
                "source_recorded_at": "2026-07-14T17:19:00+00:00",
            },
        ],
    }


class ManifestContractTest(unittest.TestCase):
    def test_valid_manifest(self) -> None:
        result = validate_manifest(manifest())
        self.assertEqual(result["owner_user_id"], OWNER)

    def test_requires_strict_newest_first_order(self) -> None:
        payload = manifest()
        payload["sources"].reverse()
        with self.assertRaisesRegex(RuntimeError, "newest-first"):
            validate_manifest(payload)

    def test_rejects_duplicate_source(self) -> None:
        payload = manifest()
        payload["sources"][1]["source_external_id"] = payload["sources"][0][
            "source_external_id"
        ]
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            validate_manifest(payload)

    def test_rejects_evaluator_version_drift(self) -> None:
        payload = manifest()
        payload["evaluator_pipeline_version"] = "future"
        with self.assertRaisesRegex(RuntimeError, "evaluator_pipeline_version"):
            validate_manifest(payload)

    def test_rejects_extra_manifest_keys(self) -> None:
        payload = manifest()
        payload["unexpected"] = True
        with self.assertRaisesRegex(RuntimeError, "manifest keys"):
            validate_manifest(payload)


class OutputAndDigestTest(unittest.TestCase):
    def test_secure_output_is_private_and_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            secure_write_json(path, {"ok": True})
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                secure_write_json(path, {"ok": False})

    def test_row_digest_is_order_sensitive_and_length_framed(self) -> None:
        count_a, digest_a = digest_rows(["ab", "c"])
        count_b, digest_b = digest_rows(["a", "bc"])
        count_c, digest_c = digest_rows(["c", "ab"])
        self.assertEqual(count_a, 2)
        self.assertEqual(count_b, 2)
        self.assertEqual(count_c, 2)
        self.assertNotEqual(digest_a, digest_b)
        self.assertNotEqual(digest_a, digest_c)

    def test_qdrant_signature_rejects_foreign_owner_payload(self) -> None:
        class FakeQdrant:
            def scroll(self, **_kwargs):
                return (
                    [
                        SimpleNamespace(
                            id=str(uuid.UUID(int=99)),
                            payload={"owner_user_id": str(uuid.UUID(int=88))},
                            vector=[0.1, 0.2],
                        )
                    ],
                    None,
                )

        with self.assertRaisesRegex(RuntimeError, "foreign-owner"):
            qdrant_signature(
                FakeQdrant(), collection="memory_claim_v1", owner=uuid.UUID(OWNER)
            )


if __name__ == "__main__":
    unittest.main()
