from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from scripts.memory_v1_v5_2_atom_admission_apply_v2 import (
    AdmissionError,
    CONTRACT,
    OWNER,
    POLICY,
    load_manifest,
    sha256_text,
    stable_json,
)


def item(seed: int, *, decision: str = "authorized") -> dict:
    prefix = f"{seed:08x}"
    value = {
        "case_id": f"case-{seed}",
        "packet_id": f"{prefix}-0000-4000-8000-000000000001",
        "evidence_id": f"{prefix}-0000-4000-8000-000000000002",
        "packet_storage_sha256": f"{seed:x}"[-1] * 64,
        "proposal_sha256": f"{seed + 1:x}"[-1] * 64,
        "expected_counts": {
            "source_atom_count": 6,
            "admitted_entity_mention_count": 2,
            "admitted_observation_count": 2 if decision == "authorized" else 0,
            "admitted_comparison_hint_count": 0,
            "deferred_atom_count": 2,
            "retained_source_only_count": 0 if decision == "authorized" else 4,
            "rejected_atom_count": 0,
        },
        "review_decision": decision,
        "review_reason_codes": ["exact_owner_review"],
        "proposal_operation_id": f"{prefix}-0000-4000-8000-000000000003",
        "proposal_id": f"{prefix}-0000-4000-8000-000000000004",
        "review_operation_id": f"{prefix}-0000-4000-8000-000000000005",
        "review_id": f"{prefix}-0000-4000-8000-000000000006",
        "apply_operation_id": None,
        "apply_id": None,
    }
    if decision == "authorized":
        value["apply_operation_id"] = (
            f"{prefix}-0000-4000-8000-000000000007"
        )
        value["apply_id"] = f"{prefix}-0000-4000-8000-000000000008"
    return value


def manifest(items: list[dict]) -> dict:
    authorized = sum(value["review_decision"] == "authorized" for value in items)
    value = {
        "contract_version": CONTRACT,
        "owner_user_id_sha256": sha256_text(str(OWNER)),
        "policy_version": POLICY,
        "required_base_commit": "a" * 40,
        "items": items,
        "expected_new_rows": {
            "v5_2_atom_admission_proposal": len(items),
            "v5_2_atom_admission_review": len(items),
            "v5_2_atom_admission_apply": authorized,
            "v5_2_atom_admission_operation": (len(items) * 2) + authorized,
            "relational_stage_batch": 0,
            "entity_mention": 0,
            "observation": 0,
            "claim": 0,
            "qdrant": 0,
        },
        "manifest_sha256": "",
    }
    value["manifest_sha256"] = sha256_text(
        stable_json(
            {key: item for key, item in value.items() if key != "manifest_sha256"}
        )
    )
    return value


class AtomAdmissionV2ManifestTest(unittest.TestCase):
    def write(self, value: dict) -> Path:
        directory = tempfile.mkdtemp()
        path = Path(directory) / "manifest.json"
        path.write_text(json.dumps(value) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        self.addCleanup(Path(directory).rmdir)
        self.addCleanup(path.unlink)
        return path

    def test_accepts_two_owner_authorized_items(self) -> None:
        value = load_manifest(self.write(manifest([item(1), item(2)])))
        self.assertEqual(value["expected_new_rows"]["v5_2_atom_admission_apply"], 2)
        self.assertEqual(value["expected_new_rows"]["v5_2_atom_admission_operation"], 6)

    def test_accepts_bounded_mixed_batch(self) -> None:
        value = load_manifest(
            self.write(manifest([item(1), item(2, decision="deferred")]))
        )
        self.assertEqual(value["expected_new_rows"]["v5_2_atom_admission_apply"], 1)
        self.assertEqual(value["expected_new_rows"]["v5_2_atom_admission_operation"], 5)

    def test_rejects_all_deferred_batch(self) -> None:
        value = manifest([item(1, decision="deferred")])
        with self.assertRaisesRegex(AdmissionError, "at least one"):
            load_manifest(self.write(value))

    def test_rejects_false_row_budget(self) -> None:
        value = manifest([item(1), item(2)])
        value["expected_new_rows"]["v5_2_atom_admission_operation"] = 5
        value["manifest_sha256"] = sha256_text(
            stable_json(
                {
                    key: item
                    for key, item in value.items()
                    if key != "manifest_sha256"
                }
            )
        )
        with self.assertRaisesRegex(AdmissionError, "row budget"):
            load_manifest(self.write(value))


if __name__ == "__main__":
    unittest.main()
