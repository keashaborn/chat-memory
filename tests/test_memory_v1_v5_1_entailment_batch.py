from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
import uuid

import memory_v1_v5_1_entailment_batch as batch


class EntailmentBatchManifestTest(unittest.TestCase):
    def manifest(self) -> dict:
        item = {
            "observation_id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()),
            "decision": "accepted",
            "reason_code": "predicate_entailment_v5_1_accepted",
            "source_spans": [{"start": 0, "end": 4, "span_sha256": "a" * 64}],
            "authorization_manifest_sha256": "b" * 64,
        }
        value = {
            "contract_version": batch.CONTRACT_VERSION,
            "owner_user_id": str(uuid.uuid4()),
            "required_head_commit": "c" * 40,
            "assessor_type": "system",
            "assessor_ref": "unit-test",
            "review_sha256": "d" * 64,
            "expected_new_rows": 2,
            "expected_table_rows": {
                "observation_entailment_v5": 1,
                "relational_operation_request": 1,
            },
            "items": [item],
            "manifest_sha256": "",
        }
        value["manifest_sha256"] = batch.sha256(
            {key: entry for key, entry in value.items() if key != "manifest_sha256"}
        )
        return value

    def write_manifest(self, value: dict) -> Path:
        directory = Path(tempfile.mkdtemp())
        path = directory / "manifest.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)
        return path

    def test_valid_manifest(self) -> None:
        value = self.manifest()
        self.assertEqual(batch.load_manifest(self.write_manifest(value)), value)

    def test_decision_reason_mismatch_fails_closed(self) -> None:
        value = self.manifest()
        value["items"][0]["reason_code"] = "predicate_semantics_unresolved"
        value["manifest_sha256"] = batch.sha256(
            {key: entry for key, entry in value.items() if key != "manifest_sha256"}
        )
        with self.assertRaises(batch.EntailmentBatchError):
            batch.load_manifest(self.write_manifest(value))

    def test_row_budget_mismatch_fails_closed(self) -> None:
        value = self.manifest()
        value["expected_new_rows"] = 3
        value["manifest_sha256"] = batch.sha256(
            {key: entry for key, entry in value.items() if key != "manifest_sha256"}
        )
        with self.assertRaises(batch.EntailmentBatchError):
            batch.load_manifest(self.write_manifest(value))


if __name__ == "__main__":
    unittest.main()
