#!/usr/bin/env python3
from __future__ import annotations

import copy
import unittest

from memory_v1_projection_v5_contract_test import validate_packet
from memory_v1_v5_claim_projection_preflight import (
    ClaimProjectionError,
    build_packet,
    build_projection,
    load_contract,
    render_canonical_text,
)


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"


def source(predicate: str) -> dict:
    base = {
        "observation_id": "43858045-c943-425c-a018-2fca175a722e",
        "observation_sha256": "8a6ebf42194c1cb1f73edb1db3e1339db5adcc8d455ff0f4c77c1ddabc29501d",
        "predicate_registry_version": "memory_predicate_registry_v5",
        "predicate": predicate,
        "polarity": "affirmed",
        "modality": "asserted",
        "projection_class": "direct_claim",
        "surface_policy": "direct_or_relevant",
        "evidence_status": "active",
        "subject_entity_id": "35029129-27bd-457b-8cb5-82dd37ba32ba",
        "subject_entity_type": "animal",
        "subject_entity_status": "active",
        "subject_canonical_name": "Koda",
        "object_kind": "literal",
        "object_entity_id": None,
        "object_entity_type": None,
        "object_entity_status": None,
        "object_canonical_name": None,
        "object_literal": {
            "kind": "literal",
            "datatype": "text",
            "value": "German Shepherd",
            "unit": None,
            "approximate": False,
        },
        "object_literal_sha256": "",
    }
    from memory_v1_projection_v5_contract_test import sha256

    if predicate == "pet.sex":
        base["object_literal"]["datatype"] = "enum"
        base["object_literal"]["value"] = "female"
    if predicate == "identity.name":
        base["object_literal"]["value"] = "Koda"
    base["object_literal_sha256"] = sha256(base["object_literal"])
    base["canonical_text"] = render_canonical_text(base)
    return base


class ClaimProjectionPreflightTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = load_contract()

    def test_literal_predicates_are_deterministic(self) -> None:
        expected = {
            "identity.name": "This animal's name is Koda.",
            "pet.breed": "Koda has recorded breed German Shepherd.",
            "pet.sex": "Koda has recorded sex female.",
        }
        for predicate, text in expected.items():
            with self.subTest(predicate=predicate):
                row = source(predicate)
                projection = build_projection(OWNER, row)
                packet = build_packet(projection)
                validate_packet(packet, OWNER, self.registry)
                self.assertEqual(projection["payload"]["canonical_text"], text)
                self.assertEqual(projection["identity"]["object_kind"], "literal")

    def test_entity_predicate_is_deterministic(self) -> None:
        row = source("pet.breed")
        row.update(
            {
                "predicate": "relationship.has_pet",
                "subject_entity_type": "self",
                "subject_canonical_name": "Avery",
                "object_kind": "entity",
                "object_entity_id": "a7f071ff-d519-4345-890f-96557c52b109",
                "object_entity_type": "animal",
                "object_entity_status": "active",
                "object_canonical_name": "Koda",
                "object_literal": None,
                "object_literal_sha256": None,
            }
        )
        row["canonical_text"] = render_canonical_text(row)
        projection = build_projection(OWNER, row)
        validate_packet(build_packet(projection), OWNER, self.registry)
        self.assertEqual(
            projection["payload"]["canonical_text"],
            "The user has a pet named Koda.",
        )

    def test_kinship_predicates_are_neutral_and_deterministic(self) -> None:
        cases = {
            "relationship.parent_of": (
                "person",
                "Dad",
                "self",
                "Self",
                "Dad is the user's parent.",
            ),
            "relationship.sibling_of": (
                "self",
                "Self",
                "person",
                "Avery",
                "The user and Avery are siblings.",
            ),
        }
        for predicate, values in cases.items():
            subject_type, subject_name, object_type, object_name, expected = values
            with self.subTest(predicate=predicate):
                row = source("pet.breed")
                row.update(
                    {
                        "predicate": predicate,
                        "subject_entity_type": subject_type,
                        "subject_canonical_name": subject_name,
                        "object_kind": "entity",
                        "object_entity_id": "a7f071ff-d519-4345-890f-96557c52b109",
                        "object_entity_type": object_type,
                        "object_entity_status": "active",
                        "object_canonical_name": object_name,
                        "object_literal": None,
                        "object_literal_sha256": None,
                    }
                )
                row["canonical_text"] = render_canonical_text(row)
                projection = build_projection(OWNER, row)
                validate_packet(build_packet(projection), OWNER, self.registry)
                self.assertEqual(projection["payload"]["canonical_text"], expected)

    def test_kinship_direction_and_entity_types_fail_closed(self) -> None:
        row = source("pet.breed")
        row.update(
            {
                "predicate": "relationship.parent_of",
                "subject_entity_type": "self",
                "subject_canonical_name": "Self",
                "object_kind": "entity",
                "object_entity_id": "a7f071ff-d519-4345-890f-96557c52b109",
                "object_entity_type": "person",
                "object_entity_status": "active",
                "object_canonical_name": "Dad",
                "object_literal": None,
                "object_literal_sha256": None,
            }
        )
        with self.assertRaises(ClaimProjectionError):
            render_canonical_text(row)

    def test_control_text_and_approximation_fail_closed(self) -> None:
        row = source("pet.breed")
        for value in (" bad", "bad\ntext"):
            forged = copy.deepcopy(row)
            forged["object_literal"]["value"] = value
            with self.assertRaises(ClaimProjectionError):
                render_canonical_text(forged)
        forged = copy.deepcopy(row)
        forged["object_literal"]["approximate"] = True
        with self.assertRaises(ClaimProjectionError):
            render_canonical_text(forged)


if __name__ == "__main__":
    unittest.main()
