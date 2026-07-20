from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "specs/memory_v1_relationship_registry_v5_1.json"
CASES_PATH = ROOT / "evals/memory_v1_relationship_v5_1_cases.jsonl"

TOP_KEYS = {
    "registry_version",
    "contract_version",
    "status",
    "runtime_active",
    "unknown_relationship_action",
    "families",
    "temporal_profiles",
    "surface_policies",
    "principles",
    "predicates",
    "forbidden_predicates",
    "deferred_families",
}
PREDICATE_KEYS = {
    "predicate",
    "family",
    "subject_entity_types",
    "object_entity_types",
    "relation_semantics",
    "canonical_direction",
    "named_party_subject_roles",
    "named_party_object_roles",
    "named_party_symmetric_roles",
    "temporal_profile",
    "perspective",
    "sensitivity_floor",
    "surface_policy",
    "manual_review_rules",
    "description",
}
REQUIRED_PREDICATES = {
    "relationship.acquaintance_of",
    "relationship.aunt_or_uncle_of",
    "relationship.business_partner_of",
    "relationship.caregiver_for",
    "relationship.coach_of",
    "relationship.collaborator_with",
    "relationship.cousin_of",
    "relationship.coworker_of",
    "relationship.friend_of",
    "relationship.grandparent_of",
    "relationship.guardian_of",
    "relationship.has_pet",
    "relationship.healthcare_provider_for",
    "relationship.in_law_of",
    "relationship.lives_with",
    "relationship.manager_of",
    "relationship.mentor_of",
    "relationship.neighbor_of",
    "relationship.parent_of",
    "relationship.plan_helper_for",
    "relationship.relative_of",
    "relationship.romantic_partner_of",
    "relationship.roommate_of",
    "relationship.sibling_of",
    "relationship.spouse_of",
    "relationship.teacher_of",
    "relationship.teammate_of",
    "relationship.training_partner_of",
    "social.avoids",
    "social.competes_with",
    "social.depends_on",
    "social.distrusts",
    "social.estranged_from",
    "social.experiences_tension_with",
    "social.feels_close_to",
    "social.feels_unsafe_with",
    "social.in_conflict_with",
    "social.no_contact_with",
    "social.perceives_as_adversary",
    "social.supports",
    "social.trusts",
}
ADVERSARIAL_OR_SAFETY = {
    "social.avoids",
    "social.distrusts",
    "social.estranged_from",
    "social.feels_unsafe_with",
    "social.in_conflict_with",
    "social.no_contact_with",
    "social.perceives_as_adversary",
}
NAME_RE = re.compile(r"^(?:relationship|social)\.[a-z][a-z0-9_]*$")


