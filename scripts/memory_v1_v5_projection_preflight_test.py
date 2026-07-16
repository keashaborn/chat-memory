#!/usr/bin/env python3
from __future__ import annotations

import unittest

from memory_v1_projection_v5_contract_test import (
    validate_packet,
    validate_registry,
)
from memory_v1_v5_projection_preflight import build_packet, build_projection


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"


def source() -> dict:
    return {
        "observation_id": "9bf1e6b2-1840-4524-98dc-142567ebe013",
        "observation_sha256": "8a6ebf42194c1cb1f73edb1db3e1339db5adcc8d455ff0f4c77c1ddabc29501d",
        "predicate": "occupation.works_as",
        "polarity": "affirmed",
        "modality": "asserted",
        "projection_class": "direct_claim",
        "surface_policy": "direct_or_relevant",
        "evidence_status": "active",
        "subject_entity_id": "35029129-27bd-457b-8cb5-82dd37ba32ba",
        "subject_entity_type": "self",
        "subject_entity_status": "active",
        "object_entity_id": "a7f071ff-d519-4345-890f-96557c52b109",
        "object_entity_type": "concept",
        "object_entity_status": "active",
        "object_canonical_name": "personal trainer",
        "temporal_semantic": "state_validity",
        "temporal_shape": "open_interval",
        "temporal_basis": "instant",
    }


class ProjectionPreflightTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import json
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        registry = json.loads(
            (root / "specs/memory_v1_predicate_registry_v5.json").read_text()
        )
        cls.registry = validate_registry(registry)

    def test_occupation_projection_is_hash_locked_and_manual(self) -> None:
        projection = build_projection(OWNER, source())
        packet = build_packet(projection)
        validate_packet(packet, OWNER, self.registry)
        self.assertEqual(projection["lane"], "claim")
        self.assertEqual(projection["target"]["action"], "create")
        self.assertEqual(
            projection["review"]["state"], "manual_review_required"
        )
        self.assertEqual(
            projection["payload"]["canonical_text"],
            "The user works as a personal trainer.",
        )

    def test_unsupported_predicate_fails_closed(self) -> None:
        value = source()
        value["predicate"] = "identity.name"
        with self.assertRaisesRegex(RuntimeError, "unsupported"):
            build_projection(OWNER, value)


if __name__ == "__main__":
    unittest.main()
