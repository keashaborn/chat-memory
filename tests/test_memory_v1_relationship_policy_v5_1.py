from __future__ import annotations

import unittest

from scripts.memory_v1_relationship_policy_v5_1 import (
    POLICY_VERSION,
    assess_relationship_proposal,
    map_named_party_role,
)


class RelationshipPolicyV51Test(unittest.TestCase):
    def test_policy_version_is_explicit(self) -> None:
        self.assertEqual(POLICY_VERSION, "memory_v1_relationship_policy_v5_1")

    def test_wife_maps_to_symmetric_spouse_not_sibling(self) -> None:
        decision = map_named_party_role(
            "wife", proposed_predicate="relationship.spouse_of"
        )
        self.assertEqual(decision.status, "accept")
        self.assertEqual(decision.mapping.predicate, "relationship.spouse_of")
        self.assertEqual(decision.mapping.edge_direction, "unordered_self_named")
        mismatch = map_named_party_role(
            "wife", proposed_predicate="relationship.sibling_of"
        )
        self.assertEqual(mismatch.status, "defer")

    def test_parent_and_child_roles_reverse_one_predicate(self) -> None:
        father = map_named_party_role(
            "father", proposed_predicate="relationship.parent_of"
        )
        son = map_named_party_role(
            "son", proposed_predicate="relationship.parent_of"
        )
        self.assertEqual(father.mapping.edge_direction, "named_to_self")
        self.assertEqual(son.mapping.edge_direction, "self_to_named")

    def test_enemy_is_owner_stance_not_objective_relationship(self) -> None:
        objective = map_named_party_role(
            "enemy", proposed_predicate="relationship.enemy_of"
        )
        self.assertEqual(objective.status, "defer")
        stance = map_named_party_role(
            "enemy", proposed_predicate="social.perceives_as_adversary"
        )
        self.assertEqual(stance.status, "accept")
        self.assertEqual(stance.mapping.perspective, "owner_reported_state")
        self.assertEqual(stance.mapping.sensitivity_floor, "restricted")

    def test_plain_partner_fails_closed(self) -> None:
        decision = map_named_party_role("partner")
        self.assertEqual(decision.status, "defer")
        self.assertEqual(
            decision.reason_code, "unregistered_or_mismatched_relationship_role"
        )

    def test_ambiguous_support_person_requires_predicate(self) -> None:
        decision = map_named_party_role("support_person")
        self.assertEqual(decision.status, "defer")
        self.assertEqual(decision.reason_code, "ambiguous_relationship_role")
        dependency = map_named_party_role(
            "support_person", proposed_predicate="social.depends_on"
        )
        support = map_named_party_role(
            "support_person", proposed_predicate="social.supports"
        )
        self.assertEqual(dependency.mapping.edge_direction, "self_to_named")
        self.assertEqual(support.mapping.edge_direction, "named_to_self")

    def test_technical_discussion_cannot_create_spouse_edge(self) -> None:
        decision = assess_relationship_proposal(
            predicate="relationship.spouse_of",
            named_party_role="wife",
            source_class="technical_discussion",
            subject_entity_type="self",
            object_entity_type="person",
            self_endpoint_count=1,
        )
        self.assertEqual(decision.status, "defer")
        self.assertEqual(
            decision.reason_code,
            "ineligible_relationship_source:technical_discussion",
        )

    def test_non_person_rival_cannot_create_competition_state(self) -> None:
        decision = assess_relationship_proposal(
            predicate="social.competes_with",
            named_party_role="rival",
            source_class="owner_assertion",
            subject_entity_type="self",
            object_entity_type="concept",
            self_endpoint_count=1,
        )
        self.assertEqual(decision.status, "defer")
        self.assertEqual(decision.reason_code, "relationship_object_type_mismatch")

    def test_dynamic_state_requires_explicit_current_state(self) -> None:
        decision = assess_relationship_proposal(
            predicate="social.in_conflict_with",
            named_party_role="person_in_conflict",
            source_class="owner_assertion",
            subject_entity_type="self",
            object_entity_type="person",
            self_endpoint_count=1,
            explicit_current_state=False,
        )
        self.assertEqual(decision.status, "defer")
        self.assertEqual(decision.reason_code, "dynamic_state_not_explicit")

    def test_restricted_state_routes_to_manual_review(self) -> None:
        decision = assess_relationship_proposal(
            predicate="social.feels_unsafe_with",
            named_party_role="person_felt_unsafe_with",
            source_class="owner_assertion",
            subject_entity_type="self",
            object_entity_type="person",
            self_endpoint_count=1,
        )
        self.assertEqual(decision.status, "manual_review")
        self.assertTrue(decision.manual_review_required)

    def test_third_party_structural_edge_routes_to_manual_review(self) -> None:
        decision = assess_relationship_proposal(
            predicate="relationship.friend_of",
            named_party_role="friend",
            source_class="owner_assertion",
            subject_entity_type="person",
            object_entity_type="person",
            self_endpoint_count=0,
            third_party_edge=True,
        )
        self.assertEqual(decision.status, "manual_review")

    def test_social_state_without_self_endpoint_is_rejected(self) -> None:
        decision = assess_relationship_proposal(
            predicate="social.supports",
            named_party_role="supporter",
            source_class="owner_assertion",
            subject_entity_type="person",
            object_entity_type="person",
            self_endpoint_count=0,
            third_party_edge=True,
        )
        self.assertEqual(decision.status, "defer")
        self.assertEqual(
            decision.reason_code, "owner_perspective_requires_self_endpoint"
        )

    def test_direct_low_sensitivity_connection_can_pass(self) -> None:
        decision = assess_relationship_proposal(
            predicate="relationship.friend_of",
            named_party_role="friend",
            source_class="owner_assertion",
            subject_entity_type="self",
            object_entity_type="person",
            self_endpoint_count=1,
        )
        self.assertEqual(decision.status, "accept")
        self.assertFalse(decision.manual_review_required)


if __name__ == "__main__":
    unittest.main()
