from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.memory_v1_predicate_runtime_profile_v2 import load_runtime_profile_v2
from scripts.memory_v1_relational_extraction_v5_local_provider import (
    LocalLlamaCppProvider,
    LocalProviderAdapterError,
    SEMANTIC_V5_2_REGISTRY_VERSION,
    SEMANTIC_V5_2_POLICY_COMPILER_VERSION,
    _HISTORICAL_PET_RELATIONSHIP_RE,
    _compile_entity_links,
    _credential_packet,
    _deterministic_policy_packet,
    _example_entity,
    _example_observation,
    _explicit_pet_breed_and_species,
    _literal,
    _packet,
    _pet_name,
    _structured_result,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    ProviderPacket,
    TrustedExtractionSource,
    validate_and_normalize,
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
        self.assertEqual(
            request.output_schema["$defs"]["ProviderTemporal"]["properties"][
                "anchored_to_source_time"
            ],
            {"const": False, "type": "boolean"},
        )
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
        self.assertEqual(
            request.output_schema["properties"]["observations"]["minItems"],
            1,
        )

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
                "pet.species",
                "relationship.has_pet",
            ],
        )
        self.assertIn(
            "never return an empty packet",
            request.instructions,
        )
        self.assertEqual(
            request.output_schema["properties"]["observations"]["minItems"],
            6,
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
            ["pet.species", "relationship.has_pet"],
        )
        self.assertEqual(
            request.output_schema["properties"]["observations"]["minItems"],
            2,
        )
        self.assertIn(
            "does not by itself prove death",
            request.instructions,
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
        self.assertEqual(
            request.output_schema["properties"]["observations"]["minItems"],
            2,
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

    def test_historical_pet_name_patterns_preserve_names_and_taxonomy(
        self,
    ) -> None:
        cases = (
            (
                "After I got my doctorate, I got a German Shepherd "
                "by the name of Max.",
                "Max",
                ("German Shepherd", "dog"),
            ),
            (
                "My next German Shepherd was Keasha von Steffen Haus.",
                "Keasha von Steffen Haus",
                ("German Shepherd", "dog"),
            ),
            (
                "Neko what was my first cat she slept on my chest.",
                "Neko",
                (None, "cat"),
            ),
        )
        for content, name, taxonomy in cases:
            with self.subTest(content=content):
                self.assertEqual(_pet_name(content), name)
                self.assertEqual(
                    _explicit_pet_breed_and_species(content),
                    taxonomy,
                )
                self.assertIsNotNone(
                    _HISTORICAL_PET_RELATIONSHIP_RE.search(content)
                )
        self.assertIsNone(
            _HISTORICAL_PET_RELATIONSHIP_RE.search(
                "I got a German Shepherd named Koda last week."
            )
        )

    def test_pet_name_breed_species_and_history_are_canonicalized(
        self,
    ) -> None:
        content = (
            "After I got my doctorate, I got a German Shepherd "
            "by the name of Max."
        )
        source = self.source(content)
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        registry = json.loads(profile.registry_path.read_text(encoding="utf-8"))
        self_entity = _example_entity(
            content,
            entity_ref="e00",
            entity_type="self",
            mention_kind="self_reference",
            name_text=None,
            relationship_role="user:self",
            reason_code="explicit_self_reference",
        )
        animal_entity = _example_entity(
            content,
            entity_ref="e01",
            entity_type="animal",
            mention_kind="role_only",
            name_text=None,
            relationship_role="pet:reported",
            reason_code="reported_pet",
        )
        relationship = _example_observation(
            content,
            observation_ref="o00",
            subject_entity_ref="e00",
            predicate="relationship.has_pet",
            object_value={"kind": "entity", "entity_ref": "e01"},
            projection_class="direct_claim",
            surface_policy="direct_or_relevant",
            sensitivity="low",
            reason_code="reported_pet_relationship",
            temporal_semantic="state_validity",
        )
        wrong_breed = _example_observation(
            content,
            observation_ref="o01",
            subject_entity_ref="e01",
            predicate="pet.breed",
            object_value=_literal("text", "Max"),
            projection_class="direct_claim",
            surface_policy="direct_or_relevant",
            sensitivity="low",
            reason_code="reported_pet_breed",
        )
        wrong_species = _example_observation(
            content,
            observation_ref="o02",
            subject_entity_ref="e01",
            predicate="pet.species",
            object_value=_literal("text", "German Shepherd"),
            projection_class="direct_claim",
            surface_policy="direct_or_relevant",
            sensitivity="low",
            reason_code="reported_pet_species",
        )
        duplicate_species = {
            **wrong_species,
            "observation_ref": "o03",
            "reason_codes": ["second_model_guess"],
            "extraction_confidence": 0.71,
        }
        packet = ProviderPacket.model_validate(
            _packet(
                entities=[self_entity, animal_entity],
                observations=[
                    relationship,
                    wrong_breed,
                    wrong_species,
                    duplicate_species,
                ],
            )
        )
        compiled, repairs = _compile_entity_links(
            source,
            packet,
            registry,
        )
        value = compiled.model_dump(mode="json")
        animal = next(
            item
            for item in value["entity_mentions"]
            if item["entity_type"] == "animal"
        )
        self.assertEqual(animal["name_text"], "Max")
        self.assertEqual(animal["mention_kind"], "named")
        by_predicate = {}
        for observation in value["observations"]:
            by_predicate.setdefault(
                observation["predicate"],
                [],
            ).append(observation)
        self.assertEqual(
            by_predicate["identity.name"][0]["object"]["value"],
            "Max",
        )
        self.assertEqual(
            by_predicate["pet.breed"][0]["object"]["value"],
            "German Shepherd",
        )
        self.assertEqual(len(by_predicate["pet.species"]), 1)
        self.assertEqual(
            by_predicate["pet.species"][0]["object"]["value"],
            "dog",
        )
        self.assertIn(
            "historical_pet_relationship_from_past_acquisition",
            by_predicate["relationship.has_pet"][0]["reason_codes"],
        )
        self.assertNotIn(
            "trusted_source_time_upper_bound",
            by_predicate["relationship.has_pet"][0]["temporal"][
                "reason_codes"
            ],
        )
        self.assertIn(
            "duplicate_semantic_observations_removed",
            repairs,
        )

    def test_ambiguous_breeding_transcript_defers_before_stance_route(
        self,
    ) -> None:
        content = (
            "I think I've read German Shepherd's five times and then "
            "I eventually bread Helsing and Dahlia."
        )
        result = _deterministic_policy_packet(
            self.source(content),
            registry_version=SEMANTIC_V5_2_REGISTRY_VERSION,
        )
        self.assertIsNotNone(result)
        packet, guard_code = result
        value = packet.model_dump(mode="json")
        self.assertEqual(
            guard_code,
            "ambiguous_breeding_transcription",
        )
        self.assertEqual(value["observations"], [])
        self.assertEqual(
            [item["reason_code"] for item in value["deferrals"]],
            ["ambiguous_transcription"],
        )

    def test_contextual_ambiguous_breeding_transcript_defers_without_model(
        self,
    ) -> None:
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        registry = json.loads(
            profile.registry_path.read_text(encoding="utf-8")
        )
        provider = LocalLlamaCppProvider(
            model="qwen3-14b-local-extractor",
            model_file_sha256=MODEL_SHA256,
            runtime_revision="llama.cpp-b10066-86a9c79f8",
            registry=registry,
            transport=SimpleNamespace(
                local_model_calls=0,
                external_model_calls=0,
            ),
        )
        source = self.source(
            "I think I've read German Shepherd's five times and then "
            "I eventually bread Helsing and Dahlia."
        )
        packet = provider.extract(
            source,
            evidence_context=object(),
        )
        value = packet.model_dump(mode="json")
        self.assertEqual(value["observations"], [])
        self.assertEqual(
            [item["reason_code"] for item in value["deferrals"]],
            ["ambiguous_transcription"],
        )
        self.assertEqual(
            provider.last_audit["policy_guard_code"],
            "ambiguous_breeding_transcription",
        )
        self.assertEqual(provider.local_model_calls, 0)
        self.assertEqual(provider.external_model_calls, 0)

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

    def test_context_pet_name_is_removed_and_ownership_is_rewired(self) -> None:
        content = (
            "My male German shepherd had a rare blood cancer and died "
            "a few months later."
        )
        source = self.source(content)
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        registry = json.loads(profile.registry_path.read_text(encoding="utf-8"))
        span = {"start": 0, "end": len(content), "quote": content}
        temporal = {
            "anchored_to_source_time": False,
            "basis": "none",
            "calendar_range": None,
            "certainty": "unknown",
            "instant": None,
            "instant_range": None,
            "precision": "unknown",
            "reason_codes": ["implicit_source_time"],
            "recurrence": None,
            "relative_offset": None,
            "semantic": "state_validity",
            "shape": "none",
            "source_form": "implicit_source_time",
        }
        packet = ProviderPacket.model_validate(
            _packet(
                entities=[
                    {
                        "entity_ref": "e00",
                        "entity_type": "animal",
                        "mention_kind": "named",
                        "name_text": "Dahlia",
                        "relationship_role": "animal:individual",
                        "source_spans": [span],
                        "extraction_confidence": 0.99,
                        "reason_codes": ["explicit_pet_name"],
                    }
                ],
                observations=[
                    {
                        "extraction_confidence": 0.99,
                        "modality": "asserted",
                        "object": {
                            "approximate": False,
                            "datatype": "boolean",
                            "kind": "literal",
                            "unit": None,
                            "value": True,
                        },
                        "observation_ref": "o00",
                        "polarity": "affirmed",
                        "predicate": "relationship.has_pet",
                        "projection_class": "direct_claim",
                        "reason_codes": ["explicit_pet_relationship"],
                        "sensitivity": "low",
                        "source_spans": [span],
                        "subject_entity_ref": "e00",
                        "surface_policy": "direct_or_relevant",
                        "temporal": temporal,
                    }
                ],
            )
        )
        compiled, repairs = _compile_entity_links(source, packet, registry)
        value = compiled.model_dump(mode="json")
        entities = {
            item["entity_ref"]: item for item in value["entity_mentions"]
        }
        animals = [
            item for item in entities.values()
            if item["entity_type"] == "animal"
        ]
        self.assertEqual(len(animals), 1)
        self.assertIsNone(animals[0]["name_text"])
        self.assertEqual(animals[0]["mention_kind"], "role_only")
        self.assertIn(
            "target_role_only_pet_reference",
            animals[0]["reason_codes"],
        )
        ownership = [
            item for item in value["observations"]
            if item["predicate"] == "relationship.has_pet"
        ]
        self.assertEqual(len(ownership), 1)
        observation = ownership[0]
        self.assertEqual(
            entities[observation["subject_entity_ref"]]["entity_type"],
            "self",
        )
        self.assertEqual(
            entities[observation["object"]["entity_ref"]]["entity_type"],
            "animal",
        )
        self.assertNotIn(
            "unregistered_predicate",
            {item["reason_code"] for item in value["deferrals"]},
        )
        self.assertIn("context_only_pet_name_removed", repairs)
        self.assertIn("pet_relation_normalized", repairs)

    def test_explicit_education_is_completed_deterministically(self) -> None:
        content = (
            "Then I went to the forest Institute of professional psychology "
            "in Springfield, Missouri."
        )
        source = self.source(content)
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        registry = json.loads(profile.registry_path.read_text(encoding="utf-8"))
        compiled, repairs = _compile_entity_links(
            source,
            ProviderPacket.model_validate(_packet()),
            registry,
        )
        value = compiled.model_dump(mode="json")
        entities = {
            item["entity_ref"]: item for item in value["entity_mentions"]
        }
        education = [
            item
            for item in value["observations"]
            if item["predicate"] == "education.attended"
        ]
        self.assertEqual(len(education), 1)
        observation = education[0]
        self.assertEqual(
            entities[observation["subject_entity_ref"]]["entity_type"],
            "self",
        )
        organization = entities[observation["object"]["entity_ref"]]
        self.assertEqual(organization["entity_type"], "organization")
        self.assertEqual(
            organization["name_text"],
            "forest Institute of professional psychology",
        )
        self.assertEqual(
            observation["source_spans"][0]["quote"],
            "forest Institute of professional psychology",
        )
        self.assertIn(
            "historical_relationship_ended_before_source",
            observation["temporal"]["reason_codes"],
        )
        self.assertFalse(
            observation["temporal"]["anchored_to_source_time"]
        )
        self.assertIsNone(
            observation["temporal"]["instant_range"]["upper"]
        )
        self.assertNotIn(
            "trusted_source_time_upper_bound",
            observation["temporal"]["reason_codes"],
        )
        self.assertIn(
            "explicit_education_observation_completed",
            repairs,
        )

    def test_unresolved_caregiving_pronoun_defers_without_self_health(self) -> None:
        content = (
            "I cared for her for many years she had a lot of skin and "
            "allergy issues."
        )
        source = self.source(content)
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        registry = json.loads(profile.registry_path.read_text(encoding="utf-8"))
        self_entity = _example_entity(
            content,
            entity_ref="e00",
            entity_type="self",
            mention_kind="self_reference",
            name_text=None,
            relationship_role="user:self",
            reason_code="explicit_self_reference",
        )
        unresolved_person = _example_entity(
            content,
            entity_ref="e01",
            entity_type="person",
            mention_kind="role_only",
            name_text=None,
            relationship_role="relationship:care_recipient",
            reason_code="unresolved_pronoun",
        )
        caregiving = _example_observation(
            content,
            observation_ref="o00",
            subject_entity_ref="e00",
            predicate="relationship.caregiver_for",
            object_value={"kind": "entity", "entity_ref": "e01"},
            projection_class="direct_claim",
            surface_policy="explicit_recall_only",
            sensitivity="high",
            reason_code="reported_caregiving",
            temporal_semantic="state_validity",
        )
        health = _example_observation(
            content,
            observation_ref="o01",
            subject_entity_ref="e00",
            predicate="health.user_reported_observation",
            object_value=_literal("text", "skin and allergy issues"),
            projection_class="supportive_context",
            surface_policy="mention_when_directly_relevant",
            sensitivity="high",
            reason_code="reported_health_detail",
            modality="reported_observation",
        )
        compiled, repairs = _compile_entity_links(
            source,
            ProviderPacket.model_validate(
                _packet(
                    entities=[self_entity, unresolved_person],
                    observations=[caregiving, health],
                )
            ),
            registry,
        )
        value = compiled.model_dump(mode="json")
        self.assertEqual(value["observations"], [])
        self.assertEqual(value["entity_mentions"], [])
        self.assertEqual(
            {item["reason_code"] for item in value["deferrals"]},
            {"context_missing"},
        )
        self.assertIn(
            "unresolved_caregiving_pronoun_deferred",
            repairs,
        )

    def test_pet_loss_ends_relationship_without_inventing_death(self) -> None:
        content = "About two years ago I lost a cat I loved a lot."
        source = self.source(content)
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        registry = json.loads(profile.registry_path.read_text(encoding="utf-8"))
        self_entity = _example_entity(
            content,
            entity_ref="e00",
            entity_type="self",
            mention_kind="self_reference",
            name_text=None,
            relationship_role="user:self",
            reason_code="explicit_self_reference",
        )
        animal = _example_entity(
            content,
            entity_ref="e01",
            entity_type="animal",
            mention_kind="role_only",
            name_text=None,
            relationship_role="pet:reported",
            reason_code="reported_pet",
        )
        ownership = _example_observation(
            content,
            observation_ref="o00",
            subject_entity_ref="e00",
            predicate="relationship.has_pet",
            object_value={"kind": "entity", "entity_ref": "e01"},
            projection_class="direct_claim",
            surface_policy="direct_or_relevant",
            sensitivity="low",
            reason_code="reported_pet_relationship",
            temporal_semantic="state_validity",
        )
        invented_death = _example_observation(
            content,
            observation_ref="o01",
            subject_entity_ref="e01",
            predicate="life_event.died",
            object_value=_literal("boolean", True),
            projection_class="direct_claim",
            surface_policy="mention_when_directly_relevant",
            sensitivity="high",
            reason_code="inferred_from_loss",
            temporal_semantic="occurrence",
        )
        compiled, repairs = _compile_entity_links(
            source,
            ProviderPacket.model_validate(
                _packet(
                    entities=[self_entity, animal],
                    observations=[ownership, invented_death],
                )
            ),
            registry,
        )
        value = compiled.model_dump(mode="json")
        self.assertEqual(
            {item["predicate"] for item in value["observations"]},
            {"pet.species", "relationship.has_pet"},
        )
        ownership = next(
            item
            for item in value["observations"]
            if item["predicate"] == "relationship.has_pet"
        )
        species = next(
            item
            for item in value["observations"]
            if item["predicate"] == "pet.species"
        )
        self.assertEqual(species["object"]["value"], "cat")
        self.assertIn(
            "historical_relationship_ended_before_source",
            ownership["temporal"]["reason_codes"],
        )
        self.assertFalse(
            ownership["temporal"]["anchored_to_source_time"]
        )
        self.assertIsNone(
            ownership["temporal"]["instant_range"]["upper"]
        )
        self.assertNotIn(
            "trusted_source_time_upper_bound",
            ownership["temporal"]["reason_codes"],
        )
        self.assertIn("pet_loss_not_promoted_to_death", repairs)
        self.assertIn(
            "pet_relationship_historical_end_normalized",
            repairs,
        )

    def test_pet_death_closes_ownership_and_historical_health(self) -> None:
        content = (
            "My male German shepherd had a rare blood cancer and died "
            "a few months later."
        )
        source = self.source(content)
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        registry = json.loads(profile.registry_path.read_text(encoding="utf-8"))
        self_entity = _example_entity(
            content,
            entity_ref="e00",
            entity_type="self",
            mention_kind="self_reference",
            name_text=None,
            relationship_role="user:self",
            reason_code="explicit_self_reference",
        )
        animal = _example_entity(
            content,
            entity_ref="e01",
            entity_type="animal",
            mention_kind="role_only",
            name_text=None,
            relationship_role="pet:reported",
            reason_code="reported_pet",
        )
        ownership = _example_observation(
            content,
            observation_ref="o00",
            subject_entity_ref="e00",
            predicate="relationship.has_pet",
            object_value={"kind": "entity", "entity_ref": "e01"},
            projection_class="direct_claim",
            surface_policy="direct_or_relevant",
            sensitivity="low",
            reason_code="reported_pet_relationship",
            temporal_semantic="state_validity",
        )
        health = _example_observation(
            content,
            observation_ref="o01",
            subject_entity_ref="e00",
            predicate="health.user_reported_observation",
            object_value=_literal("text", "rare blood cancer"),
            projection_class="supportive_context",
            surface_policy="mention_when_directly_relevant",
            sensitivity="high",
            reason_code="reported_pet_health",
            modality="reported_observation",
        )
        death = _example_observation(
            content,
            observation_ref="o02",
            subject_entity_ref="e01",
            predicate="life_event.died",
            object_value=_literal("boolean", True),
            projection_class="direct_claim",
            surface_policy="mention_when_directly_relevant",
            sensitivity="high",
            reason_code="explicit_pet_death",
            temporal_semantic="occurrence",
        )
        compiled, repairs = _compile_entity_links(
            source,
            ProviderPacket.model_validate(
                _packet(
                    entities=[self_entity, animal],
                    observations=[ownership, health, death],
                )
            ),
            registry,
        )
        value = compiled.model_dump(mode="json")
        entities = {
            item["entity_ref"]: item for item in value["entity_mentions"]
        }
        by_predicate = {
            item["predicate"]: item for item in value["observations"]
        }
        self.assertEqual(
            entities[
                by_predicate["health.user_reported_observation"][
                    "subject_entity_ref"
                ]
            ]["entity_type"],
            "animal",
        )
        self.assertIn(
            "historical_relationship_ended_before_source",
            by_predicate["relationship.has_pet"]["temporal"][
                "reason_codes"
            ],
        )
        self.assertIn(
            "historical_relationship_ended_before_source",
            by_predicate["health.user_reported_observation"]["temporal"][
                "reason_codes"
            ],
        )
        for predicate in (
            "relationship.has_pet",
            "health.user_reported_observation",
        ):
            temporal_value = by_predicate[predicate]["temporal"]
            self.assertFalse(
                temporal_value["anchored_to_source_time"]
            )
            self.assertIsNone(
                temporal_value["instant_range"]["upper"]
            )
            self.assertNotIn(
                "trusted_source_time_upper_bound",
                temporal_value["reason_codes"],
            )
        self.assertEqual(
            by_predicate["life_event.died"]["temporal"]["semantic"],
            "occurrence",
        )
        self.assertEqual(
            by_predicate["life_event.died"]["temporal"]["source_form"],
            "none",
        )
        self.assertIn(
            "pet_death_undated_occurrence_normalized",
            repairs,
        )

    def test_context_cannot_originate_death_without_target_death_cue(
        self,
    ) -> None:
        content = (
            "Her name was Keasha von Steffen Haus; she was from an "
            "excellent breeder in Wisconsin."
        )
        source = self.source(content)
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        registry = json.loads(profile.registry_path.read_text(encoding="utf-8"))
        animal = _example_entity(
            content,
            entity_ref="e00",
            entity_type="animal",
            mention_kind="named",
            name_text="Keasha von Steffen Haus",
            relationship_role="pet:deceased",
            reason_code="context_pet_name",
        )
        invented_death = _example_observation(
            content,
            observation_ref="o00",
            subject_entity_ref="e00",
            predicate="life_event.died",
            object_value=_literal("boolean", True),
            projection_class="direct_claim",
            surface_policy="mention_when_directly_relevant",
            sensitivity="high",
            reason_code="context_only_death",
            temporal_semantic="occurrence",
        )
        compiled, repairs = _compile_entity_links(
            source,
            ProviderPacket.model_validate(
                _packet(
                    entities=[animal],
                    observations=[invented_death],
                )
            ),
            registry,
        )
        value = compiled.model_dump(mode="json")
        self.assertEqual(
            [item["predicate"] for item in value["observations"]],
            ["identity.name"],
        )
        self.assertEqual(
            value["observations"][0]["object"]["value"],
            "Keasha von Steffen Haus",
        )
        self.assertEqual(len(value["entity_mentions"]), 1)
        self.assertEqual(
            value["entity_mentions"][0]["relationship_role"],
            "pet:reported",
        )
        self.assertIn("death_requires_explicit_target_cue", repairs)
        self.assertIn(
            "source_supported_pet_identity_preserved",
            repairs,
        )
        self.assertIn(
            "unsupported_pet_deceased_role_normalized",
            repairs,
        )

    def test_named_death_subject_drops_descriptive_prefix(self) -> None:
        content = (
            "And my amazing Tiekerhook male Helsing also died last year."
        )
        source = self.source(content)
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        registry = json.loads(profile.registry_path.read_text(encoding="utf-8"))
        animal = _example_entity(
            content,
            entity_ref="e00",
            entity_type="animal",
            mention_kind="named",
            name_text="Tiekerhook male Helsing",
            relationship_role="pet:deceased",
            reason_code="explicit_reported_death",
        )
        death = _example_observation(
            content,
            observation_ref="o00",
            subject_entity_ref="e00",
            predicate="life_event.died",
            object_value=_literal("boolean", True),
            projection_class="direct_claim",
            surface_policy="mention_when_directly_relevant",
            sensitivity="high",
            reason_code="explicit_reported_death",
            temporal_semantic="occurrence",
        )
        compiled, repairs = _compile_entity_links(
            source,
            ProviderPacket.model_validate(
                _packet(
                    entities=[animal],
                    observations=[death],
                )
            ),
            registry,
        )
        value = compiled.model_dump(mode="json")
        self.assertEqual(
            value["entity_mentions"][0]["name_text"],
            "Helsing",
        )
        temporal = value["observations"][0]["temporal"]
        self.assertFalse(temporal["anchored_to_source_time"])
        self.assertEqual(temporal["basis"], "relative")
        self.assertEqual(temporal["shape"], "instant")
        self.assertEqual(temporal["precision"], "year")
        self.assertEqual(
            temporal["relative_offset"],
            {
                "direction": "past",
                "magnitude": 1.0,
                "unit": "year",
                "approximate": True,
                "anchor_source": "evidence_observed_at",
            },
        )
        self.assertIn(
            "explicit_death_subject_name_canonicalized",
            repairs,
        )
        self.assertIn("explicit_death_time_canonicalized", repairs)

        provider = SimpleNamespace(
            provider_id="local_llama_cpp",
            provider_version="v1",
            external_call_capability=False,
            external_model_calls=0,
            extract=lambda _source: compiled,
        )
        validated = validate_and_normalize(
            provider,
            source=source,
            registry=registry,
            schema=json.loads(
                profile.schema_path.read_text(encoding="utf-8")
            ),
            allowed_provider_versions={"local_llama_cpp": "v1"},
            max_external_model_calls=0,
        )
        trusted_temporal = validated.normalized_packet["observations"][0][
            "temporal"
        ]
        self.assertFalse(trusted_temporal["anchored_to_source_time"])
        self.assertEqual(trusted_temporal["basis"], "relative")
        self.assertEqual(
            trusted_temporal["relative_offset"]["anchor_source"],
            "evidence_observed_at",
        )

    def test_named_pet_loss_does_not_prove_death(self) -> None:
        content = "That was Dahlia who I lost last year."
        source = self.source(content)
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        registry = json.loads(profile.registry_path.read_text(encoding="utf-8"))
        animal = _example_entity(
            content,
            entity_ref="e00",
            entity_type="animal",
            mention_kind="named",
            name_text="Dahlia",
            relationship_role="pet:reported",
            reason_code="explicit_pet_name",
        )
        invented_death = _example_observation(
            content,
            observation_ref="o00",
            subject_entity_ref="e00",
            predicate="life_event.died",
            object_value=_literal("boolean", True),
            projection_class="direct_claim",
            surface_policy="mention_when_directly_relevant",
            sensitivity="high",
            reason_code="inferred_from_loss",
            temporal_semantic="occurrence",
        )
        compiled, repairs = _compile_entity_links(
            source,
            ProviderPacket.model_validate(
                _packet(
                    entities=[animal],
                    observations=[invented_death],
                    deferrals=[
                        {
                            "reason_code": "sensitive_manual_review",
                            "memory_shape": "direct_claim",
                            "source_spans": [
                                {
                                    "start": 0,
                                    "end": len(content),
                                    "quote": content,
                                }
                            ],
                            "sensitivity": "high",
                        },
                        {
                            "reason_code": "sensitive_manual_review",
                            "memory_shape": "direct_claim",
                            "source_spans": [
                                {
                                    "start": 0,
                                    "end": len(content),
                                    "quote": content,
                                }
                            ],
                            "sensitivity": "high",
                        },
                    ],
                )
            ),
            registry,
        )
        value = compiled.model_dump(mode="json")
        self.assertNotIn(
            "life_event.died",
            {item["predicate"] for item in value["observations"]},
        )
        self.assertEqual(
            [item["predicate"] for item in value["observations"]],
            ["identity.name"],
        )
        self.assertEqual(
            value["observations"][0]["object"]["value"],
            "Dahlia",
        )
        self.assertEqual(
            value["entity_mentions"][0]["relationship_role"],
            "pet:reported",
        )
        self.assertEqual(
            {item["reason_code"] for item in value["deferrals"]},
            {"sensitive_manual_review"},
        )
        self.assertEqual(len(value["deferrals"]), 1)
        self.assertIn("pet_loss_not_promoted_to_death", repairs)
        self.assertIn("duplicate_semantic_deferrals_removed", repairs)
        self.assertIn(
            "source_supported_pet_identity_preserved",
            repairs,
        )

    def test_third_person_occupation_never_becomes_self_occupation(self) -> None:
        content = "I talked to Bob Fry who was the president at the time."
        source = self.source(content)
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        registry = json.loads(profile.registry_path.read_text(encoding="utf-8"))
        person = _example_entity(
            content,
            entity_ref="e00",
            entity_type="person",
            mention_kind="named",
            name_text="Bob Fry",
            relationship_role="person:reported",
            reason_code="explicit_named_person",
        )
        role = _example_entity(
            content,
            entity_ref="e01",
            entity_type="concept",
            mention_kind="named",
            name_text="president",
            relationship_role="occupation:reported",
            reason_code="explicit_occupation",
        )
        occupation = _example_observation(
            content,
            observation_ref="o00",
            subject_entity_ref="e00",
            predicate="occupation.works_as",
            object_value={"kind": "entity", "entity_ref": "e01"},
            projection_class="direct_claim",
            surface_policy="direct_or_relevant",
            sensitivity="medium",
            reason_code="third_person_historical_occupation",
            temporal_semantic="state_validity",
        )
        compiled, _ = _compile_entity_links(
            source,
            ProviderPacket.model_validate(
                _packet(
                    entities=[person, role],
                    observations=[occupation],
                )
            ),
            registry,
        )
        value = compiled.model_dump(mode="json")
        entities = {
            item["entity_ref"]: item for item in value["entity_mentions"]
        }
        observation = value["observations"][0]
        self.assertEqual(
            entities[observation["subject_entity_ref"]]["name_text"],
            "Bob Fry",
        )
        self.assertNotIn(
            "self",
            {item["entity_type"] for item in value["entity_mentions"]},
        )
        self.assertFalse(
            observation["temporal"]["anchored_to_source_time"]
        )
        self.assertIsNone(
            observation["temporal"]["instant_range"]["upper"]
        )
        self.assertNotIn(
            "trusted_source_time_upper_bound",
            observation["temporal"]["reason_codes"],
        )


if __name__ == "__main__":
    unittest.main()
