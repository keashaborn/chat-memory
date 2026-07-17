#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.memory_v1_projection_v5_contract_test import (
    load_jsonl,
    owner_manifest_sha256,
    semantic_key_sha256,
    sha256,
    validate_cases,
    validate_packet,
    validate_projection_schema,
    validate_registry,
)


SCHEMA = ROOT / "specs" / "memory_v1_projection_plan_v5.schema.json"
REGISTRY = ROOT / "specs" / "memory_v1_predicate_registry_v5.json"
BASE_CASES = ROOT / "evals" / "memory_v1_relational_extraction_v5_cases.jsonl"
CASES = ROOT / "evals" / "memory_v1_projection_v5_cases.jsonl"
OWNER_A = "11111111-1111-4111-8111-111111111111"
OWNER_B = "22222222-2222-4222-8222-222222222222"
SUBJECT = "33333333-3333-4333-8333-333333333333"
OBJECT = "44444444-4444-4444-8444-444444444444"
OBSERVATION = "55555555-5555-4555-8555-555555555555"
TARGET = "66666666-6666-4666-8666-666666666666"
PROJECT = "77777777-7777-4777-8777-777777777777"
HASH_A = "a" * 64
HASH_B = "b" * 64


def claim_projection() -> dict:
    projection = {
        "projection_ref": "p01",
        "lane": "claim",
        "observation_inputs": [
            {
                "observation_id": OBSERVATION,
                "observation_sha256": HASH_A,
                "stance": "supports",
            }
        ],
        "identity": {
            "subject_entity_id": SUBJECT,
            "predicate": "identity.name",
            "object_kind": "literal",
            "object_entity_id": None,
            "object_literal_sha256": HASH_B,
            "polarity": "affirmed",
            "modality": "asserted",
            "semantic_key_sha256": "",
        },
        "target": {
            "action": "create",
            "aggregate_id": None,
            "expected_revision_number": None,
            "reason_codes": [],
        },
        "temporal_policy": {
            "canonical_source": "memory.observation_temporal",
            "materialization": "link_only",
            "source_observation_id": None,
        },
        "review": {
            "state": "auto_apply_eligible",
            "authorization_required": False,
            "reason_codes": [],
        },
        "relations": [],
        "payload": {
            "kind": "claim",
            "claim_class": "direct_claim",
            "canonical_text": "The animal's name is Koda.",
            "surface_policy": "direct_or_relevant",
        },
    }
    rehash_projection(projection)
    return projection


def response_preference_projection() -> dict:
    projection = claim_projection()
    projection["lane"] = "preference"
    projection["identity"]["predicate"] = "preference.response"
    projection["identity"]["modality"] = "endorsed"
    projection["payload"] = {
        "kind": "preference",
        "preference_class": "response",
        "domain": "response",
        "preference_key": "response_length",
        "value": "concise",
        "preference_polarity": "not_applicable",
        "scope": "user_global",
        "stability": "stable",
        "surface_policy": "zero_token_control_only",
    }
    rehash_projection(projection)
    return projection


def life_preference_projection() -> dict:
    projection = response_preference_projection()
    projection["identity"]["predicate"] = "preference.life"
    projection["identity"]["modality"] = "asserted"
    projection["payload"] = {
        "kind": "preference",
        "preference_class": "life",
        "domain": "music",
        "preference_key": "classical_music",
        "value": "classical music",
        "preference_polarity": "likes",
        "scope": "user_global",
        "stability": "stable",
        "surface_policy": "relevant_recommendation_or_explicit_recall",
    }
    rehash_projection(projection)
    return projection


def project_projection() -> dict:
    projection = claim_projection()
    projection["lane"] = "project_knowledge"
    projection["identity"]["predicate"] = "project.requirement"
    projection["identity"]["modality"] = "endorsed"
    projection["temporal_policy"] = {
        "canonical_source": "memory.observation_temporal",
        "materialization": "state_validity_only",
        "source_observation_id": OBSERVATION,
    }
    projection["payload"] = {
        "kind": "project_knowledge",
        "project_id": PROJECT,
        "component_key": "memory-v1",
        "binding_source": "trusted_component_registry",
        "knowledge_kind": "requirement",
        "knowledge_key": "memory.account_isolation",
        "canonical_text": "Memory retrieval must remain owner-scoped.",
        "document_state": "ratified",
        "authority_level": "user_ratified",
        "surface_policy": "exact_project_scope_only",
    }
    rehash_projection(projection)
    return projection


