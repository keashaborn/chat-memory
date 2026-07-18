from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from scripts.memory_v1_v5_entity_resolution_batch import (
    EntityResolutionBatchError,
    load_manifest,
)


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
RESOLUTION = "6e6283dd-05e6-43e4-aee3-c4f48d947038"
ENTITY = "9378f68c-2087-4365-8a9a-a166faf1324e"


class EntityResolutionBatchManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, value: dict) -> Path:
        path = self.root / "manifest.json"
        path.write_text(json.dumps(value) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def manifest(self) -> dict:
        return {
            "contract_version": "memory_v1_v5_entity_resolution_batch_manifest_v1",
            "target_server": "seebx",
            "owner_user_id": OWNER,
            "expected_total_bindings": 2,
            "expected_new_rows": 4,
            "items": [
                {
                    "resolution_id": RESOLUTION,
                    "operation": "auto_apply",
                    "expected_action": "link_existing",
                    "expected_decision_state": "auto_link_eligible",
                    "review_reason": None,
                }
            ],
        }

    def test_accepts_exact_row_budget(self) -> None:
        metadata, _, _ = load_manifest(str(self.write(self.manifest())), root=self.root)
        self.assertEqual(metadata["expected_new_rows"], 4)

    def test_rejects_false_row_budget(self) -> None:
        value = self.manifest()
        value["expected_new_rows"] = 3
        with self.assertRaisesRegex(EntityResolutionBatchError, "row"):
            load_manifest(str(self.write(value)), root=self.root)

    def test_rejects_auto_apply_with_review_reason(self) -> None:
        value = self.manifest()
        value["items"][0]["review_reason"] = "not allowed"
        with self.assertRaisesRegex(EntityResolutionBatchError, "auto-apply"):
            load_manifest(str(self.write(value)), root=self.root)

    def test_rejects_duplicate_resolution(self) -> None:
        value = self.manifest()
        value["items"].append(dict(value["items"][0]))
        value["expected_new_rows"] = 6
        with self.assertRaisesRegex(EntityResolutionBatchError, "duplicate"):
            load_manifest(str(self.write(value)), root=self.root)

    def test_accepts_reviewed_existing_entity_with_exact_target(self) -> None:
        value = self.manifest()
        value["expected_total_bindings"] = 1
        value["expected_new_rows"] = 6
        value["items"] = [
            {
                "resolution_id": RESOLUTION,
                "operation": "approve_existing_and_apply",
                "expected_action": "link_existing",
                "expected_decision_state": "manual_review_required",
                "review_reason": "Trusted project registry alias matches exactly.",
                "expected_entity_id": ENTITY,
            }
        ]
        metadata, _, _ = load_manifest(str(self.write(value)), root=self.root)
        self.assertEqual(metadata["expected_new_rows"], 6)
        self.assertEqual(metadata["items"][0]["expected_entity_id"], ENTITY)

    def test_rejects_reviewed_existing_entity_without_exact_target(self) -> None:
        value = self.manifest()
        value["expected_total_bindings"] = 1
        value["expected_new_rows"] = 6
        value["items"] = [
            {
                "resolution_id": RESOLUTION,
                "operation": "approve_existing_and_apply",
                "expected_action": "link_existing",
                "expected_decision_state": "manual_review_required",
                "review_reason": "Trusted project registry alias matches exactly.",
                "expected_entity_id": "not-a-uuid",
            }
        ]
        with self.assertRaisesRegex(EntityResolutionBatchError, "UUID"):
            load_manifest(str(self.write(value)), root=self.root)


if __name__ == "__main__":
    unittest.main()
