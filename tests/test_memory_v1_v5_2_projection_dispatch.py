#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import unittest
import uuid

from memory_v1_projection_v5_2_contract import (
    sha256,
    validate_packet,
    validate_projection_schema,
    validate_registry,
)
from memory_v1_v5_2_projection_dispatch import (
    ProjectionDispatchError,
    build_packet,
    build_reconciled_stance_packet,
)


ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "specs").is_dir():
    ROOT = Path(__file__).resolve().parent
OWNER = "11111111-1111-4111-8111-111111111111"
SUBJECT = "aaaaaaaa-1111-4111-8111-111111111111"
OBJECT = "bbbbbbbb-2222-4222-8222-222222222222"
OBSERVATION = "cccccccc-3333-4333-8333-333333333333"
PROJECT = "dddddddd-4444-4444-8444-444444444444"


def literal(contract: str) -> dict:
    values = {
        "literal.age_number": ("number", 42, "year", False),
        "literal.health_text": ("text", "recurring knee discomfort", None, False),
        "literal.name_text": ("text", "Avery", None, False),
        "literal.pet_hearing_status": ("enum", "deaf", None, False),
        "literal.pet_sex": ("enum", "female", None, False),
        "literal.pet_text": ("text", "German shepherd", None, False),
        "literal.pet_weight": ("number", 72, "lb", True),
        "literal.preference_life": (
            "json",
            {"domain": "music", "target": "classical music", "polarity": "likes", "context": None},
            None,
            False,
        ),
        "literal.preference_response": (
            "json",
            {"dimension": "specificity", "value": "specific and direct"},
            None,
            False,
        ),
        "literal.project_text": ("text", "Memory retrieval must remain owner scoped", None, False),
        "literal.report_text": ("text", "clinical psychologist", None, False),
        "literal.reported_stance": (
            "json",
            {
                "topic_key": "evidence.truth",
                "topic_text": "evidence and truth",
                "position": "claims should track evidence rather than absolute truth",
                "orientation": "supports",
                "context": "memory architecture",
            },
            None,
            False,
        ),
        "literal.true": ("boolean", True, None, False),
    }
    datatype, value, unit, approximate = values[contract]
    return {
        "kind": "literal",
        "datatype": datatype,
        "value": value,
        "unit": unit,
        "approximate": approximate,
    }


def entity_type(contract: str) -> str:
    return {
        "entity.animal": "animal",
        "entity.concept": "concept",
        "entity.organization": "organization",
        "entity.person": "person",
        "entity.person_or_self": "person",
        "entity.place": "place",
    }[contract]


def source(entry: dict) -> dict:
    subject_types = entry["subject_entity_types"]
    subject_type = "self" if "self" in subject_types else subject_types[0]
    contract = entry["object_contract"]
    if contract.startswith("entity."):
        object_kind = "entity"
        object_literal = None
        object_entity_type = entity_type(contract)
        object_entity_id = OBJECT
        object_name = {
            "animal": "Koda",
            "concept": "clinical psychologist",
            "organization": "Caravel Autism Health",
            "person": "Kelly",
            "place": "Green Bay",
        }[object_entity_type]
        literal_sha = None
    else:
        object_kind = "literal"
        object_literal = literal(contract)
        object_entity_type = None
        object_entity_id = None
        object_name = None
        literal_sha = sha256(object_literal)
    predicate = entry["predicate"]
    project = predicate.startswith("project.")
    projection_class = (
        "direct_claim"
        if "direct_claim" in entry["projection_classes"]
        else entry["projection_classes"][0]
    )
    return {
        "owner_user_id": OWNER,
        "observation_id": OBSERVATION,
        "observation_sha256": "1" * 64,
        "evidence_id": "eeeeeeee-5555-4555-8555-555555555555",
        "evidence_content_sha256": "2" * 64,
        "evidence_status": "active",
        "predicate_registry_version": "memory_predicate_registry_v5_2",
        "predicate": predicate,
        "polarity": "affirmed",
        "modality": entry["modalities"][0],
        "projection_class": projection_class,
        "surface_policy": entry["surface_policies"][0],
        "subject_entity_id": SUBJECT,
        "subject_entity_type": subject_type,
        "subject_entity_status": "active",
        "subject_canonical_name": "Memory V1" if subject_type == "project" else "Eric",
        "object_kind": object_kind,
        "object_entity_id": object_entity_id,
        "object_entity_type": object_entity_type,
        "object_entity_status": "active" if object_kind == "entity" else None,
        "object_canonical_name": object_name,
        "object_literal": object_literal,
        "object_literal_sha256": literal_sha,
        "project_scope": (
            {
                "state": "resolved",
                "project_id": PROJECT,
                "project_key": "verbal-sage",
                "component_key": "memory-v1",
                "binding_source": "trusted_component_registry",
            }
            if project
            else {"state": "none"}
        ),
        "temporal": {
            "semantic": "state_validity" if predicate == "project.current_state" else "none",
            "basis": "calendar" if predicate == "project.current_state" else "none",
        },
    }


class ProjectionDispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        specs = ROOT / "specs" if (ROOT / "specs").is_dir() else ROOT
        cls.schema = json.loads((specs / "memory_v1_projection_plan_v5_2.schema.json").read_text())
        cls.registry = json.loads((specs / "memory_v1_predicate_registry_v5_2.json").read_text())
        validate_projection_schema(cls.schema)
        cls.registry_by_name = validate_registry(cls.registry)

    def test_every_v5_2_predicate_builds_a_valid_governed_projection(self) -> None:
        lanes: set[str] = set()
        classes: set[str] = set()
        for entry in self.registry["predicates"]:
            with self.subTest(predicate=entry["predicate"]):
                packet = build_packet(OWNER, source(entry))
                validate_packet(packet, OWNER, self.registry_by_name)
                projection = packet["projections"][0]
                lanes.add(projection["lane"])
                classes.add(projection["payload"].get("claim_class", projection["payload"].get("preference_class", projection["lane"])))
        self.assertEqual(lanes, {"claim", "preference", "project_knowledge"})
        self.assertIn("reported_stance", classes)

    def test_stance_is_attributed_and_remains_in_claim_lane(self) -> None:
        entry = self.registry_by_name["stance.reported"]
        projection = build_packet(OWNER, source(entry))["projections"][0]
        self.assertEqual(projection["lane"], "claim")
        self.assertEqual(projection["payload"]["claim_class"], "reported_stance")
        self.assertEqual(
            projection["payload"]["canonical_text"],
            'The user reports this position: "claims should track evidence rather than absolute truth."',
        )
        self.assertEqual(projection["identity"]["modality"], "reported_belief")
        packet = build_packet(OWNER, source(entry))
        self.assertEqual(packet["projector_version"], "semantic_dispatch_v2")


    def test_reconciled_stance_uses_one_primary_and_one_context_source(self) -> None:
        entry = self.registry_by_name["stance.reported"]
        primary = source(entry)
        primary["observation_id"] = "cccccccc-3333-4333-8333-333333333333"
        primary["observation_sha256"] = "1" * 64
        context = source(entry)
        context["observation_id"] = "cccccccc-3333-4333-8333-333333333334"
        context["observation_sha256"] = "3" * 64
        context["object_literal"]["value"]["position"] = (
            "I do not believe the future can be predicted"
        )
        context["object_literal_sha256"] = sha256(context["object_literal"])

        packet = build_reconciled_stance_packet(OWNER, primary, context)
        validate_packet(packet, OWNER, self.registry_by_name)
        projection = packet["projections"][0]
        self.assertEqual(packet["projector_version"], "stance_reconciliation_v1")
        self.assertEqual(
            [item["stance"] for item in projection["observation_inputs"]],
            ["supports", "context"],
        )
        self.assertEqual(
            projection["identity"]["object_literal_sha256"],
            primary["object_literal_sha256"],
        )
        self.assertEqual(
            projection["review"]["reason_codes"],
            ["initial_v5_2_reconciled_stance_requires_review"],
        )

    def test_reconciled_stance_rejects_cross_evidence_context(self) -> None:
        entry = self.registry_by_name["stance.reported"]
        primary = source(entry)
        context = source(entry)
        context["observation_id"] = "cccccccc-3333-4333-8333-333333333334"
        context["evidence_id"] = "eeeeeeee-5555-4555-8555-555555555556"
        with self.assertRaisesRegex(
            ProjectionDispatchError, "sources are incompatible"
        ):
            build_reconciled_stance_packet(OWNER, primary, context)

    def test_response_preference_never_becomes_content(self) -> None:
        entry = self.registry_by_name["preference.response"]
        projection = build_packet(OWNER, source(entry))["projections"][0]
        self.assertEqual(projection["lane"], "preference")
        self.assertEqual(projection["payload"]["preference_class"], "response")
        self.assertEqual(projection["payload"]["surface_policy"], "zero_token_control_only")
        self.assertEqual(projection["payload"]["preference_polarity"], "not_applicable")

    def test_project_state_uses_exact_scope_and_temporal_authority(self) -> None:
        entry = self.registry_by_name["project.current_state"]
        projection = build_packet(OWNER, source(entry))["projections"][0]
        self.assertEqual(projection["payload"]["component_key"], "memory-v1")
        self.assertEqual(projection["temporal_policy"]["materialization"], "state_validity_only")
        self.assertEqual(projection["temporal_policy"]["source_observation_id"], OBSERVATION)

    def test_database_json_text_is_normalized_at_dispatch_boundary(self) -> None:
        entry = self.registry_by_name["project.current_state"]
        value = source(entry)
        for field in ("object_literal", "project_scope", "temporal"):
            value[field] = json.dumps(value[field], sort_keys=True, separators=(",", ":"))
        projection = build_packet(OWNER, value)["projections"][0]
        self.assertEqual(projection["lane"], "project_knowledge")
        self.assertEqual(projection["payload"]["component_key"], "memory-v1")

    def test_malformed_database_json_text_is_rejected(self) -> None:
        entry = self.registry_by_name["identity.name"]
        value = source(entry)
        value["object_literal"] = "{"
        with self.assertRaisesRegex(ProjectionDispatchError, "not valid JSON"):
            build_packet(OWNER, value)

    def test_cross_owner_source_is_rejected(self) -> None:
        entry = self.registry_by_name["relationship.parent_of"]
        value = source(entry)
        value["owner_user_id"] = str(uuid.uuid4())
        with self.assertRaisesRegex(ProjectionDispatchError, "owner mismatch"):
            build_packet(OWNER, value)

    def test_literal_hash_drift_is_rejected(self) -> None:
        entry = self.registry_by_name["stance.reported"]
        value = source(entry)
        value["object_literal_sha256"] = "0" * 64
        with self.assertRaisesRegex(ProjectionDispatchError, "literal hash mismatch"):
            build_packet(OWNER, value)


if __name__ == "__main__":
    unittest.main()