def correction_projection() -> dict:
    projection = claim_projection()
    projection["identity"]["predicate"] = "identity.name_canonical"
    projection["identity"]["modality"] = "corrective"
    projection["target"] = {
        "action": "revise",
        "aggregate_id": TARGET,
        "expected_revision_number": 2,
        "reason_codes": ["explicit_correction"],
    }
    projection["review"] = {
        "state": "manual_review_required",
        "authorization_required": True,
        "reason_codes": ["correction_target_resolution"],
    }
    projection["relations"] = [
        {
            "relation_type": "corrects",
            "target_lane": "claim",
            "target_aggregate_id": TARGET,
            "reason_code": "explicit_name_correction",
        }
    ]
    projection["payload"] = {
        "kind": "claim",
        "claim_class": "correction",
        "canonical_text": "The canonical name is Neko.",
        "surface_policy": "normalization_only",
    }
    rehash_projection(projection)
    return projection


def rehash_projection(projection: dict, actor: str = OWNER_A) -> None:
    projection["identity"]["semantic_key_sha256"] = semantic_key_sha256(
        actor, projection["lane"], projection["identity"], projection["payload"]
    )


def packet(projection: dict) -> dict:
    value = {
        "contract_version": "memory_v1_projection_plan_v5",
        "predicate_registry_version": "memory_predicate_registry_v5",
        "projection_policy_version": "memory_projection_policy_v5",
        "projector": "deterministic_test",
        "projector_version": "v5",
        "projections": [projection],
        "packet_sha256": "",
    }
    value["packet_sha256"] = sha256(
        {key: item for key, item in value.items() if key != "packet_sha256"}
    )
    return value


