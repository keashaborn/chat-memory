from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from scripts.memory_v1_predicate_runtime_profile_v2 import load_runtime_profile_v2
from scripts.memory_v1_relational_extraction_v5_local_provider import (
    LocalLlamaCppProvider,
    LocalProviderAdapterError,
    SEMANTIC_V5_2_REGISTRY_VERSION,
    SEMANTIC_V5_2_POLICY_COMPILER_VERSION,
    _compile_entity_links,
    _credential_packet,
    _deterministic_policy_packet,
    _packet,
    _structured_result,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    ProviderPacket,
    TrustedExtractionSource,
)


ROOT = Path(__file__).resolve().parents[1]
MODEL_SHA256 = "5" * 64


class LocalProviderV52Test(unittest.TestCase):
    @staticmethod
    def source(content: str) -> TrustedExtractionSource:
        return TrustedExtractionSource.create(
            job_id="00000000-0000-4000-8000-000000000001",
            source_system="public.chat_log",
            source_external_id="00000000-0000-4000-8000-000000000002",
            source_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            source_recorded_at="2026-07-21T12:00:00+00:00",
            content=content,
        )

    def test_v5_2_prompt_and_schema_are_bound_to_semantic_rules(self) -> None:
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        registry = json.loads(profile.registry_path.read_text(encoding="utf-8"))
        provider = LocalLlamaCppProvider(
            model="qwen3-14b-local-extractor",
            model_file_sha256=MODEL_SHA256,
            runtime_revision="llama.cpp-b10066-86a9c79f8",
            registry=registry,
            transport=object(),
        )
        content = "I think public opinion is not the same as evidence."
        source = TrustedExtractionSource.create(
            job_id="00000000-0000-4000-8000-000000000001",
            source_system="public.chat_log",
            source_external_id="00000000-0000-4000-8000-000000000002",
            source_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            source_recorded_at="2026-07-21T12:00:00+00:00",
            content=content,
        )
        request = provider.request(source)
        self.assertIn("SEMANTIC_V5_2_RULES", request.instructions)
        self.assertIn("SEMANTIC_STANCE_COMPACT_V1", request.instructions)
        self.assertIn("stance.reported", request.instructions)
        self.assertEqual(request.prompt_profile, "semantic_stance_compact_v1")
        self.assertLess(len(request.instructions), 15_000)
        predicate = request.output_schema["$defs"]["ProviderObservation"][
            "properties"
        ]["predicate"]
        self.assertEqual(predicate["enum"], ["stance.reported"])
        observation_properties = request.output_schema["$defs"][
            "ProviderObservation"
        ]["properties"]
        self.assertEqual(
            observation_properties["object"],
            {"$ref": "#/$defs/LiteralObject"},
        )
        self.assertEqual(
            observation_properties["modality"]["const"],
            "reported_belief",
        )
        self.assertEqual(
            observation_properties["projection_class"]["const"],
            "reported_stance",
        )
        self.assertEqual(
            observation_properties["surface_policy"]["const"],
            "relevant_recall_or_explicit_recall",
        )
        literal_properties = request.output_schema["$defs"]["LiteralObject"][
            "properties"
        ]
        self.assertEqual(literal_properties["datatype"]["const"], "json")
        self.assertEqual(
            literal_properties["value"],
            {"$ref": "#/$defs/ReportedStanceValue"},
        )
        self.assertEqual(literal_properties["unit"], {"type": "null"})
        self.assertEqual(literal_properties["approximate"]["const"], False)
        self.assertEqual(request.output_schema["properties"]["observations"]["minItems"], 1)
        self.assertEqual(
            provider._policy_compiler_version,
            SEMANTIC_V5_2_POLICY_COMPILER_VERSION,
        )
        self.assertEqual(
            provider._policy_compiler_version,
            "memory_v1_semantic_policy_compiler_v8",
        )

    def test_multiple_explicit_stance_cues_require_atomic_split(self) -> None:
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        registry = json.loads(profile.registry_path.read_text(encoding="utf-8"))
        provider = LocalLlamaCppProvider(
            model="qwen3-14b-local-extractor",
            model_file_sha256=MODEL_SHA256,
            runtime_revision="llama.cpp-b10066-86a9c79f8",
            registry=registry,
            transport=object(),
        )
        request = provider.request(
            self.source(
                "I think the future cannot be predicted. "
                "I believe worrying about next month is not useful."
            )
        )
        self.assertEqual(request.prompt_profile, "semantic_stance_compact_v1")
        self.assertIn(
            "Return at least two non-duplicate atomic stance observations",
            request.instructions,
        )
        self.assertEqual(
            request.output_schema["properties"]["observations"]["minItems"],
            2,
        )

    def test_education_source_uses_compact_governed_route(self) -> None:
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        registry = json.loads(profile.registry_path.read_text(encoding="utf-8"))
        provider = LocalLlamaCppProvider(
            model="qwen3-14b-local-extractor",
            model_file_sha256=MODEL_SHA256,
            runtime_revision="llama.cpp-b10066-86a9c79f8",
            registry=registry,
            transport=object(),
        )
        request = provider.request(
            self.source("I attended the University of Wisconsin.")
        )
        self.assertEqual(
            request.prompt_profile,
            "personal_context_compact_v1",
        )
        self.assertIn("PERSONAL_CONTEXT_COMPACT_V1", request.instructions)
        self.assertLess(len(request.instructions), 12_000)
        predicate = request.output_schema["$defs"]["ProviderObservation"][
            "properties"
        ]["predicate"]
        self.assertEqual(predicate["enum"], ["education.attended"])

    def test_pet_death_source_uses_compact_governed_route(self) -> None:
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        registry = json.loads(profile.registry_path.read_text(encoding="utf-8"))
        provider = LocalLlamaCppProvider(
            model="qwen3-14b-local-extractor",
            model_file_sha256=MODEL_SHA256,
            runtime_revision="llama.cpp-b10066-86a9c79f8",
            registry=registry,
            transport=object(),
        )
        request = provider.request(
            self.source(
                "My male German shepherd had a rare blood cancer and died."
            )
        )
        self.assertEqual(
            request.prompt_profile,
            "personal_context_compact_v1",
        )
        predicate = request.output_schema["$defs"]["ProviderObservation"][
            "properties"
        ]["predicate"]
        self.assertEqual(
            predicate["enum"],
            [
                "health.user_reported_observation",
                "life_event.died",
                "pet.breed",
                "pet.sex",
                "relationship.has_pet",
            ],
        )

    def test_generic_lost_cat_uses_compact_governed_route(self) -> None:
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        registry = json.loads(profile.registry_path.read_text(encoding="utf-8"))
        provider = LocalLlamaCppProvider(
            model="qwen3-14b-local-extractor",
            model_file_sha256=MODEL_SHA256,
            runtime_revision="llama.cpp-b10066-86a9c79f8",
            registry=registry,
            transport=object(),
        )
        request = provider.request(
            self.source("About two years ago I lost a cat I loved a lot.")
        )
        self.assertEqual(
            request.prompt_profile,
            "personal_context_compact_v1",
        )
        predicate = request.output_schema["$defs"]["ProviderObservation"][
            "properties"
        ]["predicate"]
        self.assertEqual(
            predicate["enum"],
            ["life_event.died", "relationship.has_pet"],
        )

    def test_caregiving_health_source_uses_compact_governed_route(self) -> None:
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        registry = json.loads(profile.registry_path.read_text(encoding="utf-8"))
        provider = LocalLlamaCppProvider(
            model="qwen3-14b-local-extractor",
            model_file_sha256=MODEL_SHA256,
            runtime_revision="llama.cpp-b10066-86a9c79f8",
            registry=registry,
            transport=object(),
        )
        request = provider.request(
            self.source(
                "I cared for her for years because she had skin and allergy "
                "issues."
            )
        )
        self.assertEqual(
            request.prompt_profile,
            "personal_context_compact_v1",
        )
        predicate = request.output_schema["$defs"]["ProviderObservation"][
            "properties"
        ]["predicate"]
        self.assertEqual(
            predicate["enum"],
            [
                "health.user_reported_observation",
                "relationship.caregiver_for",
            ],
        )

    def test_third_person_role_uses_compact_governed_route(self) -> None:
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        registry = json.loads(profile.registry_path.read_text(encoding="utf-8"))
        provider = LocalLlamaCppProvider(
            model="qwen3-14b-local-extractor",
            model_file_sha256=MODEL_SHA256,
            runtime_revision="llama.cpp-b10066-86a9c79f8",
            registry=registry,
            transport=object(),
        )
        request = provider.request(
            self.source(
                "I talked to Bob Fry who was the president at the time."
            )
        )
        self.assertEqual(
            request.prompt_profile,
            "personal_context_compact_v1",
        )
        self.assertIn(
            "I talked to Jordan Lee, who was the president at the time.",
            request.instructions,
        )
        self.assertIn(
            '"predicate":"occupation.works_as"',
            request.instructions,
        )
        predicate = request.output_schema["$defs"]["ProviderObservation"][
            "properties"
        ]["predicate"]
        self.assertEqual(predicate["enum"], ["occupation.works_as"])

    def test_employer_statement_uses_compact_governed_route(self) -> None:
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        registry = json.loads(profile.registry_path.read_text(encoding="utf-8"))
        provider = LocalLlamaCppProvider(
            model="qwen3-14b-local-extractor",
            model_file_sha256=MODEL_SHA256,
            runtime_revision="llama.cpp-b10066-86a9c79f8",
            registry=registry,
            transport=object(),
        )
        request = provider.request(
            self.source(
                "I started working for the Wisconsin Early Autism Project."
            )
        )
        self.assertEqual(request.prompt_profile, "employment_compact_v1")
        self.assertIn("EMPLOYMENT_COMPACT_V1", request.instructions)
        self.assertIn(
            "does not by itself prove that it is still current",
            request.instructions,
        )
        self.assertLess(len(request.instructions), 15_000)
        predicate = request.output_schema["$defs"]["ProviderObservation"][
            "properties"
        ]["predicate"]
        self.assertEqual(predicate["enum"], ["employment.worked_for"])

    def test_non_employer_work_phrase_does_not_use_employment_route(self) -> None:
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        registry = json.loads(profile.registry_path.read_text(encoding="utf-8"))
        provider = LocalLlamaCppProvider(
            model="qwen3-14b-local-extractor",
            model_file_sha256=MODEL_SHA256,
            runtime_revision="llama.cpp-b10066-86a9c79f8",
            registry=registry,
            transport=object(),
        )
        request = provider.request(
            self.source("I work for a living, but my real interest is music.")
        )
        self.assertEqual(request.prompt_profile, "full_registry_v1")

    def test_incomplete_response_retains_sanitized_usage_diagnostics(self) -> None:
        with self.assertRaises(LocalProviderAdapterError) as caught:
            _structured_result(
                {
                    "id": "local-test",
                    "model": "qwen3-14b-local-extractor",
                    "choices": [
                        {
                            "message": {
                                "content": "{",
                                "reasoning_content": None,
                            },
                            "finish_reason": "length",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 7_000,
                        "completion_tokens": 4_000,
                    },
                }
            )
        error = caught.exception
        self.assertEqual(error.code, "local_incomplete_response")
        self.assertTrue(error.retryable)
        self.assertEqual(error.finish_reason, "length")
        self.assertEqual(error.prompt_tokens, 7_000)
        self.assertEqual(error.completion_tokens, 4_000)

    def test_malformed_general_health_belief_defers_without_health_fact(self) -> None:
        content = (
            "I see most mental illnesses being at the result of believing "
            "in concepts like hell."
        )
        result = _deterministic_policy_packet(
            self.source(content),
            registry_version=SEMANTIC_V5_2_REGISTRY_VERSION,
        )
        self.assertIsNotNone(result)
        packet, guard_code = result
        value = packet.model_dump(mode="json")
        self.assertEqual(guard_code, "ambiguous_reported_belief_transcription")
        self.assertEqual(value["entity_mentions"], [])
        self.assertEqual(value["observations"], [])
        self.assertEqual(
            [item["reason_code"] for item in value["deferrals"]],
            ["ambiguous_transcription"],
        )
        self.assertEqual(value["deferrals"][0]["memory_shape"], "none")
        self.assertEqual(value["deferrals"][0]["sensitivity"], "medium")

    def test_clear_unconventional_belief_is_attributed_as_stance(self) -> None:
        content = (
            "I believe many mental illnesses result from beliefs about hell."
        )
        result = _deterministic_policy_packet(
            self.source(content),
            registry_version=SEMANTIC_V5_2_REGISTRY_VERSION,
        )
        self.assertIsNotNone(result)
        packet, guard_code = result
        value = packet.model_dump(mode="json")
        self.assertEqual(guard_code, "explicit_reported_mental_health_stance")
        self.assertEqual(len(value["observations"]), 1)
        observation = value["observations"][0]
        self.assertEqual(observation["predicate"], "stance.reported")
        self.assertEqual(observation["modality"], "reported_belief")
        self.assertEqual(observation["projection_class"], "reported_stance")
        self.assertEqual(
            observation["surface_policy"],
            "relevant_recall_or_explicit_recall",
        )
        self.assertEqual(
            observation["object"]["value"]["topic_key"],
            "mental_health.causal_beliefs",
        )
        self.assertNotIn(
            "health.user_reported_observation",
            {item["predicate"] for item in value["observations"]},
        )

    def test_personal_health_report_is_not_routed_as_general_stance(self) -> None:
        content = "I believe I have a mental illness because of these symptoms."
        self.assertIsNone(
            _deterministic_policy_packet(
                self.source(content),
                registry_version=SEMANTIC_V5_2_REGISTRY_VERSION,
            )
        )
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        registry = json.loads(profile.registry_path.read_text(encoding="utf-8"))
        provider = LocalLlamaCppProvider(
            model="qwen3-14b-local-extractor",
            model_file_sha256=MODEL_SHA256,
            runtime_revision="llama.cpp-b10066-86a9c79f8",
            registry=registry,
            transport=object(),
        )
        request = provider.request(self.source(content))
        self.assertEqual(request.prompt_profile, "full_registry_v1")
        predicate = request.output_schema["$defs"]["ProviderObservation"][
            "properties"
        ]["predicate"]
        self.assertIn("health.user_reported_observation", predicate["enum"])

    def test_pet_name_correction_precedes_question_only_guard(self) -> None:
        content = (
            "Was it Nemo or Neko? It was Neko; the spell check changed "
            "it accidentally to Nemo."
        )
        result = _deterministic_policy_packet(
            self.source(content),
            registry_version=SEMANTIC_V5_2_REGISTRY_VERSION,
        )
        self.assertIsNotNone(result)
        packet, guard_code = result
        value = packet.model_dump(mode="json")
        self.assertEqual(guard_code, "explicit_corrected_pet_name")
        self.assertEqual(len(value["entity_mentions"]), 1)
        self.assertEqual(value["entity_mentions"][0]["entity_type"], "animal")
        self.assertEqual(value["entity_mentions"][0]["name_text"], "Neko")
        self.assertEqual(len(value["observations"]), 1)
        observation = value["observations"][0]
        self.assertEqual(observation["predicate"], "identity.name_canonical")
        self.assertEqual(observation["subject_entity_ref"], "e00")
        self.assertEqual(observation["object"]["value"], "Neko")
        self.assertEqual(observation["modality"], "corrective")
        self.assertEqual(observation["projection_class"], "correction")
        self.assertEqual(observation["surface_policy"], "normalization_only")
        self.assertEqual(
            {item["relation_type"] for item in value["comparison_hints"]},
            {"corrects", "supersedes"},
        )
        self.assertEqual(
            {
                item["target_lookup_key"]
                for item in value["comparison_hints"]
            },
            {"identity.name:nemo"},
        )

    def test_compound_named_caregiving_uses_deterministic_split_path(self) -> None:
        content = (
            "Yes, I am doing fine, but there is a weight that comes with "
            "all the loss. I have spent a lot of time caring for others "
            "including my wife, Monika after her psychotic break about "
            "five years ago. Taking care of others has been my life for "
            "a while now. I enjoy working on the app and lifting weights."
        )
        result = _deterministic_policy_packet(
            self.source(content),
            registry_version=SEMANTIC_V5_2_REGISTRY_VERSION,
        )
        self.assertIsNotNone(result)
        packet, guard_code = result
        value = packet.model_dump(mode="json")
        self.assertEqual(guard_code, "explicit_named_caregiving")
        self.assertEqual(
            {item["predicate"] for item in value["observations"]},
            {
                "relationship.caregiver_for",
                "relationship.spouse_of",
            },
        )
        entities = {
            item["entity_ref"]: item for item in value["entity_mentions"]
        }
        caregiving = next(
            item
            for item in value["observations"]
            if item["predicate"] == "relationship.caregiver_for"
        )
        self.assertEqual(
            entities[caregiving["subject_entity_ref"]]["entity_type"],
            "self",
        )
        self.assertEqual(
            entities[caregiving["object"]["entity_ref"]]["name_text"],
            "Monika",
        )
        self.assertEqual(
            {item["reason_code"] for item in value["deferrals"]},
            {"sensitive_manual_review", "compound_requires_split"},
        )
        spouse = next(
            item
            for item in value["observations"]
            if item["predicate"] == "relationship.spouse_of"
        )
        caregiver_span = caregiving["source_spans"][0]
        spouse_span = spouse["source_spans"][0]
        self.assertEqual(
            content[caregiver_span["start"] : caregiver_span["end"]],
            "caring for others including my wife, Monika",
        )
        self.assertEqual(
            content[spouse_span["start"] : spouse_span["end"]],
            "my wife, Monika",
        )
        self.assertLess(caregiver_span["end"], len(content))
        self.assertLess(spouse_span["end"], len(content))
        compound = next(
            item
            for item in value["deferrals"]
            if item["reason_code"] == "compound_requires_split"
        )
        self.assertEqual(compound["source_spans"][0]["start"], 0)
        self.assertEqual(compound["source_spans"][0]["end"], len(content))

    def test_named_caregiving_compiles_owner_to_recipient(self) -> None:
        content = (
            "I have spent much of the last year caring for others, "
            "including my wife Monika after her psychotic break."
        )
        source = self.source(content)
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        registry = json.loads(profile.registry_path.read_text(encoding="utf-8"))
        packet = ProviderPacket.model_validate(_packet())
        compiled, repairs = _compile_entity_links(source, packet, registry)
        value = compiled.model_dump(mode="json")
        entities = {
            item["entity_ref"]: item for item in value["entity_mentions"]
        }
        caregiving = [
            item
            for item in value["observations"]
            if item["predicate"] == "relationship.caregiver_for"
        ]
        self.assertEqual(len(caregiving), 1)
        observation = caregiving[0]
        self.assertEqual(
            entities[observation["subject_entity_ref"]]["entity_type"],
            "self",
        )
        self.assertEqual(
            entities[observation["object"]["entity_ref"]]["name_text"],
            "Monika",
        )
        role_parts = entities[observation["object"]["entity_ref"]][
            "relationship_role"
        ].split("|")
        self.assertEqual(len(role_parts), len(set(role_parts)))
        self.assertEqual(observation["sensitivity"], "high")
        self.assertIn(
            "explicit_relationship_observation:relationship.caregiver_for",
            repairs,
        )

    def test_former_profession_compiles_historical_occupation(self) -> None:
        content = (
            "As a professional, I was a clinical psychologist and a BCBA."
        )
        source = self.source(content)
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        registry = json.loads(profile.registry_path.read_text(encoding="utf-8"))
        packet_value = _credential_packet(source, "BCBA").model_dump(
            mode="json"
        )
        packet_value["deferrals"] = [
            {
                "reason_code": "project_scope_unresolved",
                "memory_shape": "project_knowledge",
                "source_spans": [
                    {
                        "start": 0,
                        "end": len(content),
                        "quote": content,
                    }
                ],
                "sensitivity": "medium",
            }
        ]
        packet = ProviderPacket.model_validate(packet_value)
        compiled, repairs = _compile_entity_links(source, packet, registry)
        value = compiled.model_dump(mode="json")
        entities = {
            item["entity_ref"]: item for item in value["entity_mentions"]
        }
        occupations = [
            item
            for item in value["observations"]
            if item["predicate"] == "occupation.works_as"
        ]
        self.assertEqual(len(occupations), 2)
        self.assertEqual(
            {
                entities[item["object"]["entity_ref"]]["name_text"]
                for item in occupations
            },
            {"clinical psychologist", "BCBA"},
        )
        for observation in occupations:
            self.assertEqual(
                entities[observation["subject_entity_ref"]]["entity_type"],
                "self",
            )
            self.assertEqual(
                observation["temporal"]["shape"],
                "open_interval",
            )
            self.assertIsNone(
                observation["temporal"]["instant_range"]["lower"]
            )
            self.assertIn(
                "historical_relationship_ended_before_source",
                observation["temporal"]["reason_codes"],
            )
        self.assertNotIn(
            "credential.reported",
            {item["predicate"] for item in value["observations"]},
        )
        self.assertIn("explicit_occupation_concept_entity", repairs)
        self.assertIn(
            "coordinated_former_credential_reclassified",
            repairs,
        )
        self.assertIn("orphan_project_scope_deferral_removed", repairs)
        self.assertNotIn(
            "project_scope_unresolved",
            {item.reason_code for item in compiled.deferrals},
        )

    def test_retirement_does_not_invent_current_occupation(self) -> None:
        content = (
            "I retired from psychology and applied behavior analysis and "
            "now mainly build apps."
        )
        source = self.source(content)
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        registry = json.loads(profile.registry_path.read_text(encoding="utf-8"))
        packet = ProviderPacket.model_validate(_packet())
        compiled, _ = _compile_entity_links(source, packet, registry)
        self.assertNotIn(
            "occupation.works_as",
            {item.predicate for item in compiled.observations},
        )


if __name__ == "__main__":
    unittest.main()
