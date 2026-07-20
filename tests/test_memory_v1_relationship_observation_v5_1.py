from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.memory_v1_relationship_observation_v5_1 import (
    POLICY_VERSION,
    map_entity_relationship_role,
    normalize_relationship_observation,
    predicate_supported_by_source,
    relationship_predicate_candidates_from_source,
    relationship_role_candidates,
)
from scripts.memory_v1_relationship_policy_v5_1 import load_registry


ROOT = Path(__file__).resolve().parents[1]


def positive_cases() -> list[dict]:
    rows = []
    for line in (ROOT / "evals/memory_v1_relationship_v5_1_cases.jsonl").read_text().splitlines():
        row = json.loads(line)
        if row["case_id"] <= "rel-v5_1-041":
            rows.append(row)
    return rows


def role_for(contract: dict, direction: str, text: str) -> str:
    field = {
        "named_to_self": "named_party_subject_roles",
        "self_to_named": "named_party_object_roles",
        "unordered_self_named": "named_party_symmetric_roles",
    }[direction]
    roles = contract[field]
    return next(
        (
            role
            for role in roles
            if predicate_supported_by_source(
                contract["predicate"], role, text
            )
        ),
        roles[0],
    )


def packet_parts(
    text: str,
    predicate: str,
    direction: str,
    role: str,
    *,
    named_type: str = "person",
) -> tuple[list[dict], dict]:
    entities = [
        {
            "entity_ref": "e00",
            "entity_type": "self",
            "relationship_role": "user:self",
        },
        {
            "entity_ref": "e01",
            "entity_type": named_type,
            "relationship_role": f"family:{role}",
        },
    ]
    if direction == "named_to_self":
        subject, obj = "e01", "e00"
    else:
        subject, obj = "e00", "e01"
    observation = {
        "observation_ref": "o00",
        "subject_entity_ref": subject,
        "predicate": predicate,
        "object": {"kind": "entity", "entity_ref": obj},
        "polarity": "affirmed",
        "modality": "asserted",
        "projection_class": "direct_claim",
        "surface_policy": "direct_or_relevant",
        "sensitivity": "low",
        "source_spans": [{"start": 0, "end": len(text), "quote": text}],
        "reason_codes": ["synthetic_relationship_case"],
        "temporal": {"semantic": "observation_time"},
    }
    return entities, observation


