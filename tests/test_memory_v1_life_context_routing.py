from __future__ import annotations

import unittest

from rag_engine.memory_v1_intent import VERSION, classify_memory_intent


class MemoryV1LifeContextRoutingTests(unittest.TestCase):
    def assert_claim_route(
        self,
        query: str,
        expected_predicates: list[str],
    ) -> dict[str, object]:
        plan = classify_memory_intent(query, request_classification="GENERAL")
        self.assertEqual(plan["version"], "memory_intent_adapter_v15")
        self.assertEqual(VERSION, "memory_intent_adapter_v15")
        self.assertEqual(plan["memory_intent"], "personal_recall")
        self.assertEqual(plan["domains"], ["life_context"])
        self.assertTrue(plan["direct_relevance"])
        self.assertTrue(plan["routes"]["governed_claims"])
        self.assertTrue(plan["claim_context"]["eligible"])
        self.assertTrue(plan["claim_context"]["explicit_recall"])
        self.assertEqual(
            plan["claim_context"]["allowed_predicates"],
            expected_predicates,
        )
        return plan

    def test_profession_recall_routes_only_to_occupation_claims(self) -> None:
        for query in (
            "What professions have I worked in?",
            "Do you remember what my profession is?",
            "What have I done for work?",
            "Where have I worked?",
            "Have I worked as a clinical psychologist?",
        ):
            with self.subTest(query=query):
                self.assert_claim_route(query, ["occupation.works_as"])

    def test_caregiving_recall_routes_only_to_caregiver_relationship(self) -> None:
        for query in (
            "Do you remember who I care for?",
            "Who am I a caregiver for?",
            "Who have I been caring for?",
            "Tell me about my caregiving responsibilities.",
            "Am I a caregiver for Monika?",
            "Do I care for Monika?",
            "Tell me about my caregiving relationship with Monika.",
        ):
            with self.subTest(query=query):
                self.assert_claim_route(
                    query,
                    ["relationship.caregiver_for"],
                )

    def test_caregiving_support_does_not_open_occupation_claims(self) -> None:
        plan = classify_memory_intent(
            "Caring for my wife has been difficult lately.",
            request_classification="GENERAL",
        )
        self.assertEqual(plan["memory_intent"], "relevant_support")
        self.assertFalse(plan["claim_context"]["explicit_recall"])
        self.assertEqual(
            plan["claim_context"]["allowed_predicates"],
            ["preference.life", "relationship.caregiver_for"],
        )

    def test_technical_profession_question_remains_suppressed(self) -> None:
        plan = classify_memory_intent(
            "Do you remember what my profession is?",
            request_classification="TECH",
        )
        self.assertFalse(plan["routes"]["governed_claims"])
        self.assertEqual(
            plan["claim_context"]["reason"],
            "turn_intent:tech",
        )

    def test_ordinary_job_word_does_not_open_personal_memory(self) -> None:
        plan = classify_memory_intent(
            "What job is the server running?",
            request_classification="GENERAL",
        )
        self.assertFalse(plan["routes"]["governed_claims"])

    def test_direct_family_relation_recall_uses_one_relation_lane(self) -> None:
        cases = {
            "Who is my spouse?": ["relationship.spouse_of"],
            "Who is my dad?": ["relationship.parent_of"],
            "Who is my daughter?": ["relationship.parent_of"],
            "Who is my sister?": ["relationship.sibling_of"],
            "Who is my cousin?": ["relationship.cousin_of"],
            "Who is my aunt?": ["relationship.aunt_or_uncle_of"],
            "Who is my grandparent?": ["relationship.grandparent_of"],
        }
        for query, predicates in cases.items():
            with self.subTest(query=query):
                plan = classify_memory_intent(
                    query,
                    request_classification="GENERAL",
                )
                self.assertEqual(
                    plan["claim_context"]["allowed_predicates"],
                    predicates,
                )


if __name__ == "__main__":
    unittest.main()
