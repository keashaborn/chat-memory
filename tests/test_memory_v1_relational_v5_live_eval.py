from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from scripts.memory_v1_relational_v5_live_eval import (
    ModelPacket,
    enforce_temporal_authority,
    enrich_packet,
    evaluate_case,
    normalize_canonical_relationship_direction,
    normalize_explicit_project_requirement,
    normalize_pet_relationship_roles,
    outcome,
    packet_integrity_reasons,
    packet_quality,
    source_span,
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

    def test_nearest_exact_quote_recovers_repeated_short_span(self) -> None:
        text = "I said I stayed"
        span = source_span({"start": 6, "end": 7, "quote": "I"}, text)
        self.assertEqual((span["start"], span["end"]), (7, 8))

    def test_self_reference_can_recover_distant_repeated_exact_quote(self) -> None:
        text = "I began here, and much later I stated another fact."
        span = source_span(
            {"start": 100, "end": 101, "quote": "I"},
            text,
            allow_repeated=True,
        )
        self.assertEqual(text[span["start"] : span["end"]], "I")

    def test_source_wrapper_trailing_newline_is_removed(self) -> None:
        span = source_span(
            {"start": 0, "end": len(self.text) + 1, "quote": self.text + "\n"},
            self.text,
        )
        self.assertEqual((span["start"], span["end"]), (0, len(self.text)))

    def test_empty_open_state_interval_is_anchored_to_trusted_source(self) -> None:
        temporal = temporal_observation()
        temporal.update({"semantic": "state_validity", "shape": "open_interval"})
        enforce_temporal_authority(temporal, self.source["source_recorded_at"])
        self.assertEqual(temporal["basis"], "instant")
        self.assertEqual(
            temporal["instant_range"],
            {"lower": "2026-07-14T17:20:49.923120Z", "upper": None, "bounds": "[)"},
        )

    def test_explicit_project_commitment_normalizes_one_proposal(self) -> None:
        text = "I would love exact owner isolation. A graph could be useful."
        observations = [
            {
                "predicate": "project.proposed_feature",
                "modality": "proposed",
                "source_spans": [{"start": 0, "end": 35, "quote": "I would love exact owner isolation."}],
                "temporal": temporal_observation(),
            },
            {
                "predicate": "project.proposed_feature",
                "modality": "proposed",
                "source_spans": [{"start": 36, "end": len(text), "quote": "A graph could be useful."}],
                "temporal": temporal_observation(),
            },
        ]
        self.assertTrue(normalize_explicit_project_requirement(observations, text))
        self.assertEqual(observations[0]["predicate"], "project.requirement")
        self.assertEqual(observations[0]["modality"], "endorsed")
        self.assertEqual(observations[1]["predicate"], "project.proposed_feature")

    def test_non_memory_project_scope_deferral_does_not_change_outcome(self) -> None:
        packet = {
            "observations": [],
            "deferrals": [
                {"reason_code": "question_only", "memory_shape": "none"},
                {"reason_code": "project_scope_unresolved", "memory_shape": "none"},
            ],
        }
        self.assertEqual(outcome(packet), "no_observation")
        packet["deferrals"][1]["memory_shape"] = "project_knowledge"
        self.assertEqual(outcome(packet), "defer_all")

    def test_transient_question_ignores_non_authoritative_context_deferral(self) -> None:
        packet = {
            "observations": [],
            "deferrals": [
                {"reason_code": "question_only", "memory_shape": "none"},
                {"reason_code": "transient_state", "memory_shape": "none"},
                {"reason_code": "context_missing", "memory_shape": "none"},
            ],
        }
        self.assertEqual(outcome(packet), "no_observation")

    def test_sibling_direction_is_canonicalized_self_to_person(self) -> None:
        entities = {
            "e01": {"entity_type": "self"},
            "e02": {"entity_type": "person"},
        }
        observations = [
            {
                "predicate": "relationship.sibling_of",
                "subject_entity_ref": "e02",
                "object": {"kind": "entity", "entity_ref": "e01"},
            }
        ]
        self.assertTrue(
            normalize_canonical_relationship_direction(observations, entities)
        )
        self.assertEqual(observations[0]["subject_entity_ref"], "e01")
        self.assertEqual(observations[0]["object"]["entity_ref"], "e02")

    def test_pet_roles_derive_from_accepted_relationships_and_death(self) -> None:
        entities = {
            "e01": {"entity_type": "self", "relationship_role": "user:self"},
            "e02": {"entity_type": "animal", "relationship_role": None},
            "e03": {"entity_type": "animal", "relationship_role": None},
            "e04": {"entity_type": "animal", "relationship_role": None},
            "e05": {"entity_type": "animal", "relationship_role": None},
        }
        observations = [
            {
                "predicate": "relationship.has_pet",
                "subject_entity_ref": "e01",
                "object": {"kind": "entity", "entity_ref": "e02"},
                "polarity": "affirmed",
                "temporal": {"semantic": "state_validity"},
            },
            {
                "predicate": "relationship.has_pet",
                "subject_entity_ref": "e01",
                "object": {"kind": "entity", "entity_ref": "e03"},
                "polarity": "affirmed",
                "temporal": {"semantic": "state_validity"},
            },
            {
                "predicate": "relationship.has_pet",
                "subject_entity_ref": "e01",
                "object": {"kind": "entity", "entity_ref": "e04"},
                "polarity": "affirmed",
                "temporal": {"semantic": "state_validity"},
            },
            {
                "predicate": "life_event.died",
                "subject_entity_ref": "e04",
                "object": {
                    "kind": "literal",
                    "datatype": "boolean",
                    "value": True,
                },
                "polarity": "affirmed",
                "temporal": {"semantic": "occurrence"},
            },
            {
                "predicate": "life_event.died",
                "subject_entity_ref": "e05",
                "object": {
                    "kind": "literal",
                    "datatype": "boolean",
                    "value": True,
                },
                "polarity": "affirmed",
                "temporal": {"semantic": "occurrence"},
            },
        ]
        self.assertTrue(normalize_pet_relationship_roles(entities, observations))
        self.assertEqual(entities["e02"]["relationship_role"], "pet:current:1")
        self.assertEqual(entities["e03"]["relationship_role"], "pet:current:2")
        self.assertEqual(entities["e04"]["relationship_role"], "pet:deceased")
        self.assertIsNone(entities["e05"]["relationship_role"])

    def test_registry_supports_owner_relationships_and_planned_health(self) -> None:
        rows = {item["predicate"]: item for item in self.registry["predicates"]}
        parent_contract = self.registry["object_contracts"][
            rows["relationship.parent_of"]["object_contract"]
        ]
        self.assertIn("self", parent_contract["entity_types"])
        self.assertIn("self", rows["relationship.sibling_of"]["subject_entity_types"])
        health = rows["health.user_reported_observation"]
        self.assertIn("planned", health["modalities"])
        self.assertIn("planned_time", health["temporal_semantics"])

    def test_integrity_repair_detects_dangling_project_deferral(self) -> None:
        packet = {
            "entity_mentions": [],
            "observations": [],
            "deferrals": [
                {
                    "reason_code": "project_scope_unresolved",
                    "memory_shape": "project_knowledge",
                }
            ],
        }
        self.assertIn(
            "project_scope_deferral_without_project_entity_and_observation",
            packet_integrity_reasons(packet, []),
        )

    def test_integrity_repair_rechecks_animal_structured_domain(self) -> None:
        packet = {
            "entity_mentions": [{"entity_type": "animal"}],
            "observations": [],
            "deferrals": [
                {"reason_code": "structured_domain", "memory_shape": "none"}
            ],
        }
        self.assertIn(
            "recheck_conversational_veterinary_plan_routing",
            packet_integrity_reasons(packet, []),
        )

    def test_packet_quality_prefers_fewer_deterministic_rejections(self) -> None:
        packet = {
            "entity_mentions": [],
            "observations": [],
            "deferrals": [],
        }
        rejected = [
            {"kind": "observation", "ref": "o01", "reason": "dangling reference"}
        ]
        self.assertLess(packet_quality(packet, []), packet_quality(packet, rejected))

    def test_correction_authority_adds_unresolved_relations_and_role(self) -> None:
        payload = self.correction_packet().model_dump(mode="json")
        payload["entity_mentions"][0]["relationship_role"] = "pet:current:1"
        payload["comparison_hints"] = payload["comparison_hints"][:1]
        packet, rejections = enrich_packet(
            ModelPacket.model_validate(payload),
            source=self.source,
            text=self.text,
            registry=self.registry,
        )
        self.assertEqual(rejections, [])
        self.assertEqual(
            packet["entity_mentions"][0]["relationship_role"],
            "pet:corrected_name_subject",
        )
        self.assertEqual(
            {item["relation_type"] for item in packet["comparison_hints"]},
            {"corrects", "supersedes"},
        )
        self.assertTrue(
            all(item["target_claim_id"] is None for item in packet["comparison_hints"])
        )

    def test_high_sensitivity_observation_forces_manual_review_deferral(self) -> None:
        payload = self.correction_packet().model_dump(mode="json")
        payload["observations"][0]["sensitivity"] = "high"
        packet, rejections = enrich_packet(
            ModelPacket.model_validate(payload),
            source=self.source,
            text=self.text,
            registry=self.registry,
        )
        self.assertEqual(rejections, [])
        self.assertTrue(
            any(
                item["reason_code"] == "sensitive_manual_review"
                and item["review_required"]
                for item in packet["deferrals"]
            )
        )

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
