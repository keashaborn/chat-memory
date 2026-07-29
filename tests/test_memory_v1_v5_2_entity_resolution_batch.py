from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from scripts.memory_v1_v5_2_entity_resolution_batch import (
    EntityResolutionBatchError,
    load_manifest,
)
from scripts.memory_v1_v5_stage_batch import StageBatchError


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
SELF_RESOLUTION = "af73eab2-c33c-4dd5-befb-75c78c197825"
SOURCE_RESOLUTION = "f3d091e2-0fb9-4889-89c9-918903a82570"
SUCCESSOR_RESOLUTION = "d05adb80-26b8-5c7a-a4c0-53f0df11e1a7"
ENTITY = "3cf07024-5b6b-4ae0-b453-b6cae4860720"


class V52EntityResolutionBatchManifestTest(unittest.TestCase):
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
            "contract_version": "memory_v1_v5_2_entity_resolution_batch_manifest_v1",
            "target_server": "seebx",
            "owner_user_id": OWNER,
            "expected_total_bindings": 2,
            "expected_new_rows": 12,
            "items": [
                {
                    "resolution_id": SELF_RESOLUTION,
                    "operation": "auto_apply",
                    "expected_action": "link_existing",
                    "expected_decision_state": "auto_link_eligible",
                    "review_reason": None,
                },
                {
                    "resolution_id": SOURCE_RESOLUTION,
                    "successor_resolution_id": SUCCESSOR_RESOLUTION,
                    "operation": "reconcile_existing_and_apply",
                    "expected_action": "create_new",
                    "expected_decision_state": "manual_review_required",
                    "expected_entity_id": ENTITY,
                    "review_reason": (
                        "The unique active owner entity has prior governed role history "
                        "for the exact relationship role."
                    ),
                },
            ],
        }

    def test_accepts_exact_v5_2_row_budget(self) -> None:
        metadata, _, _ = load_manifest(str(self.write(self.manifest())), root=self.root)
        self.assertEqual(metadata["expected_new_rows"], 12)

    def test_rejects_false_row_budget(self) -> None:
        value = self.manifest()
        value["expected_new_rows"] = 11
        with self.assertRaisesRegex(EntityResolutionBatchError, "row"):
            load_manifest(str(self.write(value)), root=self.root)

    def test_rejects_create_new_without_successor(self) -> None:
        value = self.manifest()
        del value["items"][1]["successor_resolution_id"]
        with self.assertRaisesRegex(StageBatchError, "fields"):
            load_manifest(str(self.write(value)), root=self.root)

    def test_rejects_legacy_operation(self) -> None:
        value = self.manifest()
        value["items"][1]["operation"] = "approve_and_apply"
        with self.assertRaisesRegex(StageBatchError, "fields"):
            load_manifest(str(self.write(value)), root=self.root)

    def test_accepts_manual_create_new_with_exact_row_budget(self) -> None:
        value = self.manifest()
        value["expected_total_bindings"] = 1
        value["expected_new_rows"] = 7
        value["items"] = [
            {
                "resolution_id": SOURCE_RESOLUTION,
                "operation": "manual_create_new_and_apply",
                "expected_action": "create_new",
                "expected_decision_state": "manual_review_required",
                "review_reason": (
                    "The exact named owner-scoped entity has no matching candidate."
                ),
            }
        ]
        metadata, _, _ = load_manifest(str(self.write(value)), root=self.root)
        self.assertEqual(metadata["expected_new_rows"], 7)

    def test_rejects_manual_create_new_with_link_action(self) -> None:
        value = self.manifest()
        value["expected_total_bindings"] = 1
        value["expected_new_rows"] = 7
        value["items"] = [
            {
                "resolution_id": SOURCE_RESOLUTION,
                "operation": "manual_create_new_and_apply",
                "expected_action": "link_existing",
                "expected_decision_state": "manual_review_required",
                "review_reason": "A review reason exists.",
            }
        ]
        with self.assertRaisesRegex(
            EntityResolutionBatchError, "manual-create-new"
        ):
            load_manifest(str(self.write(value)), root=self.root)

    def test_rejects_manual_create_new_without_review_reason(self) -> None:
        value = self.manifest()
        value["expected_total_bindings"] = 1
        value["expected_new_rows"] = 7
        value["items"] = [
            {
                "resolution_id": SOURCE_RESOLUTION,
                "operation": "manual_create_new_and_apply",
                "expected_action": "create_new",
                "expected_decision_state": "manual_review_required",
                "review_reason": None,
            }
        ]
        with self.assertRaisesRegex(
            EntityResolutionBatchError, "manual-create-new"
        ):
            load_manifest(str(self.write(value)), root=self.root)

    def test_rejects_reused_successor_id(self) -> None:
        value = self.manifest()
        value["items"][1]["successor_resolution_id"] = SELF_RESOLUTION
        with self.assertRaisesRegex(EntityResolutionBatchError, "reused"):
            load_manifest(str(self.write(value)), root=self.root)

    def test_accepts_manual_link_existing_with_exact_row_budget(self) -> None:
        value = self.manifest()
        value["expected_total_bindings"] = 1
        value["expected_new_rows"] = 6
        value["items"] = [
            {
                "resolution_id": SOURCE_RESOLUTION,
                "operation": "manual_link_existing_and_apply",
                "expected_action": "link_existing",
                "expected_decision_state": "manual_review_required",
                "expected_entity_id": ENTITY,
                "review_reason": (
                    "The reviewed correction uniquely identifies the existing "
                    "owner-scoped entity."
                ),
            }
        ]
        metadata, _, _ = load_manifest(str(self.write(value)), root=self.root)
        self.assertEqual(metadata["expected_new_rows"], 6)

    def test_rejects_manual_link_without_expected_entity(self) -> None:
        value = self.manifest()
        value["expected_total_bindings"] = 1
        value["expected_new_rows"] = 6
        value["items"] = [
            {
                "resolution_id": SOURCE_RESOLUTION,
                "operation": "manual_link_existing_and_apply",
                "expected_action": "link_existing",
                "expected_decision_state": "manual_review_required",
                "review_reason": "A review reason exists.",
            }
        ]
        with self.assertRaisesRegex(StageBatchError, "fields"):
            load_manifest(str(self.write(value)), root=self.root)

    def test_rejects_manual_link_without_review_reason(self) -> None:
        value = self.manifest()
        value["expected_total_bindings"] = 1
        value["expected_new_rows"] = 6
        value["items"] = [
            {
                "resolution_id": SOURCE_RESOLUTION,
                "operation": "manual_link_existing_and_apply",
                "expected_action": "link_existing",
                "expected_decision_state": "manual_review_required",
                "expected_entity_id": ENTITY,
                "review_reason": None,
            }
        ]
        with self.assertRaisesRegex(
            EntityResolutionBatchError, "manual-link-existing"
        ):
            load_manifest(str(self.write(value)), root=self.root)


if __name__ == "__main__":
    unittest.main()
