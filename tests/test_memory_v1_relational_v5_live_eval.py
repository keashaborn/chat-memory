from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from scripts.memory_v1_relational_v5_live_eval import (
    ModelPacket,
    enrich_packet,
    evaluate_case,
)


def temporal_observation() -> dict:
    return {
        "semantic": "observation_time",
        "shape": "none",
        "basis": "none",
        "source_form": "none",
        "certainty": "unknown",
        "precision": "unknown",
        "instant": None,
        "calendar_range": None,
        "instant_range": None,
        "relative_offset": None,
        "recurrence": None,
        "anchored_to_source_time": False,
        "reason_codes": [],
    }


class V5LiveEvalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = json.loads(
            Path("specs/memory_v1_predicate_registry_v5.json").read_text(encoding="utf-8")
        )
        cls.text = "My cat's correct name is Koda."
        cls.source = {
            "job_id": "600e5563-5ffe-4421-adcc-2b235028879f",
            "source_external_id": "3af6fbf1-4ac2-49e6-aef3-a308c6157ee0",
            "source_sha256": hashlib.sha256(cls.text.encode()).hexdigest(),
            "source_recorded_at": "2026-07-14T17:20:49.923120+00:00",
        }

    def correction_packet(self, predicate: str = "identity.name_canonical") -> ModelPacket:
        return ModelPacket.model_validate(
            {
                "entity_mentions": [
                    {
                        "entity_ref": "e01",
                        "entity_type": "animal",
                        "mention_kind": "named",
                        "name_text": "Koda",
                        "relationship_role": "pet:corrected_name_subject",
                        "source_spans": [
                            {"start": 0, "end": len(self.text) + 4, "quote": self.text}
                        ],
                        "extraction_confidence": 0.99,
                        "reason_codes": ["explicit_name_correction"],
                    }
                ],
                "observations": [
                    {
                        "observation_ref": "o01",
                        "subject_entity_ref": "e01",
                        "predicate": predicate,
                        "object": {
                            "kind": "literal",
                            "datatype": "text",
                            "value": "Koda",
                            "unit": None,
                            "approximate": False,
                        },
                        "polarity": "affirmed",
                        "modality": "corrective",
                        "projection_class": "correction",
                        "surface_policy": "normalization_only",
                        "temporal": temporal_observation(),
                        "sensitivity": "medium",
                        "extraction_confidence": 0.99,
                        "source_spans": [
                            {"start": 0, "end": len(self.text) + 4, "quote": self.text}
                        ],
                        "reason_codes": ["explicit_name_correction"],
                    }
                ],
                "comparison_hints": [
                    {
                        "observation_ref": "o01",
                        "relation_type": "corrects",
                        "target_lookup_key": "prior_name_for_same_pet",
                        "reason_codes": ["target_requires_owner_scoped_resolution"],
                    },
                    {
                        "observation_ref": "o01",
                        "relation_type": "supersedes",
                        "target_lookup_key": "prior_name_for_same_pet",
                        "reason_codes": ["target_requires_owner_scoped_resolution"],
                    },
                ],
                "deferrals": [],
                "packet_findings": [],
            }
        )

    def test_server_injects_authoritative_fields_and_span_hashes(self) -> None:
        packet, rejections = enrich_packet(
            self.correction_packet(),
            source=self.source,
            text=self.text,
            registry=self.registry,
        )
        self.assertEqual(rejections, [])
        self.assertEqual(packet["source_envelope"]["source_sha256"], self.source["source_sha256"])
        self.assertEqual(packet["predicate_registry_version"], "memory_predicate_registry_v5")
        observation = packet["observations"][0]
        self.assertEqual(observation["predicate_registry_status"], "governed")
        self.assertEqual(
            observation["source_spans"][0]["span_sha256"],
            hashlib.sha256(self.text.encode()).hexdigest(),
        )
        self.assertEqual(observation["project_scope"]["state"], "not_applicable")
        self.assertEqual(
            observation["temporal"]["instant"],
            "2026-07-14T17:20:49.923120Z",
        )
        self.assertIsNone(packet["comparison_hints"][0]["target_claim_id"])
        self.assertEqual(packet["comparison_hints"][0]["resolution_state"], "unresolved")

    def test_unknown_predicate_is_rejected_and_deferred(self) -> None:
        packet, rejections = enrich_packet(
            self.correction_packet(predicate="invented.name_fact"),
            source=self.source,
            text=self.text,
            registry=self.registry,
        )
        self.assertEqual(packet["observations"], [])
        self.assertTrue(any(item["reason_code"] == "unregistered_predicate" for item in packet["deferrals"]))
        self.assertTrue(any("not in the extraction-enabled" in item["reason"] for item in rejections))

    def test_invalid_diagnostic_code_does_not_discard_semantic_packet(self) -> None:
        payload = self.correction_packet().model_dump(mode="json")
        payload["packet_findings"] = ["Not snake case"]
        packet, rejections = enrich_packet(
            ModelPacket.model_validate(payload),
            source=self.source,
            text=self.text,
            registry=self.registry,
        )
        self.assertEqual(rejections, [])
        self.assertEqual(len(packet["observations"]), 1)
        self.assertIn("invalid_model_finding_normalized", packet["packet_findings"])

    def test_case_evaluator_detects_required_correction_semantics(self) -> None:
        packet, _ = enrich_packet(
            self.correction_packet(),
            source=self.source,
            text=self.text,
            registry=self.registry,
        )
        expected = {
            "outcome": "extract",
            "required_projection_classes": ["correction"],
            "forbidden_projection_classes": ["life_preference", "response_preference"],
            "required_entity_roles": ["pet:corrected_name_subject"],
            "required_predicate_families": ["identity.name_canonical"],
            "required_temporal_features": [],
            "required_comparison_relations": ["corrects", "supersedes"],
            "required_deferrals": [],
            "forbidden_predicates": ["raises.name"],
            "require_manual_review": True,
        }
        result = evaluate_case(packet, expected, self.registry)
        self.assertTrue(result["passed"], result["findings"])

    def test_evaluator_contains_no_runtime_mutation_calls(self) -> None:
        source = Path("scripts/memory_v1_relational_v5_live_eval.py").read_text(
            encoding="utf-8"
        )
        forbidden = (
            "INSERT INTO",
            "UPDATE memory.",
            "DELETE FROM",
            ".upsert(",
            ".delete(",
            "persist_extraction(",
            "propose_candidate(",
            "apply_candidate(",
        )
        for token in forbidden:
            self.assertNotIn(token, source)
        self.assertIn("store=False", source)
        self.assertIn("zero_write_proof(before, after)", source)


if __name__ == "__main__":
    unittest.main()
