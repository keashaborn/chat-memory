from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
RUBRIC_PATH = (
    ROOT / "evals" / "memory_v1_v5_2_nuanced_belief_canary_20260722.json"
)
REGISTRY_PATH = ROOT / "specs" / "memory_v1_predicate_registry_v5_2.json"
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class NuancedBeliefCanaryRubricTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rubric = json.loads(RUBRIC_PATH.read_text(encoding="utf-8"))
        cls.registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))

    def test_source_binding_is_hash_locked_and_source_free(self) -> None:
        binding = self.rubric["source_binding"]
        uuid.UUID(binding["job_id"])
        uuid.UUID(binding["evidence_id"])
        self.assertRegex(binding["evidence_content_sha256"], SHA_RE)
        self.assertRegex(binding["owner_user_id_sha256"], SHA_RE)
        self.assertFalse(binding["source_prose_included"])
        self.assertNotIn("text", binding)
        self.assertEqual(
            binding["owner_user_id_sha256"],
            hashlib.sha256(
                b"1240822d-ac9a-4096-95aa-e2b24d36ef50"
            ).hexdigest(),
        )

    def test_stance_expectation_matches_registry_contract(self) -> None:
        stance = next(
            item
            for item in self.registry["predicates"]
            if item["predicate"] == "stance.reported"
        )
        expected = self.rubric["expected_packet"]["required_stance_contract"]
        self.assertIn(expected["modality"], stance["modalities"])
        self.assertIn(
            expected["projection_class"], stance["projection_classes"]
        )
        self.assertIn(expected["surface_policy"], stance["surface_policies"])
        self.assertEqual(stance["object_contract"], "literal.reported_stance")
        literal = self.registry["object_contracts"][stance["object_contract"]]
        self.assertEqual(literal["datatype"], expected["object_datatype"])
        self.assertEqual(
            set(literal["value_schema"]["required"]),
            set(expected["required_object_fields"]),
        )

    def test_rubric_is_high_precision_and_write_bounded(self) -> None:
        packet = self.rubric["expected_packet"]
        self.assertEqual(
            packet["predicate_counts"]["stance.reported"],
            {"minimum": 2, "maximum": 4},
        )
        self.assertIn(
            "health.user_reported_observation", packet["forbidden_predicates"]
        )
        self.assertIn("ambiguous_transcription", packet["forbidden_deferrals"])
        self.assertTrue(
            set(packet["allowed_deferrals"]).isdisjoint(
                packet["forbidden_deferrals"]
            )
        )
        boundary = self.rubric["write_boundary"]
        self.assertEqual(boundary["external_model_calls"], 0)
        self.assertEqual(boundary["staging_writes"], 0)
        self.assertEqual(boundary["claim_writes"], 0)
        self.assertEqual(boundary["qdrant_writes"], 0)
        self.assertEqual(boundary["prompt_influence"], 0)


if __name__ == "__main__":
    unittest.main()
