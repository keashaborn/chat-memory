from __future__ import annotations

import copy
import unittest

from scripts.memory_v1_v5_project_projection_preflight import (
    ProjectProjectionPreflightError,
    build_packet,
    build_projection,
    load_contract,
    object_literal_sha256,
    validate_source,
)
from scripts.memory_v1_projection_v5_contract_test import validate_packet


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"


def source() -> dict:
    literal = {
        "kind": "literal",
        "datatype": "text",
        "value": "being built as a backend-governed memory service",
        "unit": None,
        "approximate": False,
    }
    return {
        "observation_id": "258d8d96-2cbd-4296-b878-769c90533fae",
        "observation_sha256": "a" * 64,
        "predicate": "project.current_state",
        "predicate_registry_version": "memory_predicate_registry_v5",
        "polarity": "affirmed",
        "modality": "asserted",
        "projection_class": "project_knowledge",
        "surface_policy": "exact_project_scope_only",
        "object_literal": literal,
        "object_literal_sha256": object_literal_sha256(literal),
        "subject_entity_id": "9378f68c-2087-4365-8a9a-a166faf1324e",
        "subject_entity_type": "project",
        "subject_entity_status": "active",
        "evidence_status": "active",
        "evidence_content_sha256": "b" * 64,
        "evidence_observed_at": "2026-07-17T10:09:51Z",
        "temporal_semantic": "observation_time",
        "temporal_shape": "instant",
        "temporal_basis": "instant",
        "temporal_source_form": "implicit_source_time",
        "temporal_certainty": "exact",
        "temporal_precision": "exact",
        "temporal_normalized_sha256": "c" * 64,
        "project_id": "08cd6a8a-5599-43d5-8d5c-b59401df8ccc",
        "project_key": "verbal-sage",
        "component_key": "memory-v1",
        "binding_source": "trusted_component_registry",
    }


class ProjectProjectionPreflightTest(unittest.TestCase):
    def test_builds_schema_valid_exact_scope_projection(self) -> None:
        value = source()
        validate_source(value)
        projection = build_projection(
            OWNER, value, "architecture.memory_service"
        )
        packet = build_packet(projection)
        validate_packet(packet, OWNER, load_contract())
        self.assertEqual(projection["lane"], "project_knowledge")
        self.assertEqual(
            projection["payload"]["canonical_text"],
            value["object_literal"]["value"],
        )
        self.assertEqual(
            projection["review"]["state"], "manual_review_required"
        )

    def test_rejects_untrusted_component_scope(self) -> None:
        value = source()
        value["binding_source"] = "trusted_thread_binding"
        with self.assertRaises(ProjectProjectionPreflightError):
            validate_source(value)

    def test_rejects_invalid_knowledge_key(self) -> None:
        with self.assertRaises(ProjectProjectionPreflightError):
            build_projection(OWNER, source(), "Memory Service")

    def test_does_not_mutate_source(self) -> None:
        value = source()
        original = copy.deepcopy(value)
        build_projection(OWNER, value, "architecture.memory_service")
        self.assertEqual(value, original)


if __name__ == "__main__":
    unittest.main()
