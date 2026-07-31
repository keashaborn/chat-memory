from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from scripts.memory_v1_relational_extraction_v5_local_provider import (
    SEMANTIC_V5_2_POLICY_COMPILER_VERSION,
    _compile_entity_links,
    _example_entity,
    _example_observation,
    _literal,
    _packet,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    ProviderPacket,
    TrustedExtractionSource,
)


ROOT = Path(__file__).resolve().parents[1]


def source(content: str, external_id: str) -> TrustedExtractionSource:
    return TrustedExtractionSource.create(
        job_id="862f5ab6-a037-44b0-be54-1b8c8c25f941",
        source_system="public.chat_log",
        source_external_id=external_id,
        source_sha256=hashlib.sha256(content.encode()).hexdigest(),
        source_recorded_at="2026-07-31T11:24:23.478195Z",
        source_observed_at="2026-07-30T21:39:53.840736Z",
        content=content,
    )


def registry() -> dict[str, object]:
    return json.loads(
        (
            ROOT / "specs/memory_v1_predicate_registry_v5_2_compiler_v12.json"
        ).read_text()
    )


class SemanticCompilerV12Test(unittest.TestCase):
    def test_compiler_version_is_v12(self) -> None:
        self.assertEqual(
            SEMANTIC_V5_2_POLICY_COMPILER_VERSION,
            "memory_v1_semantic_policy_compiler_v12",
        )

    def test_explicit_name_care_setting_and_duration_are_completed(self) -> None:
        content = (
            "His name is Jerry and he's currently in assisted-living. "
            "He has pretty severe dementia. "
            "He only remembers for about three seconds."
        )
        trusted = source(content, "681ab38d-a742-463c-ad26-c74c65eacaa9")
        entities = [
            _example_entity(
                content,
                entity_ref="e00",
                entity_type="person",
                mention_kind="role_only",
                name_text=None,
                relationship_role="family:father",
                reason_code="explicit_relationship_role",
            ),
            _example_entity(
                content,
                entity_ref="e01",
                entity_type="place",
                mention_kind="named",
                name_text="assisted living",
                relationship_role="residence:reported",
                reason_code="model_place_category",
            ),
        ]
        observations = [
            _example_observation(
                content,
                observation_ref="o00",
                subject_entity_ref="e00",
                predicate="residence.lives_at",
                object_value={"kind": "entity", "entity_ref": "e01"},
                projection_class="supportive_context",
                surface_policy="explicit_recall_only",
                sensitivity="high",
                reason_code="model_residence",
                temporal_semantic="state_validity",
            ),
            _example_observation(
                content,
                observation_ref="o01",
                subject_entity_ref="e00",
                predicate="health.user_reported_observation",
                object_value=_literal("text", "severe dementia"),
                projection_class="supportive_context",
                surface_policy="explicit_recall_only",
                sensitivity="high",
                reason_code="explicit_health_condition",
                modality="reported_observation",
                temporal_semantic="state_validity",
            ),
            _example_observation(
                content,
                observation_ref="o02",
                subject_entity_ref="e00",
                predicate="health.user_reported_observation",
                object_value=_literal("text", "about three seconds"),
                projection_class="supportive_context",
                surface_policy="explicit_recall_only",
                sensitivity="high",
                reason_code="explicit_health_condition",
                modality="reported_observation",
                temporal_semantic="state_validity",
            ),
        ]
        packet = ProviderPacket.model_validate(
            _packet(entities=entities, observations=observations)
        )

        compiled, repairs = _compile_entity_links(trusted, packet, registry())
        value = compiled.model_dump(mode="json")
        by_predicate: dict[str, list[dict[str, object]]] = {}
        for item in value["observations"]:
            by_predicate.setdefault(item["predicate"], []).append(item)

        self.assertNotIn("residence.lives_at", by_predicate)
        self.assertEqual(
            by_predicate["residence.care_setting"][0]["object"]["value"],
            "assisted_living",
        )
        self.assertEqual(
            by_predicate["residence.care_setting"][0]["source_spans"][0][
                "quote"
            ],
            "assisted-living",
        )
        self.assertEqual(
            by_predicate["identity.name"][0]["object"]["value"], "Jerry"
        )
        self.assertEqual(
            by_predicate["identity.name"][0]["source_spans"][0]["quote"],
            "Jerry",
        )
        duration = next(
            item
            for item in by_predicate["health.user_reported_observation"]
            if "explicit_short_term_memory_duration" in item["reason_codes"]
        )
        self.assertEqual(
            duration["object"]["value"],
            "short-term memory lasts about three seconds",
        )
        self.assertEqual(
            duration["source_spans"][0]["quote"],
            "He only remembers for about three seconds",
        )
        self.assertEqual(len(value["entity_mentions"]), 1)
        self.assertEqual(value["entity_mentions"][0]["name_text"], "Jerry")
        self.assertIn("assisted_living_setting_completed", repairs)
        self.assertIn("explicit_entity_name_observation_completed", repairs)

    def test_misaligned_stance_span_is_repaired_to_supporting_clause(self) -> None:
        content = (
            "Yes, but when you do that sort of thing, no one's really gonna "
            "understand what you're talking about so I see it as what is a human "
            "being doing when they recognize someone is near them how do they "
            "affecting that someone and how are they changing it because of it "
            "now a human being in my perspective is a fractal and has the same "
            "functions as any fractal above or below it. And why do I say it's a "
            "fractal because it is a rule set that is a combination of rule sets."
        )
        trusted = source(content, "61d4fb6f-b211-491e-8edc-d160efefe17e")
        entity = _example_entity(
            content,
            entity_ref="e00",
            entity_type="self",
            mention_kind="role_only",
            name_text=None,
            relationship_role="user:self",
            reason_code="first_person_subject",
        )
        observation = _example_observation(
            content,
            observation_ref="o00",
            subject_entity_ref="e00",
            predicate="stance.reported",
            object_value=_literal(
                "json",
                {
                    "context": None,
                    "position": (
                        "a human being is a fractal and has the same functions "
                        "as any fractal above or below it"
                    ),
                    "topic_key": "ontology.human_being_as_fractal",
                    "topic_text": "human being as fractal",
                    "orientation": "supports",
                },
            ),
            projection_class="reported_stance",
            surface_policy="relevant_recall_or_explicit_recall",
            sensitivity="medium",
            reason_code="explicit_reported_stance",
            modality="reported_belief",
        )
        observation["source_spans"] = [
            {"start": 206, "end": 226, "quote": content[206:226]}
        ]
        packet = ProviderPacket.model_validate(
            _packet(entities=[entity], observations=[observation])
        )

        compiled, repairs = _compile_entity_links(trusted, packet, registry())
        result = compiled.model_dump(mode="json")["observations"][0]
        quote = result["source_spans"][0]["quote"]
        self.assertIn("a human being in my perspective is a fractal", quote)
        self.assertNotEqual(quote, content[206:226])
        self.assertIn("observation_source_span_realigned", repairs)


if __name__ == "__main__":
    unittest.main()