class RelationshipObservationV51Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = load_registry()
        cls.contracts = {
            row["predicate"]: row for row in cls.registry["predicates"]
        }

    def test_policy_version_is_explicit(self) -> None:
        self.assertEqual(POLICY_VERSION, "memory_v1_relationship_observation_v5_1")

    def test_all_41_positive_predicates_are_source_entailed_and_normalized(self) -> None:
        seen = set()
        for case in positive_cases():
            expected = case["expected"]
            predicate = expected["predicate"]
            direction = expected["edge_direction"]
            contract = self.contracts[predicate]
            role = role_for(contract, direction, case["text"])
            named_type = "animal" if predicate == "relationship.has_pet" else "person"
            entities, observation = packet_parts(
                case["text"], predicate, direction, role, named_type=named_type
            )
            decision = normalize_relationship_observation(
                observation,
                entities,
                case["text"],
                source_class="owner_assertion",
                explicit_current_state=True,
            )
            with self.subTest(case=case["case_id"], predicate=predicate):
                self.assertEqual(
                    decision.status,
                    "manual_review" if expected["manual_review"] else "accept",
                )
                self.assertIsNotNone(decision.normalized_observation)
                normalized = decision.normalized_observation
                self.assertEqual(normalized["temporal"]["semantic"], "state_validity")
                if direction == "named_to_self":
                    self.assertEqual(normalized["subject_entity_ref"], "e01")
                    self.assertEqual(normalized["object"]["entity_ref"], "e00")
                else:
                    self.assertEqual(normalized["subject_entity_ref"], "e00")
                    self.assertEqual(normalized["object"]["entity_ref"], "e01")
                if predicate.startswith("social."):
                    self.assertEqual(normalized["modality"], "reported_observation")
            seen.add(predicate)
        self.assertEqual(seen, set(self.contracts))

    def test_colon_namespaced_roles_are_resolved(self) -> None:
        self.assertEqual(
            relationship_role_candidates("family:mother_in_law"),
            (
                "family_mother_in_law",
                "family",
                "mother_in_law",
                "mother",
                "in",
                "law",
            ),
        )
        decision = map_entity_relationship_role(
            "family:mother_in_law",
            proposed_predicate="relationship.in_law_of",
        )
        self.assertEqual(decision.status, "accept")
        self.assertEqual(decision.mapping.edge_direction, "unordered_self_named")

    def test_compound_role_falls_back_to_registered_atomic_role(self) -> None:
        decision = map_entity_relationship_role(
            "professional:running_coach",
            proposed_predicate="relationship.coach_of",
        )
        self.assertEqual(decision.status, "accept")
        self.assertEqual(decision.mapping.edge_direction, "named_to_self")

    def test_explicit_source_reconciles_nearby_wrong_predicate(self) -> None:
        self.assertEqual(
            relationship_predicate_candidates_from_source(
                "I currently live with Jordan.",
                "social:roommate",
            ),
            ("relationship.lives_with",),
        )
        self.assertEqual(
            relationship_predicate_candidates_from_source(
                "I do not trust Alex anymore.",
                None,
            ),
            ("social.distrusts",),
        )
        self.assertEqual(
            relationship_predicate_candidates_from_source(
                "Ruth is my mother-in-law.",
                "family:mother_in_law",
            ),
            ("relationship.in_law_of",),
        )

    def test_spouse_role_cannot_be_normalized_as_sibling(self) -> None:
        text = "Jessica is my wife."
        entities, observation = packet_parts(
            text, "relationship.sibling_of", "unordered_self_named", "wife"
        )
        decision = normalize_relationship_observation(
            observation, entities, text, source_class="owner_assertion"
        )
        self.assertEqual(decision.status, "defer")
        self.assertEqual(
            decision.reason_code,
            "unregistered_or_mismatched_relationship_role",
        )

    def test_parent_direction_is_repaired_from_reversed_model_output(self) -> None:
        text = "Jerry is my father."
        entities, observation = packet_parts(
            text, "relationship.parent_of", "self_to_named", "father"
        )
        decision = normalize_relationship_observation(
            observation, entities, text, source_class="owner_assertion"
        )
        self.assertEqual(decision.status, "accept")
        self.assertEqual(decision.normalized_observation["subject_entity_ref"], "e01")
        self.assertEqual(decision.normalized_observation["object"]["entity_ref"], "e00")
        self.assertIn("canonical_relationship_direction", decision.repairs)

    def test_irregular_children_plural_entails_parent_relation(self) -> None:
        text = "David, Bertha, and Maggie are my children."
        entities, observation = packet_parts(
            text, "relationship.parent_of", "self_to_named", "child"
        )
        decision = normalize_relationship_observation(
            observation, entities, text, source_class="owner_assertion"
        )
        self.assertEqual(decision.status, "accept")
        self.assertIsNotNone(decision.normalized_observation)

    def test_model_role_without_source_evidence_is_deferred(self) -> None:
        text = "Morgan appears in the contact database."
        entities, observation = packet_parts(
            text, "relationship.friend_of", "unordered_self_named", "friend"
        )
        decision = normalize_relationship_observation(
            observation, entities, text, source_class="owner_assertion"
        )
        self.assertEqual(decision.status, "defer")
        self.assertEqual(
            decision.reason_code,
            "relationship_predicate_not_entailed_by_source",
        )

    def test_nested_possessive_relation_does_not_attach_to_self(self) -> None:
        text = "Nora is my sister's friend."
        entities, observation = packet_parts(
            text, "relationship.friend_of", "unordered_self_named", "friend"
        )
        decision = normalize_relationship_observation(
            observation, entities, text, source_class="owner_assertion"
        )
        self.assertEqual(decision.status, "defer")
        self.assertEqual(
            decision.reason_code,
            "relationship_belongs_to_third_party",
        )

    def test_technical_and_question_sources_fail_closed(self) -> None:
        text = "The predicate is named friend_of."
        entities, observation = packet_parts(
            text, "relationship.friend_of", "unordered_self_named", "friend"
        )
        for source_class in ("technical_discussion", "question_only"):
            decision = normalize_relationship_observation(
                observation, entities, text, source_class=source_class
            )
            with self.subTest(source_class=source_class):
                self.assertEqual(decision.status, "defer")

    def test_dynamic_state_requires_explicit_current_state(self) -> None:
        text = "I avoided Drew once in 2018."
        entities, observation = packet_parts(
            text, "social.avoids", "self_to_named", "person_avoided"
        )
        decision = normalize_relationship_observation(
            observation,
            entities,
            text,
            source_class="owner_assertion",
            explicit_current_state=False,
        )
        self.assertEqual(decision.status, "defer")
        self.assertEqual(decision.reason_code, "dynamic_state_not_explicit")

    def test_adversarial_state_is_owner_report_and_restricted(self) -> None:
        text = "I consider Blake an enemy after what happened."
        entities, observation = packet_parts(
            text,
            "social.perceives_as_adversary",
            "self_to_named",
            "enemy",
        )
        decision = normalize_relationship_observation(
            observation, entities, text, source_class="owner_assertion"
        )
        self.assertEqual(decision.status, "manual_review")
        normalized = decision.normalized_observation
        self.assertEqual(normalized["modality"], "reported_observation")
        self.assertEqual(normalized["sensitivity"], "restricted")
        self.assertEqual(normalized["surface_policy"], "explicit_recall_only")

    def test_objective_enemy_predicate_is_never_registered(self) -> None:
        self.assertFalse(
            predicate_supported_by_source(
                "relationship.enemy_of", "enemy", "Blake is my enemy."
            )
        )

    def test_third_party_edge_is_not_automatically_normalized(self) -> None:
        text = "Nora says Taylor is her friend."
        entities = [
            {"entity_ref": "e01", "entity_type": "person", "relationship_role": "friend"},
            {"entity_ref": "e02", "entity_type": "person", "relationship_role": "person"},
        ]
        _, observation = packet_parts(
            text, "relationship.friend_of", "unordered_self_named", "friend"
        )
        observation["subject_entity_ref"] = "e01"
        observation["object"]["entity_ref"] = "e02"
        decision = normalize_relationship_observation(
            observation, entities, text, source_class="owner_assertion"
        )
        self.assertEqual(decision.status, "defer")
        self.assertEqual(
            decision.reason_code,
            "third_party_relationship_requires_manual_resolution",
        )


if __name__ == "__main__":
    unittest.main()
