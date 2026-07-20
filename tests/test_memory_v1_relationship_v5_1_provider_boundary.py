from __future__ import annotations

import unittest

from scripts.memory_v1_relational_extraction_v5_local_provider import (
    _explicit_current_relationship_state,
    _relationship_v5_1_repair_entities_and_roles,
    _relationship_v5_1_raw_packet_boundary,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    TrustedExtractionSource,
    canonical_sha256,
    sha256_text,
)
from scripts.memory_v1_predicate_registry_v5_1 import (
    DEFAULT_INTEGRATION,
    provider_registry,
)


REGISTRY = {
    "predicates": [
        {
            "predicate": "relationship.training_partner_of",
            "relationship_policy": {
                "named_party_subject_roles": [],
                "named_party_object_roles": [],
                "named_party_symmetric_roles": [
                    "lifting_partner",
                    "training_partner",
                    "workout_partner",
                ],
            },
        }
    ]
}


class RelationshipProviderBoundaryTest(unittest.TestCase):
    @staticmethod
    def source(text: str) -> TrustedExtractionSource:
        return TrustedExtractionSource.create(
            job_id="00000000-0000-0000-0000-000000000001",
            source_system="public.chat_log",
            source_external_id="00000000-0000-0000-0000-000000000002",
            source_sha256=sha256_text(text),
            source_recorded_at="2026-07-20T12:00:00+00:00",
            content=text,
        )

    def test_unknown_deferral_reason_fails_closed_and_is_hashed(self) -> None:
        raw = {
            "deferrals": [
                {
                    "reason_code": "explicit_current_state_required",
                }
            ]
        }
        normalized, audit = _relationship_v5_1_raw_packet_boundary(
            raw, REGISTRY
        )
        self.assertEqual(
            normalized["deferrals"][0]["reason_code"],
            "sensitive_manual_review",
        )
        self.assertEqual(audit["normalized_unknown_deferral_reason_count"], 1)
        self.assertEqual(
            audit["normalized_unknown_deferral_reason_sha256s"],
            [canonical_sha256("explicit_current_state_required")],
        )
        self.assertEqual(
            raw["deferrals"][0]["reason_code"],
            "explicit_current_state_required",
        )

    def test_legal_deferral_reason_is_unchanged(self) -> None:
        raw = {"deferrals": [{"reason_code": "insufficient_evidence"}]}
        normalized, audit = _relationship_v5_1_raw_packet_boundary(
            raw, REGISTRY
        )
        self.assertEqual(normalized, raw)
        self.assertEqual(audit["normalized_unknown_deferral_reason_count"], 0)

    def test_relationship_shape_exposes_only_registered_taxonomy(self) -> None:
        raw = {
            "deferrals": [],
            "entity_mentions": [
                {
                    "entity_ref": "e00",
                    "entity_type": "self",
                    "relationship_role": "user:self",
                },
                {
                    "entity_ref": "e01",
                    "entity_type": "person",
                    "relationship_role": "social:workout_partner",
                },
            ],
            "observations": [
                {
                    "predicate": "relationship.training_partner_of",
                    "subject_entity_ref": "e00",
                    "object": {"kind": "entity", "entity_ref": "e01"},
                }
            ],
        }
        _, audit = _relationship_v5_1_raw_packet_boundary(raw, REGISTRY)
        self.assertEqual(audit["relationship_shape_count"], 1)
        self.assertEqual(
            audit["relationship_shapes"],
            [
                {
                    "predicate": "relationship.training_partner_of",
                    "subject_entity_type": "self",
                    "object_entity_type": "person",
                    "registered_named_party_roles": ["workout_partner"],
                    "unregistered_role_sha256": None,
                }
            ],
        )

    def test_dual_role_person_gets_predicate_specific_role(self) -> None:
        text = "I am the primary caregiver for my father Jerry."
        source = self.source(text)
        entities = [
            {
                "entity_ref": "e00",
                "entity_type": "self",
                "relationship_role": "user:self",
            },
            {
                "entity_ref": "e01",
                "entity_type": "person",
                "relationship_role": "family:father",
            },
        ]
        observation = {
            "predicate": "relationship.caregiver_for",
            "subject_entity_ref": "e00",
            "object": {"kind": "entity", "entity_ref": "e01"},
            "source_spans": [
                {"start": 0, "end": len(text), "quote": text}
            ],
        }
        repairs = _relationship_v5_1_repair_entities_and_roles(
            source,
            observation,
            entities,
            provider_registry(DEFAULT_INTEGRATION),
        )
        self.assertIn("relationship_named_party_role_augmented", repairs)
        self.assertIn("care_recipient", entities[1]["relationship_role"])

    def test_missing_social_person_is_repaired_only_from_explicit_name(self) -> None:
        text = "There has been ongoing tension between me and Robin for several months."
        source = self.source(text)
        entities = [
            {
                "entity_ref": "e00",
                "entity_type": "self",
                "relationship_role": "user:self",
            }
        ]
        observation = {
            "predicate": "social.experiences_tension_with",
            "subject_entity_ref": "e00",
            "object": {"kind": "entity", "entity_ref": "e01"},
            "source_spans": [
                {"start": 0, "end": len(text), "quote": text}
            ],
        }
        repairs = _relationship_v5_1_repair_entities_and_roles(
            source,
            observation,
            entities,
            provider_registry(DEFAULT_INTEGRATION),
        )
        self.assertIn("relationship_named_entity_link", repairs)
        self.assertEqual(entities[1]["entity_ref"], "e01")
        self.assertEqual(entities[1]["name_text"], "Robin")
        self.assertIn("person_in_tension", entities[1]["relationship_role"])

    def test_wrong_nearby_predicate_is_reconciled_from_explicit_source(self) -> None:
        text = "I currently live with Jordan."
        source = self.source(text)
        entities = [
            {
                "entity_ref": "e00",
                "entity_type": "self",
                "relationship_role": "user:self",
            },
            {
                "entity_ref": "e01",
                "entity_type": "person",
                "relationship_role": "social:roommate",
            },
        ]
        observation = {
            "predicate": "relationship.roommate_of",
            "subject_entity_ref": "e00",
            "object": {"kind": "entity", "entity_ref": "e01"},
            "source_spans": [
                {"start": 0, "end": len(text), "quote": text}
            ],
        }
        repairs = _relationship_v5_1_repair_entities_and_roles(
            source,
            observation,
            entities,
            provider_registry(DEFAULT_INTEGRATION),
        )
        self.assertIn("relationship_predicate_source_reconciled", repairs)
        self.assertEqual(observation["predicate"], "relationship.lives_with")

    def test_entity_object_is_repaired_from_two_explicit_endpoints(self) -> None:
        text = "My father Jerry currently depends on me for transportation."
        source = self.source(text)
        entities = [
            {
                "entity_ref": "e00",
                "entity_type": "self",
                "relationship_role": "user:self",
            },
            {
                "entity_ref": "e01",
                "entity_type": "person",
                "relationship_role": "family:father",
            },
        ]
        observation = {
            "predicate": "social.depends_on",
            "subject_entity_ref": "e01",
            "object": {
                "kind": "literal",
                "datatype": "text",
                "value": "transportation",
                "unit": None,
                "approximate": False,
            },
            "source_spans": [
                {"start": 0, "end": len(text), "quote": text}
            ],
        }
        repairs = _relationship_v5_1_repair_entities_and_roles(
            source,
            observation,
            entities,
            provider_registry(DEFAULT_INTEGRATION),
        )
        self.assertIn("relationship_entity_object_repaired", repairs)
        self.assertEqual(
            observation["object"],
            {"kind": "entity", "entity_ref": "e00"},
        )

    def test_no_longer_state_is_explicit_transition(self) -> None:
        self.assertTrue(
            _explicit_current_relationship_state(
                "I no longer avoid Drew; we reconciled last week."
            )
        )

    def test_explicit_doctor_word_repairs_provider_role(self) -> None:
        text = "Dr. Chen is my primary care doctor."
        source = self.source(text)
        entities = [
            {
                "entity_ref": "e00",
                "entity_type": "self",
                "relationship_role": "user:self",
            },
            {
                "entity_ref": "e01",
                "entity_type": "person",
                "relationship_role": "medical:primary_care_physician",
            },
        ]
        observation = {
            "predicate": "relationship.healthcare_provider_for",
            "subject_entity_ref": "e00",
            "object": {"kind": "entity", "entity_ref": "e01"},
            "source_spans": [
                {"start": 0, "end": len(text), "quote": text}
            ],
        }
        repairs = _relationship_v5_1_repair_entities_and_roles(
            source,
            observation,
            entities,
            provider_registry(DEFAULT_INTEGRATION),
        )
        self.assertIn("relationship_source_role_augmented", repairs)
        self.assertIn("doctor", entities[1]["relationship_role"])

    def test_negated_trust_is_reconciled_to_reported_distrust(self) -> None:
        text = "I do not trust Alex anymore."
        source = self.source(text)
        entities = [
            {
                "entity_ref": "e00",
                "entity_type": "self",
                "relationship_role": "user:self",
            },
            {
                "entity_ref": "e01",
                "entity_type": "person",
                "relationship_role": "social:person_trusted",
            },
        ]
        observation = {
            "predicate": "social.trusts",
            "subject_entity_ref": "e00",
            "object": {"kind": "entity", "entity_ref": "e01"},
            "polarity": "negated",
            "modality": "negated",
            "source_spans": [
                {"start": 0, "end": len(text), "quote": text}
            ],
        }
        repairs = _relationship_v5_1_repair_entities_and_roles(
            source,
            observation,
            entities,
            provider_registry(DEFAULT_INTEGRATION),
        )
        self.assertIn("negated_trust_to_reported_distrust", repairs)
        self.assertEqual(observation["predicate"], "social.distrusts")
        self.assertEqual(observation["polarity"], "affirmed")
        self.assertEqual(observation["modality"], "reported_observation")

    def test_reconciliation_uses_full_trusted_record_not_model_span(self) -> None:
        text = "Ruth is my mother-in-law."
        source = self.source(text)
        entities = [
            {
                "entity_ref": "e00",
                "entity_type": "self",
                "relationship_role": "user:self",
            },
            {
                "entity_ref": "e01",
                "entity_type": "person",
                "relationship_role": "family:mother_in_law",
            },
        ]
        truncated_end = text.index("-in-law")
        observation = {
            "predicate": "relationship.mentor_of",
            "subject_entity_ref": "e00",
            "object": {"kind": "entity", "entity_ref": "e01"},
            "source_spans": [
                {
                    "start": 0,
                    "end": truncated_end,
                    "quote": text[:truncated_end],
                }
            ],
        }
        repairs = _relationship_v5_1_repair_entities_and_roles(
            source,
            observation,
            entities,
            provider_registry(DEFAULT_INTEGRATION),
        )
        self.assertIn("relationship_predicate_source_reconciled", repairs)
        self.assertEqual(observation["predicate"], "relationship.in_law_of")


if __name__ == "__main__":
    unittest.main()
