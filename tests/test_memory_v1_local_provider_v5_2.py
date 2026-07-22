from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from scripts.memory_v1_predicate_runtime_profile_v2 import load_runtime_profile_v2
from scripts.memory_v1_relational_extraction_v5_local_provider import (
    LocalLlamaCppProvider,
    SEMANTIC_V5_2_POLICY_COMPILER_VERSION,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    TrustedExtractionSource,
)


ROOT = Path(__file__).resolve().parents[1]
MODEL_SHA256 = "5" * 64


class LocalProviderV52Test(unittest.TestCase):
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
        self.assertIn("RELATIONSHIP_V5_1_RULES", request.instructions)
        self.assertIn("SEMANTIC_V5_2_RULES", request.instructions)
        self.assertIn("stance.reported", request.instructions)
        predicate = request.output_schema["$defs"]["ProviderObservation"][
            "properties"
        ]["predicate"]
        self.assertIn("stance.reported", predicate["enum"])
        self.assertIn("education.attended", predicate["enum"])
        self.assertIn("employment.worked_for", predicate["enum"])
        self.assertEqual(
            provider._policy_compiler_version,
            SEMANTIC_V5_2_POLICY_COMPILER_VERSION,
        )


if __name__ == "__main__":
    unittest.main()