class ProjectionV5Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        cls.registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
        cls.registry_by_name = validate_registry(copy.deepcopy(cls.registry))
        cls.base_cases = load_jsonl(BASE_CASES)
        cls.cases = load_jsonl(CASES)

    def test_checked_in_contract_is_valid(self) -> None:
        validate_projection_schema(copy.deepcopy(self.schema))
        validate_cases(copy.deepcopy(self.cases), copy.deepcopy(self.base_cases))

    def test_claim_packet_is_valid(self) -> None:
        validate_packet(packet(claim_projection()), OWNER_A, self.registry_by_name)

    def test_response_preference_is_zero_token_control(self) -> None:
        validate_packet(
            packet(response_preference_projection()), OWNER_A, self.registry_by_name
        )

    def test_life_preference_is_not_response_control(self) -> None:
        validate_packet(packet(life_preference_projection()), OWNER_A, self.registry_by_name)

    def test_project_requires_trusted_project_id_and_scope(self) -> None:
        validate_packet(packet(project_projection()), OWNER_A, self.registry_by_name)

    def test_root_project_scope_is_distinct_and_valid(self) -> None:
        projection = project_projection()
        projection["payload"]["component_key"] = None
        projection["payload"]["binding_source"] = "trusted_thread_binding"
        rehash_projection(projection)
        validate_packet(packet(projection), OWNER_A, self.registry_by_name)

    def test_component_without_registry_binding_fails_closed(self) -> None:
        projection = project_projection()
        projection["payload"]["binding_source"] = "explicit_source_text"
        rehash_projection(projection)
        with self.assertRaisesRegex(AssertionError, "registry-bound"):
            validate_packet(packet(projection), OWNER_A, self.registry_by_name)

    def test_correction_requires_reviewed_relation(self) -> None:
        validate_packet(packet(correction_projection()), OWNER_A, self.registry_by_name)

    def test_owner_property_in_schema_fails_closed(self) -> None:
        schema = copy.deepcopy(self.schema)
        schema["properties"]["owner_user_id"] = {"type": "string"}
        with self.assertRaisesRegex(AssertionError, "forbidden projection"):
            validate_projection_schema(schema)

    def test_scalar_salience_in_schema_fails_closed(self) -> None:
        schema = copy.deepcopy(self.schema)
        schema["$defs"]["claim_payload"]["properties"]["salience"] = {
            "type": "number"
        }
        with self.assertRaisesRegex(AssertionError, "forbidden projection"):
            validate_projection_schema(schema)

    def test_owner_changes_semantic_identity_and_manifest(self) -> None:
        projection = claim_projection()
        key_a = semantic_key_sha256(
            OWNER_A, projection["lane"], projection["identity"], projection["payload"]
        )
        key_b = semantic_key_sha256(
            OWNER_B, projection["lane"], projection["identity"], projection["payload"]
        )
        self.assertNotEqual(key_a, key_b)
        packet_hash = packet(projection)["packet_sha256"]
        self.assertNotEqual(
            owner_manifest_sha256(OWNER_A, packet_hash),
            owner_manifest_sha256(OWNER_B, packet_hash),
        )

    def test_other_owner_cannot_reuse_semantic_key(self) -> None:
        value = packet(claim_projection())
        with self.assertRaisesRegex(AssertionError, "semantic key mismatch"):
            validate_packet(value, OWNER_B, self.registry_by_name)

    def test_packet_hash_mismatch_fails_closed(self) -> None:
        value = packet(claim_projection())
        value["projector_version"] = "tampered"
        with self.assertRaisesRegex(AssertionError, "packet SHA-256 mismatch"):
            validate_packet(value, OWNER_A, self.registry_by_name)

    def test_response_preference_content_surface_fails_closed(self) -> None:
        projection = response_preference_projection()
        projection["payload"]["surface_policy"] = "relevant_recommendation_or_explicit_recall"
        rehash_projection(projection)
        with self.assertRaisesRegex(AssertionError, "surface policy is not registered"):
            validate_packet(packet(projection), OWNER_A, self.registry_by_name)

    def test_life_preference_zero_token_surface_fails_closed(self) -> None:
        projection = life_preference_projection()
        projection["payload"]["surface_policy"] = "zero_token_control_only"
        rehash_projection(projection)
        with self.assertRaisesRegex(AssertionError, "surface policy is not registered"):
            validate_packet(packet(projection), OWNER_A, self.registry_by_name)

    def test_project_kind_mismatch_fails_closed(self) -> None:
        projection = project_projection()
        projection["payload"]["knowledge_kind"] = "constraint"
        rehash_projection(projection)
        with self.assertRaisesRegex(AssertionError, "project knowledge kind mismatch"):
            validate_packet(packet(projection), OWNER_A, self.registry_by_name)

    def test_unregistered_modality_fails_closed(self) -> None:
        projection = claim_projection()
        projection["identity"]["modality"] = "planned"
        rehash_projection(projection)
        with self.assertRaisesRegex(AssertionError, "modality is not registered"):
            validate_packet(packet(projection), OWNER_A, self.registry_by_name)

    def test_predicate_object_contract_fails_closed(self) -> None:
        projection = claim_projection()
        projection["identity"]["object_kind"] = "entity"
        projection["identity"]["object_entity_id"] = OBJECT
        projection["identity"]["object_literal_sha256"] = None
        rehash_projection(projection)
        with self.assertRaisesRegex(AssertionError, "object kind does not match"):
            validate_packet(packet(projection), OWNER_A, self.registry_by_name)

    def test_extra_lane_payload_field_fails_closed(self) -> None:
        projection = claim_projection()
        projection["payload"]["confidence"] = 0.99
        rehash_projection(projection)
        with self.assertRaisesRegex(AssertionError, "claim payload fields mismatch"):
            validate_packet(packet(projection), OWNER_A, self.registry_by_name)

    def test_claim_cannot_materialize_lossy_interval(self) -> None:
        projection = claim_projection()
        projection["temporal_policy"] = {
            "canonical_source": "memory.observation_temporal",
            "materialization": "state_validity_only",
            "source_observation_id": OBSERVATION,
        }
        with self.assertRaisesRegex(AssertionError, "unsafe temporal interval"):
            validate_packet(packet(projection), OWNER_A, self.registry_by_name)

    def test_temporal_source_must_be_linked_observation(self) -> None:
        projection = project_projection()
        projection["temporal_policy"]["source_observation_id"] = OBJECT
        with self.assertRaisesRegex(AssertionError, "not a projection input"):
            validate_packet(packet(projection), OWNER_A, self.registry_by_name)

    def test_existing_target_requires_revision_lock(self) -> None:
        projection = claim_projection()
        projection["target"] = {
            "action": "revise",
            "aggregate_id": TARGET,
            "expected_revision_number": None,
            "reason_codes": [],
        }
        with self.assertRaisesRegex(AssertionError, "requires ID and revision"):
            validate_packet(packet(projection), OWNER_A, self.registry_by_name)

    def test_correction_without_relation_fails_closed(self) -> None:
        projection = correction_projection()
        projection["relations"] = []
        with self.assertRaisesRegex(AssertionError, "correction lacks reviewed"):
            validate_packet(packet(projection), OWNER_A, self.registry_by_name)

    def test_relation_cannot_cross_lanes(self) -> None:
        projection = correction_projection()
        projection["relations"][0]["target_lane"] = "project_knowledge"
        with self.assertRaisesRegex(AssertionError, "cross-lane relation"):
            validate_packet(packet(projection), OWNER_A, self.registry_by_name)

    def test_modality_is_part_of_semantic_identity(self) -> None:
        projection = claim_projection()
        asserted = projection["identity"]["semantic_key_sha256"]
        projection["identity"]["modality"] = "uncertain"
        uncertain = semantic_key_sha256(
            OWNER_A, projection["lane"], projection["identity"], projection["payload"]
        )
        self.assertNotEqual(asserted, uncertain)

    def test_time_is_provenance_not_semantic_identity(self) -> None:
        projection = claim_projection()
        first = projection["identity"]["semantic_key_sha256"]
        projection["temporal_policy"]["source_observation_id"] = OBSERVATION
        second = semantic_key_sha256(
            OWNER_A, projection["lane"], projection["identity"], projection["payload"]
        )
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
