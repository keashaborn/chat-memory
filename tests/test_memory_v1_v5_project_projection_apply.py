from __future__ import annotations

import copy
import unittest

from scripts.memory_v1_projection_v5_contract_test import (
    owner_manifest_sha256,
    sha256,
    stable_json,
)
from scripts.memory_v1_v5_project_projection_apply import (
    BUNDLE_CONTRACT,
    ProjectProjectionApplyError,
    validate_bundle,
)
from scripts.memory_v1_v5_project_projection_preflight import (
    build_packet,
    build_projection,
    object_literal_sha256,
)


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
        "polarity": "affirmed",
        "modality": "asserted",
        "object_literal": literal,
        "object_literal_sha256": object_literal_sha256(literal),
        "subject_entity_id": "9378f68c-2087-4365-8a9a-a166faf1324e",
        "project_id": "08cd6a8a-5599-43d5-8d5c-b59401df8ccc",
        "component_key": "memory-v1",
        "binding_source": "trusted_component_registry",
    }


def bundle() -> dict:
    packet = build_packet(
        build_projection(OWNER, source(), "architecture.memory_service")
    )
    value = {
        "contract_version": BUNDLE_CONTRACT,
        "owner_user_id": OWNER,
        "plan_id": "b8e6f378-0f69-56b8-a032-e01af4411f8e",
        "packet": packet,
        "packet_text": stable_json(packet),
        "packet_sha256": packet["packet_sha256"],
        "owner_manifest_sha256": owner_manifest_sha256(
            OWNER, packet["packet_sha256"]
        ),
        "source_snapshot": {
            "observation_id": source()["observation_id"],
        },
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
    }
    value["bundle_sha256"] = sha256(value)
    return value


class ProjectProjectionApplyTest(unittest.TestCase):
    def test_accepts_exact_project_bundle(self) -> None:
        validate_bundle(bundle())

    def test_rejects_bundle_hash_tamper(self) -> None:
        value = bundle()
        value["plan_id"] = "c8e6f378-0f69-56b8-a032-e01af4411f8e"
        with self.assertRaises(ProjectProjectionApplyError):
            validate_bundle(value)

    def test_rejects_untrusted_component_binding(self) -> None:
        value = bundle()
        value["packet"]["projections"][0]["payload"][
            "binding_source"
        ] = "trusted_thread_binding"
        value["packet_text"] = stable_json(value["packet"])
        value["bundle_sha256"] = sha256(
            {key: item for key, item in value.items() if key != "bundle_sha256"}
        )
        with self.assertRaises(Exception):
            validate_bundle(value)

    def test_validation_does_not_mutate_bundle(self) -> None:
        value = bundle()
        original = copy.deepcopy(value)
        validate_bundle(value)
        self.assertEqual(value, original)


if __name__ == "__main__":
    unittest.main()
