from __future__ import annotations

import copy
import unittest
import uuid

from memory_v1_life_event_claim_projection_v5_1 import (
    LifeEventProjectionError,
    build_packet,
    build_projection,
    render_canonical_text,
    validate_packet,
)
from memory_v1_projection_v5_contract_test import sha256


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"


def source(name: str = "Neko", subject_type: str = "animal") -> dict:
    literal = {
        "kind": "literal",
        "datatype": "boolean",
        "value": True,
        "unit": None,
        "approximate": False,
    }
    value = {
        "observation_id": uuid.uuid4(),
        "observation_sha256": "a" * 64,
        "predicate": "life_event.died",
        "predicate_registry_version": "memory_predicate_registry_v5",
        "polarity": "affirmed",
        "modality": "reported_observation",
        "projection_class": "direct_claim",
        "surface_policy": "direct_or_relevant",
        "object_kind": "literal",
        "object_literal": literal,
        "object_literal_sha256": sha256(literal),
        "subject_entity_id": uuid.uuid4(),
        "subject_entity_type": subject_type,
        "subject_entity_status": "active",
        "subject_canonical_name": name,
        "object_entity_id": None,
        "evidence_status": "active",
        "temporal_semantic": "occurrence",
    }
    value["canonical_text"] = render_canonical_text(value)
    return value


class LifeEventProjectionTest(unittest.TestCase):
    def test_named_animal_renderer(self) -> None:
        self.assertEqual(render_canonical_text(source()), "Neko died.")

    def test_parent_role_renderer(self) -> None:
        self.assertEqual(
            render_canonical_text(source("mother", "person")),
            "The user's mother died.",
        )

    def test_packet_is_owner_bound_and_manual_review_only(self) -> None:
        projection = build_projection(OWNER, source())
        packet = build_packet(projection, "memory_predicate_registry_v5")
        validate_packet(packet, OWNER)
        self.assertTrue(projection["review"]["authorization_required"])
        self.assertEqual(projection["review"]["state"], "manual_review_required")

    def test_false_literal_fails_closed(self) -> None:
        value = source()
        value["object_literal"]["value"] = False
        with self.assertRaises(LifeEventProjectionError):
            build_projection(OWNER, value)

    def test_weakened_review_fails_closed(self) -> None:
        projection = build_projection(OWNER, source())
        packet = build_packet(projection, "memory_predicate_registry_v5")
        forged = copy.deepcopy(packet)
        forged["projections"][0]["review"]["authorization_required"] = False
        with self.assertRaises(LifeEventProjectionError):
            validate_packet(forged, OWNER)


if __name__ == "__main__":
    unittest.main()
