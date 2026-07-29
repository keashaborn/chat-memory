from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from scripts.memory_v1_v5_2_atom_admission_manifest_batch import (
    AtomManifestError,
    load_spec,
    validate_plan,
)


OWNER = "11111111-1111-4111-8111-111111111111"
PACKET = "22222222-2222-4222-8222-222222222222"
EVIDENCE = "33333333-3333-4333-8333-333333333333"
COUNTS = {
    "source_atom_count": 3,
    "admitted_entity_mention_count": 1,
    "admitted_observation_count": 1,
    "admitted_comparison_hint_count": 0,
    "deferred_atom_count": 1,
    "retained_source_only_count": 0,
    "rejected_atom_count": 0,
}


def spec_value() -> dict:
    return {
        "contract_version": "memory_v1_v5_2_atom_admission_spec_v1",
        "target_server": "seebx",
        "owner_user_id": OWNER,
        "items": [
            {
                "case_id": "pet_identity",
                "packet_id": PACKET,
                "evidence_id": EVIDENCE,
                "expected_counts": COUNTS,
                "expected_entity_refs": ["e00"],
                "expected_predicates": ["identity.name"],
                "review_decision": "authorized",
                "review_reason_codes": ["explicit_named_pet_atom_reviewed"],
            }
        ],
    }


def plan_value() -> dict:
    return {
        "source_packet_id": PACKET,
        "source_evidence_id": EVIDENCE,
        "counts": COUNTS,
        "source_packet_storage_sha256": "a" * 64,
        "proposal_sha256": "b" * 64,
        "stage_projection": {
            "entity_mentions": [{"entity_ref": "e00"}],
            "observations": [
                {"observation_ref": "o00", "predicate": "identity.name"}
            ],
            "deferrals": [],
            "packet_findings": [],
        },
    }


class AtomAdmissionManifestBatchTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "spec.json"

    def write_spec(self, value: dict) -> None:
        self.path.write_text(json.dumps(value), encoding="utf-8")
        os.chmod(self.path, 0o644)

    def test_loads_bounded_authorized_spec(self):
        self.write_spec(spec_value())
        spec, digest = load_spec(str(self.path), self.root)
        self.assertEqual(spec["owner_user_id"], OWNER)
        self.assertEqual(spec["items"][0]["packet_id"], PACKET)
        self.assertEqual(len(digest), 64)

    def test_rejects_duplicate_packet(self):
        value = spec_value()
        duplicate = dict(value["items"][0])
        duplicate["case_id"] = "duplicate"
        value["items"].append(duplicate)
        self.write_spec(value)
        with self.assertRaisesRegex(AtomManifestError, "invalid"):
            load_spec(str(self.path), self.root)

    def test_rejects_authorized_plan_without_observation(self):
        value = spec_value()
        value["items"][0]["expected_counts"] = {
            **COUNTS,
            "admitted_observation_count": 0,
        }
        self.write_spec(value)
        with self.assertRaisesRegex(AtomManifestError, "no observation"):
            load_spec(str(self.path), self.root)

    def test_validates_exact_projection(self):
        self.write_spec(spec_value())
        spec, _ = load_spec(str(self.path), self.root)
        validate_plan(spec["items"][0], plan_value())

    def test_rejects_predicate_drift(self):
        self.write_spec(spec_value())
        spec, _ = load_spec(str(self.path), self.root)
        plan = plan_value()
        plan["stage_projection"]["observations"][0]["predicate"] = "life_event.died"
        with self.assertRaisesRegex(AtomManifestError, "predicates drifted"):
            validate_plan(spec["items"][0], plan)

    def test_rejects_projection_with_deferred_material(self):
        self.write_spec(spec_value())
        spec, _ = load_spec(str(self.path), self.root)
        plan = plan_value()
        plan["stage_projection"]["deferrals"] = [{"atom_ref": "d00"}]
        with self.assertRaisesRegex(AtomManifestError, "retained deferred"):
            validate_plan(spec["items"][0], plan)


if __name__ == "__main__":
    unittest.main()
