from __future__ import annotations

import json
from types import SimpleNamespace
import unittest

from scripts.memory_v1_predicate_runtime_profile_v2 import (
    load_runtime_profile_v2,
)
from scripts.memory_v1_relational_extraction_v5_local_provider import (
    LocalLlamaCppProvider,
    _compile_entity_links,
    _context_safe_deterministic_policy_packet,
    _example_entity,
    _example_observation,
    _literal,
    _packet,
)
from scripts.memory_v1_relational_extraction_v5_provider import ProviderPacket
from tests.test_memory_v1_local_provider_v5_2 import (
    LocalProviderV52Test,
    MODEL_SHA256,
    ROOT,
)


class SemanticCompilerV10Test(unittest.TestCase):
    @staticmethod
    def provider() -> LocalLlamaCppProvider:
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        registry = json.loads(
            profile.registry_path.read_text(encoding="utf-8")
        )
        return LocalLlamaCppProvider(
            model="qwen3-14b-local-extractor",
            model_file_sha256=MODEL_SHA256,
            runtime_revision="llama.cpp-b10066-86a9c79f8",
            registry=registry,
            transport=SimpleNamespace(
                local_model_calls=0,
                external_model_calls=0,
            ),
        )

    def test_context_bound_questions_do_not_call_the_model(self) -> None:
        provider = self.provider()
        questions = (
            "Do you know anything about Jerry's condition?",
            "Do you know anything about my family?",
            "what is my name",
            "What should I call you?",
            "In one sentence, what is progressive overload?",
            "In one sentence, explain why a warm-up matters before strength "
            "training.",
            "What can you tell me about my family?",
            "Do you know my dad's name in a certain current situation?",
            "Do you recall any of the pets that I've had in the past we "
            "currently have right now",
            "Do you remember anything about my career?",
            "Do you know anything about my work history?",
        )
        for content in questions:
            with self.subTest(content=content):
                packet = provider.extract(
                    LocalProviderV52Test.source(content),
                    evidence_context=object(),
                )
                value = packet.model_dump(mode="json")
                self.assertEqual(value["entity_mentions"], [])
                self.assertEqual(value["observations"], [])
                self.assertEqual(
                    [item["reason_code"] for item in value["deferrals"]],
                    ["question_only"],
                )
                self.assertEqual(
                    provider.last_audit["policy_guard_code"],
                    "question_only",
                )
        self.assertEqual(provider.local_model_calls, 0)
        self.assertEqual(provider.external_model_calls, 0)

    def test_contextual_confirmation_is_reviewable_without_model(self) -> None:
        provider = self.provider()
        packet = provider.extract(
            LocalProviderV52Test.source(
                "Very good that's true. Do you know my sister's name is"
            ),
            evidence_context=object(),
        )
        value = packet.model_dump(mode="json")
        self.assertEqual(value["entity_mentions"], [])
        self.assertEqual(value["observations"], [])
        self.assertEqual(
            [item["reason_code"] for item in value["deferrals"]],
            ["entity_resolution_unresolved"],
        )
        self.assertEqual(
            provider.last_audit["policy_guard_code"],
            "contextual_confirmation_requires_typed_binding",
        )
        self.assertEqual(provider.local_model_calls, 0)

    def test_mixed_assertion_and_question_remains_model_eligible(self) -> None:
        source = LocalProviderV52Test.source(
            "My dad's name is Jerry. Do you remember that?"
        )
        self.assertIsNone(
            _context_safe_deterministic_policy_packet(
                source,
                registry_version="memory_predicate_registry_v5_2",
            )
        )

    def test_general_declaration_before_question_remains_model_eligible(
        self,
    ) -> None:
        source = LocalProviderV52Test.source(
            "My father has dementia. Do you know his name?"
        )
        self.assertIsNone(
            _context_safe_deterministic_policy_packet(
                source,
                registry_version="memory_predicate_registry_v5_2",
            )
        )

    def test_operational_test_chatter_defers_without_model(self) -> None:
        provider = self.provider()
        packet = provider.extract(
            LocalProviderV52Test.source(
                "OK, well I'm just testing the memory system to see how much "
                "has been done. Thank you for your help."
            ),
            evidence_context=object(),
        )
        self.assertEqual(
            [
                item["reason_code"]
                for item in packet.model_dump(mode="json")["deferrals"]
            ],
            ["insufficient_evidence"],
        )
        self.assertEqual(provider.local_model_calls, 0)

    def test_compound_father_statement_completes_residence_and_duration(
        self,
    ) -> None:
        content = (
            "His name is Jerry and he's currently in assisted-living. "
            "He has pretty severe dementia. He only remembers for about "
            "three seconds."
        )
        source = LocalProviderV52Test.source(content)
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        registry = json.loads(
            profile.registry_path.read_text(encoding="utf-8")
        )
        person = _example_entity(
            content,
            entity_ref="e00",
            entity_type="person",
            mention_kind="named",
            name_text="Jerry",
            relationship_role="family:father",
            reason_code="explicit_named_person",
        )
        redundant_person = _example_entity(
            content,
            entity_ref="e01",
            entity_type="person",
            mention_kind="named",
            name_text="Jerry",
            relationship_role="family:father",
            reason_code="redundant_named_person",
        )
        dementia = _example_observation(
            content,
            observation_ref="o02",
            subject_entity_ref="e00",
            predicate="health.user_reported_observation",
            object_value=_literal("text", "severe dementia"),
            projection_class="supportive_context",
            surface_policy="explicit_recall_only",
            sensitivity="high",
            reason_code="explicit_health_condition",
            modality="reported_observation",
            temporal_semantic="state_validity",
        )
        malformed_residence = _example_observation(
            content,
            observation_ref="o01",
            subject_entity_ref="e00",
            predicate="residence.lives_at",
            object_value=_literal("text", "assisted living"),
            projection_class="supportive_context",
            surface_policy="mention_when_directly_relevant",
            sensitivity="medium",
            reason_code="provider_literal_residence",
            temporal_semantic="state_validity",
        )
        duration = _example_observation(
            content,
            observation_ref="o03",
            subject_entity_ref="e00",
            predicate="health.user_reported_observation",
            object_value=_literal("text", "three seconds"),
            projection_class="supportive_context",
            surface_policy="explicit_recall_only",
            sensitivity="high",
            reason_code="explicit_health_condition",
            modality="reported_observation",
            temporal_semantic="state_validity",
        )
        compiled, repairs = _compile_entity_links(
            source,
            ProviderPacket.model_validate(
                _packet(
                    entities=[person, redundant_person],
                    observations=[malformed_residence, dementia, duration],
                )
            ),
            registry,
        )
        value = compiled.model_dump(mode="json")
        entities = {
            item["entity_ref"]: item for item in value["entity_mentions"]
        }
        residence = [
            item
            for item in value["observations"]
            if item["predicate"] == "residence.lives_at"
        ]
        self.assertEqual(len(residence), 1)
        self.assertEqual(
            entities[residence[0]["object"]["entity_ref"]]["name_text"],
            "assisted living",
        )
        health_values = {
            item["object"]["value"]
            for item in value["observations"]
            if item["predicate"] == "health.user_reported_observation"
        }
        self.assertIn("severe dementia", health_values)
        self.assertIn(
            "short-term memory lasts about three seconds",
            health_values,
        )
        self.assertNotIn("three seconds", health_values)
        self.assertIn(
            "explicit_assisted_living_residence_canonicalized",
            repairs,
        )
        self.assertIn("short_term_memory_duration_canonicalized", repairs)


if __name__ == "__main__":
    unittest.main()