def load_registry() -> dict:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def load_cases() -> list[dict]:
    rows = []
    for line_number, line in enumerate(
        CASES_PATH.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            raise AssertionError(f"blank JSONL line {line_number}")
        rows.append(json.loads(line))
    return rows


def unique_strings(value: object, label: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AssertionError(f"{label} must be a string list")
    if nonempty and not value:
        raise AssertionError(f"{label} must not be empty")
    if len(value) != len(set(value)):
        raise AssertionError(f"{label} must contain unique values")
    return value


class RelationshipRegistryV51Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = load_registry()
        cls.cases = load_cases()
        cls.rows = {
            row["predicate"]: row for row in cls.registry.get("predicates", [])
        }

    def test_registry_is_explicitly_offline(self) -> None:
        self.assertEqual(set(self.registry), TOP_KEYS)
        self.assertEqual(
            self.registry["registry_version"], "memory_relationship_registry_v5_1"
        )
        self.assertEqual(
            self.registry["contract_version"],
            "memory_v1_relationship_extraction_v5_1",
        )
        self.assertEqual(self.registry["status"], "proposed")
        self.assertIs(self.registry["runtime_active"], False)
        self.assertEqual(
            self.registry["unknown_relationship_action"],
            "defer_unregistered_relationship",
        )

    def test_predicate_set_is_complete_unique_and_sorted(self) -> None:
        rows = self.registry["predicates"]
        names = [row["predicate"] for row in rows]
        self.assertEqual(names, sorted(names))
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), REQUIRED_PREDICATES)

    def test_predicate_contract_shapes(self) -> None:
        families = unique_strings(self.registry["families"], "families", nonempty=True)
        self.assertEqual(families, sorted(families))
        temporal_profiles = set(self.registry["temporal_profiles"])
        surface_policies = set(self.registry["surface_policies"])
        allowed_entities = {"animal", "person", "self"}
        for row in self.registry["predicates"]:
            name = row["predicate"]
            self.assertEqual(set(row), PREDICATE_KEYS, name)
            self.assertRegex(name, NAME_RE)
            self.assertIn(row["family"], families, name)
            subjects = set(
                unique_strings(row["subject_entity_types"], f"{name} subjects", nonempty=True)
            )
            objects = set(
                unique_strings(row["object_entity_types"], f"{name} objects", nonempty=True)
            )
            self.assertLessEqual(subjects | objects, allowed_entities, name)
            self.assertIn(row["relation_semantics"], {"directed", "symmetric"}, name)
            self.assertIn(row["temporal_profile"], temporal_profiles, name)
            self.assertIn(
                row["perspective"],
                {"owner_reported_connection", "owner_reported_state"},
                name,
            )
            self.assertIn(
                row["sensitivity_floor"],
                {"low", "medium", "high", "restricted"},
                name,
            )
            self.assertIn(row["surface_policy"], surface_policies, name)
            for field in (
                "named_party_subject_roles",
                "named_party_object_roles",
                "named_party_symmetric_roles",
                "manual_review_rules",
            ):
                unique_strings(row[field], f"{name} {field}")
            self.assertTrue(row["description"].strip(), name)

    def test_canonical_direction_and_role_sides_are_unambiguous(self) -> None:
        for name, row in self.rows.items():
            if row["relation_semantics"] == "symmetric":
                self.assertEqual(row["canonical_direction"], "unordered_entity_pair", name)
                self.assertFalse(row["named_party_subject_roles"], name)
                self.assertFalse(row["named_party_object_roles"], name)
                self.assertTrue(row["named_party_symmetric_roles"], name)
                self.assertEqual(
                    set(row["subject_entity_types"]),
                    set(row["object_entity_types"]),
                    name,
                )
            else:
                self.assertNotEqual(row["canonical_direction"], "unordered_entity_pair", name)
                self.assertFalse(row["named_party_symmetric_roles"], name)

    def test_parent_and_child_language_share_one_canonical_edge(self) -> None:
        parent = self.rows["relationship.parent_of"]
        self.assertEqual(set(parent["subject_entity_types"]), {"person", "self"})
        self.assertEqual(set(parent["object_entity_types"]), {"person", "self"})
        self.assertIn("father", parent["named_party_subject_roles"])
        self.assertIn("mother", parent["named_party_subject_roles"])
        self.assertIn("son", parent["named_party_object_roles"])
        self.assertIn("daughter", parent["named_party_object_roles"])
        self.assertNotIn("relationship.child_of", self.rows)

    def test_partner_categories_do_not_collapse(self) -> None:
        spouse = self.rows["relationship.spouse_of"]
        romantic = self.rows["relationship.romantic_partner_of"]
        self.assertEqual(
            set(spouse["named_party_symmetric_roles"]), {"husband", "spouse", "wife"}
        )
        self.assertIn("boyfriend", romantic["named_party_symmetric_roles"])
        self.assertIn("girlfriend", romantic["named_party_symmetric_roles"])
        self.assertNotIn("partner", romantic["named_party_symmetric_roles"])

    def test_relational_states_are_owner_perspective_and_fast_decay(self) -> None:
        for name, row in self.rows.items():
            if not name.startswith("social."):
                continue
            self.assertEqual(row["family"], "relational_state", name)
            self.assertEqual(row["temporal_profile"], "dynamic_state", name)
            self.assertEqual(row["perspective"], "owner_reported_state", name)
            self.assertEqual(row["relation_semantics"], "directed", name)
            self.assertIn("self", row["subject_entity_types"], name)
            self.assertIn("person", row["object_entity_types"], name)
            if set(row["subject_entity_types"]) != {"self"}:
                self.assertIn("self_endpoint_required", row["manual_review_rules"], name)

    def test_adversarial_states_are_high_sensitivity(self) -> None:
        for name in ADVERSARIAL_OR_SAFETY:
            row = self.rows[name]
            self.assertIn(row["sensitivity_floor"], {"high", "restricted"}, name)
            self.assertIn(
                row["surface_policy"],
                {
                    "explicit_person_or_relationship_context_only",
                    "restricted_explicit_recall_only",
                },
                name,
            )
        self.assertNotIn("relationship.enemy_of", self.rows)
        adversary = self.rows["social.perceives_as_adversary"]
        self.assertEqual(adversary["perspective"], "owner_reported_state")
        self.assertEqual(adversary["sensitivity_floor"], "restricted")

    def test_product_permissions_cannot_become_memory_evidence(self) -> None:
        principles = self.registry["principles"]
        self.assertIs(principles["account_relationships_are_not_memory_evidence"], True)
        self.assertIn(
            "lifeswitch_permission_must_not_be_inferred",
            self.rows["relationship.plan_helper_for"]["manual_review_rules"],
        )

    def test_cases_cover_every_predicate(self) -> None:
        covered: set[str] = set()
        ids = []
        for row in self.cases:
            ids.append(row["case_id"])
            expected = row["expected"]
            predicate = expected.get("predicate")
            if predicate:
                covered.add(predicate)
            covered.update(expected.get("required_predicates", []))
            self.assertIsInstance(expected.get("forbidden_predicates"), list)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(covered, REQUIRED_PREDICATES)

    def test_negative_cases_block_metaphor_code_fiction_and_questions(self) -> None:
        by_id = {row["case_id"]: row["expected"] for row in self.cases}
        for case_id in {
            "rel-v5_1-042",
            "rel-v5_1-043",
            "rel-v5_1-044",
            "rel-v5_1-045",
            "rel-v5_1-046",
            "rel-v5_1-047",
            "rel-v5_1-048",
            "rel-v5_1-050",
            "rel-v5_1-051",
            "rel-v5_1-052",
            "rel-v5_1-053",
        }:
            self.assertIn(by_id[case_id]["outcome"], {"defer", "no_relationship"})
            self.assertIsNone(by_id[case_id]["predicate"])

    def test_temporal_status_is_not_encoded_in_predicate_names(self) -> None:
        self.assertIn("relationship.ex_spouse_of", self.registry["forbidden_predicates"])
        self.assertIn("relationship.ex_friend_of", self.registry["forbidden_predicates"])
        by_id = {row["case_id"]: row["expected"] for row in self.cases}
        self.assertEqual(by_id["rel-v5_1-055"]["predicate"], "relationship.spouse_of")
        self.assertEqual(
            by_id["rel-v5_1-055"]["temporal_expectation"], "closed_or_bounded"
        )
        self.assertEqual(by_id["rel-v5_1-056"]["predicate"], "relationship.friend_of")

    def test_compound_family_case_cannot_turn_spouse_or_children_into_siblings(self) -> None:
        case = next(row for row in self.cases if row["case_id"] == "rel-v5_1-049")
        expected = case["expected"]
        self.assertEqual(
            set(expected["required_predicates"]),
            {"relationship.parent_of", "relationship.spouse_of"},
        )
        self.assertIn("relationship.sibling_of", expected["forbidden_predicates"])


if __name__ == "__main__":
    unittest.main()
