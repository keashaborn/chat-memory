from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import memory_v1_role_only_family_apply_v5_1 as runner  # noqa: E402


def manifest(action: str, bindings: int = 5) -> dict[str, object]:
    if action == "create_new":
        expected_tables = {
            "entity": 1,
            "entity_resolution_plan": 1,
            "entity_resolution_review": 1,
            "entity_resolution_apply": 1,
            "entity_role_resolution_v5_1": 1,
            "observation_entity_binding": bindings,
            "relational_operation_request": 2,
        }
    else:
        expected_tables = {
            "entity": 0,
            "entity_resolution_candidate": 1,
            "entity_resolution_plan": 1,
            "entity_resolution_review": 1,
            "entity_resolution_apply": 1,
            "entity_role_resolution_v5_1": 1,
            "observation_entity_binding": bindings,
            "relational_operation_request": 2,
        }
    value: dict[str, object] = {
        "contract_version": runner.CONTRACT,
        "owner_user_id": "1240822d-ac9a-4096-95aa-e2b24d36ef50",
        "required_head_commit": "4" * 40,
        "source_resolution_id": "11111111-1111-4111-8111-111111111111",
        "successor_resolution_id": "22222222-2222-4222-8222-222222222222",
        "reconcile_request_id": "33333333-3333-4333-8333-333333333333",
        "review_request_id": "44444444-4444-4444-8444-444444444444",
        "apply_request_id": "55555555-5555-4555-8555-555555555555",
        "reconcile_reason": "Owner-scoped family role reconciliation.",
        "review_reason": "Reviewed owner-scoped family role reconciliation.",
        "expected_relationship_role": "family:father",
        "expected_successor_action": action,
        "expected_bindings": bindings,
        "expected_new_rows": sum(expected_tables.values()),
        "expected_table_rows": expected_tables,
        "mention_sha256": "1" * 64,
        "candidate_set_sha256": "2" * 64,
        "successor_decision_sha256": "3" * 64,
        "reconciliation_manifest_sha256": "4" * 64,
        "database_writes": 0,
        "external_model_calls": 0,
        "qdrant_writes": 0,
        "prompt_influence": 0,
        "manifest_sha256": "",
    }
    value["manifest_sha256"] = runner.sha256(
        {key: item for key, item in value.items() if key != "manifest_sha256"}
    )
    return value


class FamilyRoleManifestTests(unittest.TestCase):
    def write_manifest(self, directory: str, value: dict[str, object], name: str) -> Path:
        path = Path(directory) / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return path

    def test_runner_accepts_both_successor_actions(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            runner, "private_path", side_effect=lambda value, must_exist: Path(value)
        ):
            for action in ("create_new", "link_existing"):
                path = self.write_manifest(directory, manifest(action), f"{action}.json")
                loaded = runner.load_manifest(str(path))
                self.assertEqual(loaded["expected_successor_action"], action)

    def test_link_existing_requires_candidate_and_zero_entity_rows(self) -> None:
        value = manifest("link_existing")
        tables = value["expected_table_rows"]
        assert isinstance(tables, dict)
        tables["entity"] = 1
        value["manifest_sha256"] = runner.sha256(
            {key: item for key, item in value.items() if key != "manifest_sha256"}
        )
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            runner, "private_path", side_effect=lambda item, must_exist: Path(item)
        ):
            path = self.write_manifest(directory, value, "invalid.json")
            with self.assertRaisesRegex(RuntimeError, "boundary or hash mismatch"):
                runner.load_manifest(str(path))


if __name__ == "__main__":
    unittest.main()
