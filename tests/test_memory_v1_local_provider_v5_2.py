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
    _deterministic_policy_packet,
    _structured_result,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
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
        self.assertEqual(
            provider._policy_compiler_version,
            SEMANTIC_V5_2_POLICY_COMPILER_VERSION,
        )

    def test_non_stance_v5_2_source_retains_full_registry_profile(self) -> None:
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
        self.assertEqual(request.prompt_profile, "full_registry_v1")
        predicate = request.output_schema["$defs"]["ProviderObservation"][
            "properties"
        ]["predicate"]
        self.assertIn("education.attended", predicate["enum"])
        self.assertIn("employment.worked_for", predicate["enum"])

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


if __name__ == "__main__":
    unittest.main()
