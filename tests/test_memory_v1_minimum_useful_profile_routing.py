from __future__ import annotations

import unittest

from rag_engine.memory_v1_entity_scope_resolver_v2 import (
    resolve_memory_claim_selector_context_v2,
)
from rag_engine.memory_v1_intent import VERSION, classify_memory_intent
from tests.test_memory_v1_entity_scope_resolver_v2 import (
    BCBA,
    CLINICAL_PSYCHOLOGIST,
    DAHLIA,
    DAD,
    SELF,
    request,
    snapshot,
)


SELF_PROFILE_PREDICATES = [
    "identity.name",
    "occupation.works_as",
    "relationship.has_pet",
    "relationship.parent_of",
    "relationship.spouse_of",
    "stance.reported",
]


class MemoryV1MinimumUsefulProfileRoutingTests(unittest.TestCase):
    def test_self_name_recall_uses_only_owner_self_name(self) -> None:
        for query in (
            "What is my name?",
            "What's my name?",
            "Do you remember my name?",
            "What do you remember my name is?",
        ):
            with self.subTest(query=query):
                plan = classify_memory_intent(
                    query,
                    request_classification="GENERAL",
                )
                self.assertEqual(VERSION, "memory_intent_adapter_v17")
                self.assertEqual(plan["domains"], ["self_identity"])
                self.assertTrue(plan["routes"]["governed_claims"])
                self.assertEqual(
                    plan["claim_context"]["allowed_predicates"],
                    ["identity.name"],
                )
                self.assertFalse(
                    plan["claim_context"]["broad_profile_recall"]
                )

    def test_generic_self_profile_recall_has_a_closed_safe_predicate_set(
        self,
    ) -> None:
        for query in (
            "What do you know about me?",
            "Do you remember anything about me?",
            "Tell me what you know about me.",
            "What personal facts do you remember about me?",
        ):
            with self.subTest(query=query):
                plan = classify_memory_intent(
                    query,
                    request_classification="GENERAL",
                )
                self.assertEqual(plan["domains"], ["self_profile"])
                self.assertTrue(plan["routes"]["governed_claims"])
                self.assertTrue(plan["claim_context"]["explicit_recall"])
                self.assertTrue(
                    plan["claim_context"]["broad_profile_recall"]
                )
                self.assertEqual(
                    plan["claim_context"]["allowed_predicates"],
                    SELF_PROFILE_PREDICATES,
                )
                self.assertNotIn(
                    "life_event.died",
                    plan["claim_context"]["allowed_predicates"],
                )
                self.assertNotIn(
                    "relationship.caregiver_for",
                    plan["claim_context"]["allowed_predicates"],
                )

    def test_information_providing_name_turn_does_not_trigger_retrieval(
        self,
    ) -> None:
        plan = classify_memory_intent(
            "My name is Eric Lund.",
            request_classification="GENERAL",
        )
        self.assertFalse(plan["routes"]["governed_claims"])

    def test_technical_signal_suppresses_generic_profile_recall(self) -> None:
        plan = classify_memory_intent(
            "What do you know about me?",
            request_classification="TECH",
        )
        self.assertFalse(plan["routes"]["governed_claims"])
        self.assertEqual(plan["claim_context"]["reason"], "turn_intent:tech")

    def test_self_identity_scope_is_owner_self_only(self) -> None:
        context = resolve_memory_claim_selector_context_v2(
            request=request("What is my name?"),
            claim_context={
                "domain": "self_identity",
                "allowed_predicates": ["identity.name"],
            },
            snapshot=snapshot(),
        )
        rule = context.entity_scope.predicate_rules[0]
        self.assertEqual(rule.predicate, "identity.name")
        self.assertEqual(rule.subject_entity_ids, (SELF,))

    def test_broad_self_profile_scope_uses_only_governed_neighbors(self) -> None:
        context = resolve_memory_claim_selector_context_v2(
            request=request("What do you know about me?"),
            claim_context={
                "domain": "self_profile",
                "allowed_predicates": SELF_PROFILE_PREDICATES,
            },
            snapshot=snapshot(),
        )
        rules = {
            item.predicate: item
            for item in context.entity_scope.predicate_rules
        }
        self.assertEqual(rules["identity.name"].subject_entity_ids, (SELF,))
        self.assertEqual(rules["stance.reported"].subject_entity_ids, (SELF,))
        self.assertEqual(
            set(rules["occupation.works_as"].object_entity_ids),
            {BCBA, CLINICAL_PSYCHOLOGIST},
        )
        self.assertEqual(
            rules["relationship.has_pet"].object_entity_ids,
            (DAHLIA,),
        )
        self.assertEqual(
            rules["relationship.parent_of"].subject_entity_ids,
            (DAD,),
        )
