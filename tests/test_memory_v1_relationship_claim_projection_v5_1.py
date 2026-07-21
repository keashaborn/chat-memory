from __future__ import annotations

import copy
import unittest
import uuid

from memory_v1_relationship_claim_projection_v5_1 import (
    RelationshipProjectionError,
    build_packet,
    build_projection,
    load_registry,
    render_canonical_text,
    validate_packet,
)


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"


def source(predicate: str, subject_type: str, object_type: str) -> dict:
    registry = load_registry()[predicate]
    modality = "reported_observation" if predicate.startswith("social.") else "asserted"
    if modality not in registry["modalities"]:
        modality = "asserted"
    value = {
        "observation_id": uuid.uuid4(),
        "observation_sha256": "a" * 64,
        "predicate": predicate,
        "predicate_registry_version": "memory_predicate_registry_v5_1",
        "polarity": "affirmed",
        "modality": modality,
        "projection_class": "direct_claim",
        "surface_policy": registry["surface_policies"][0],
        "evidence_status": "active",
        "subject_entity_id": uuid.uuid4(),
        "subject_entity_type": subject_type,
        "subject_entity_status": "active",
        "subject_canonical_name": "Self" if subject_type == "self" else "Alex",
        "object_entity_id": uuid.uuid4(),
        "object_entity_type": object_type,
        "object_entity_status": "active",
        "object_canonical_name": (
            "Self" if object_type == "self" else "Koda" if object_type == "animal" else "Jordan"
        ),
    }
    value["canonical_text"] = render_canonical_text(value)
    return value


class RelationshipProjectionTest(unittest.TestCase):
    def test_every_registered_relationship_has_renderer(self) -> None:
        registry = load_registry()
        self.assertGreaterEqual(len(registry), 40)
        for predicate, entry in registry.items():
            policy = entry["relationship_policy"]
            subject_type = "self" if "self" in policy["subject_entity_types"] else policy["subject_entity_types"][0]
            object_type = policy["object_entity_types"][0]
            rendered = render_canonical_text(source(predicate, subject_type, object_type))
            self.assertTrue(rendered.endswith("."), predicate)

    def test_parent_of_self_is_precise(self) -> None:
        value = source("relationship.parent_of", "person", "self")
        self.assertEqual(value["canonical_text"], "Alex is the user's parent.")

    def test_packet_is_owner_bound_and_manual_review_only(self) -> None:
        projection = build_projection(
            OWNER, source("relationship.parent_of", "person", "self")
        )
        packet = build_packet(projection)
        validate_packet(packet, OWNER)
        self.assertTrue(projection["review"]["authorization_required"])
        self.assertEqual(projection["review"]["state"], "manual_review_required")

    def test_weakened_review_fails_closed(self) -> None:
        projection = build_projection(
            OWNER, source("relationship.friend_of", "self", "person")
        )
        packet = build_packet(projection)
        packet["projections"][0]["review"]["authorization_required"] = False
        packet["packet_sha256"] = "0" * 64
        with self.assertRaises(RelationshipProjectionError):
            validate_packet(packet, OWNER)

    def test_extra_packet_field_fails_closed(self) -> None:
        projection = build_projection(
            OWNER, source("social.experiences_tension_with", "self", "person")
        )
        packet = copy.deepcopy(build_packet(projection))
        packet["unexpected"] = True
        with self.assertRaises(RelationshipProjectionError):
            validate_packet(packet, OWNER)


if __name__ == "__main__":
    unittest.main()
