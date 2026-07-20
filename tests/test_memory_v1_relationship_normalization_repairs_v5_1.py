from __future__ import annotations

import sys
import unittest
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.memory_v1_relational_extraction_v5_local_provider import (
    _relationship_v5_1_repair_entities_and_roles,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    TrustedExtractionSource,
    _normalize_temporal,
    sha256_text,
)
from scripts.memory_v1_relationship_observation_v5_1 import (
    normalize_relationship_observation,
    relationship_predicate_candidates_from_source,
)


def temporal() -> dict[str, object]:
    return {
        "semantic": "state_validity",
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


def observation(
    predicate: str,
    text: str,
    *,
    subject_ref: str = "e00",
    object_ref: str = "e01",
    modality: str = "asserted",
    polarity: str = "affirmed",
) -> dict[str, object]:
    return {
        "observation_ref": "o01",
        "subject_entity_ref": subject_ref,
        "predicate": predicate,
        "object": {"kind": "entity", "entity_ref": object_ref},
        "projection_class": "direct_claim",
        "surface_policy": "direct_or_relevant",
        "sensitivity": "low",
        "modality": modality,
        "polarity": polarity,
        "temporal": temporal(),
        "source_spans": [{"start": 0, "end": len(text)}],
        "reason_codes": [],
    }


class RelationshipNormalizationRepairsV51Test(unittest.TestCase):
    def test_girlfriend_overrides_wrong_spouse_proposal(self) -> None:
        self.assertEqual(
            relationship_predicate_candidates_from_source(
                "Jamie is my girlfriend.",
                "family:spouse",
            ),
            ("relationship.romantic_partner_of",),
        )

    def test_no_contact_is_affirmed_lexical_state(self) -> None:
        text = "I have had no contact with my uncle Ray since March."
        entities = [
            {
                "entity_ref": "e00",
                "entity_type": "self",
                "relationship_role": "user:self",
            },
            {
                "entity_ref": "e01",
                "entity_type": "person",
                "relationship_role": "family:uncle|social:no_contact_person",
            },
        ]
        observation_value = observation(
            "social.no_contact_with",
            text,
            modality="negated",
            polarity="negated",
        )
        observation_value["temporal"].update(
            {
                "shape": "bounded_interval",
                "basis": "calendar",
                "source_form": "partial_absolute",
                "certainty": "bounded",
                "precision": "month",
                "calendar_range": {
                    "lower": "2026-03-01",
                    "upper": "2026-04-01",
                    "bounds": "[)",
                },
            }
        )
        decision = normalize_relationship_observation(
            observation_value,
            entities,
            text,
            source_class="owner_assertion",
        )
        self.assertIsNotNone(decision.normalized_observation)
        normalized = decision.normalized_observation or {}
        self.assertEqual(normalized["polarity"], "affirmed")
        self.assertEqual(normalized["modality"], "reported_observation")
        self.assertEqual(normalized["temporal"]["shape"], "open_interval")
        self.assertIsNone(
            normalized["temporal"]["calendar_range"]["upper"]
        )

    def test_single_self_named_pair_repairs_model_self_loop(self) -> None:
        text = "Jessica used to be my wife."
        source = TrustedExtractionSource.create(
            job_id="00000000-0000-0000-0000-000000000001",
            source_system="public.chat_log",
            source_external_id="00000000-0000-0000-0000-000000000002",
            source_sha256=sha256_text(text),
            source_recorded_at="2026-07-20T05:00:00Z",
            content=text,
        )
        entities = [
            {
                "entity_ref": "e00",
                "entity_type": "self",
                "relationship_role": "user:self",
            },
            {
                "entity_ref": "e01",
                "entity_type": "person",
                "relationship_role": "family:wife",
            },
        ]
        value = observation(
            "relationship.spouse_of",
            text,
            subject_ref="e01",
            object_ref="e01",
        )
        registry = json.loads(
            (ROOT / "specs/memory_v1_predicate_registry_v5_1.json").read_text()
        )
        repairs = _relationship_v5_1_repair_entities_and_roles(
            source,
            value,
            entities,
            registry,
        )
        self.assertIn("relationship_self_loop_rewired", repairs)
        self.assertEqual(value["subject_entity_ref"], "e00")
        self.assertEqual(value["object"]["entity_ref"], "e01")

    def test_support_direction_is_derived_from_source_syntax(self) -> None:
        text = "Monika provides a lot of emotional support for me."
        entities = [
            {
                "entity_ref": "e00",
                "entity_type": "self",
                "relationship_role": "user:self",
            },
            {
                "entity_ref": "e01",
                "entity_type": "person",
                "relationship_role": "relationship:supported_person",
            },
        ]
        decision = normalize_relationship_observation(
            observation("social.supports", text),
            entities,
            text,
            source_class="owner_assertion",
        )
        self.assertIsNotNone(decision.normalized_observation)
        normalized = decision.normalized_observation or {}
        self.assertEqual(normalized["subject_entity_ref"], "e01")
        self.assertEqual(normalized["object"]["entity_ref"], "e00")

    def test_historical_relationship_is_bounded_by_source_time(self) -> None:
        text = "Jessica used to be my wife."
        entities = [
            {
                "entity_ref": "e00",
                "entity_type": "self",
                "relationship_role": "user:self",
            },
            {
                "entity_ref": "e01",
                "entity_type": "person",
                "relationship_role": "family:wife",
            },
        ]
        decision = normalize_relationship_observation(
            observation(
                "relationship.spouse_of",
                text,
                modality="reported_observation",
            ),
            entities,
            text,
            source_class="owner_assertion",
        )
        self.assertIsNotNone(decision.normalized_observation)
        value = (decision.normalized_observation or {})["temporal"]
        self.assertEqual(
            (decision.normalized_observation or {})["modality"],
            "asserted",
        )
        self.assertEqual(value["shape"], "bounded_interval")
        normalized = _normalize_temporal(value, "2026-07-20T05:00:00Z")
        self.assertTrue(normalized["anchored_to_source_time"])
        self.assertIsNone(normalized["instant_range"]["lower"])
        self.assertEqual(
            normalized["instant_range"]["upper"],
            "2026-07-20T05:00:00.000000Z",
        )


if __name__ == "__main__":
    unittest.main()
