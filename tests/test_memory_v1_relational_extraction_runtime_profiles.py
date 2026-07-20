from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.memory_v1_predicate_runtime_profile import load_runtime_profile
from scripts.memory_v1_relational_extraction_v5_provider import (
    SyntheticFixtureProvider,
    TrustedExtractionSource,
    load_registry,
    load_schema,
    sha256_text,
    validate_and_normalize,
)


V5_GOLDEN_PACKET_SHA256 = (
    "58902e1d5678cd3876ce0ab551041bc34a1773c9a08ab5b4332e66db4bfe2cbb"
)


def source_and_provider() -> tuple[TrustedExtractionSource, SyntheticFixtureProvider]:
    content = "What is my name?"
    source = TrustedExtractionSource.create(
        job_id="11111111-1111-4111-8111-111111111111",
        source_system="public.chat_log",
        source_external_id="22222222-2222-4222-8222-222222222222",
        source_sha256=sha256_text(content),
        source_recorded_at="2026-07-20T12:00:00Z",
        content=content,
    )
    provider = SyntheticFixtureProvider(
        {
            "entity_mentions": [],
            "observations": [],
            "comparison_hints": [],
            "deferrals": [
                {
                    "reason_code": "question_only",
                    "memory_shape": "none",
                    "source_spans": [
                        {"start": 0, "end": len(content), "quote": content}
                    ],
                    "sensitivity": "low",
                }
            ],
            "packet_findings": ["question_only"],
        }
    )
    return source, provider


def validate_profile(name: str):
    profile = load_runtime_profile(ROOT, name)
    registry = load_registry(
        profile.registry_path,
        profile.registry_artifact_sha256,
    )
    schema = load_schema(
        profile.schema_path,
        profile.schema_artifact_sha256,
        expected_contract_version=profile.contract_version,
    )
    source, provider = source_and_provider()
    return validate_and_normalize(
        provider,
        source=source,
        registry=registry,
        schema=schema,
    )


class RelationalExtractionRuntimeProfilesTest(unittest.TestCase):
    def test_v5_replay_hash_is_unchanged(self) -> None:
        result = validate_profile("v5")
        self.assertEqual(result.normalized_packet_sha256, V5_GOLDEN_PACKET_SHA256)
        self.assertEqual(
            result.normalized_packet["contract_version"],
            "memory_v1_relational_extraction_v5",
        )
        self.assertEqual(
            result.normalized_packet["predicate_registry_version"],
            "memory_predicate_registry_v5",
        )

    def test_v5_1_emits_exact_new_contract_pair(self) -> None:
        result = validate_profile("v5_1")
        self.assertEqual(
            result.normalized_packet["contract_version"],
            "memory_v1_relational_extraction_v5_1",
        )
        self.assertEqual(
            result.normalized_packet["predicate_registry_version"],
            "memory_predicate_registry_v5_1",
        )
        self.assertNotEqual(result.normalized_packet_sha256, V5_GOLDEN_PACKET_SHA256)

    def test_cross_profile_schema_is_rejected(self) -> None:
        v5 = load_runtime_profile(ROOT, "v5")
        v5_1 = load_runtime_profile(ROOT, "v5_1")
        registry = load_registry(
            v5_1.registry_path,
            v5_1.registry_artifact_sha256,
        )
        schema = load_schema(
            v5.schema_path,
            v5.schema_artifact_sha256,
        )
        source, provider = source_and_provider()
        with self.assertRaisesRegex(ValueError, "schema/registry contract mismatch"):
            validate_and_normalize(
                provider,
                source=source,
                registry=registry,
                schema=schema,
            )


if __name__ == "__main__":
    unittest.main()

