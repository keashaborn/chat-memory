from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.memory_v1_v5_1_review_local_packet import (
    BUNDLE_CONTRACT,
    REVIEW_CONTRACT,
    LocalPacketReviewError,
    review_profile,
)
from scripts.memory_v1_v5_1_stage_preflight import (
    V5_1_EXTRACTION_CONTRACT,
    V5_1_REGISTRY_VERSION,
    V5_2_EXTRACTION_CONTRACT,
    V5_2_REGISTRY_VERSION,
    _validate_extraction_packet,
)
from scripts.memory_v1_predicate_runtime_profile_v2 import load_runtime_profile_v2
from scripts.memory_v1_relational_extraction_v5_provider import (
    SyntheticFixtureProvider,
    TrustedExtractionSource,
    load_registry,
    load_schema,
    sha256_text,
    validate_and_normalize,
)
from tests.test_memory_v1_semantic_coverage_v5_2 import (
    SOURCE_RECORDED_AT,
    fixture,
    load_cases,
)


ROOT = Path(__file__).resolve().parents[1]


class V52ReviewProfileTest(unittest.TestCase):
    def test_v5_1_defaults_are_unchanged(self) -> None:
        profile = review_profile("v5_1")
        self.assertEqual(profile.review_contract, REVIEW_CONTRACT)
        self.assertEqual(profile.bundle_contract, BUNDLE_CONTRACT)
        self.assertEqual(profile.extraction_contract, V5_1_EXTRACTION_CONTRACT)
        self.assertEqual(profile.registry_version, V5_1_REGISTRY_VERSION)

    def test_v5_2_profile_is_exact_and_separate(self) -> None:
        profile = review_profile("v5_2")
        self.assertEqual(profile.extraction_contract, V5_2_EXTRACTION_CONTRACT)
        self.assertEqual(profile.registry_version, V5_2_REGISTRY_VERSION)
        self.assertEqual(
            profile.resolution_contract,
            "memory_v1_entity_resolution_review_v5_2",
        )
        self.assertNotEqual(profile.review_namespace, review_profile("v5_1").review_namespace)

    def test_v5_2_packet_passes_only_v5_2_validator_binding(self) -> None:
        case = next(item for item in load_cases() if item["case_id"] == "sem-v5_2-003")
        text = case["text"]
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        packet = validate_and_normalize(
            SyntheticFixtureProvider(fixture(case)),
            source=TrustedExtractionSource.create(
                job_id="00000000-0000-4000-8000-000000000001",
                source_system="public.chat_log",
                source_external_id="00000000-0000-4000-9000-000000000001",
                source_sha256=sha256_text(text),
                source_recorded_at=SOURCE_RECORDED_AT,
                content=text,
            ),
            registry=load_registry(
                profile.registry_path, profile.registry_artifact_sha256
            ),
            schema=load_schema(
                profile.schema_path,
                profile.schema_artifact_sha256,
                expected_contract_version=profile.contract_version,
            ),
        ).normalized_packet
        _validate_extraction_packet(
            packet,
            extraction_contract=V5_2_EXTRACTION_CONTRACT,
            registry_version=V5_2_REGISTRY_VERSION,
        )
        with self.assertRaisesRegex(RuntimeError, "contract version mismatch"):
            _validate_extraction_packet(packet)

    def test_v5_2_resolution_schema_is_version_bound(self) -> None:
        schema = json.loads(
            (ROOT / "specs/memory_v1_entity_resolution_review_v5_2.schema.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(
            schema["properties"]["contract_version"]["const"],
            "memory_v1_entity_resolution_review_v5_2",
        )
        self.assertEqual(
            schema["properties"]["predicate_registry_version"]["const"],
            V5_2_REGISTRY_VERSION,
        )

    def test_unknown_profile_fails_closed(self) -> None:
        with self.assertRaisesRegex(LocalPacketReviewError, "not allowlisted"):
            review_profile("latest")


if __name__ == "__main__":
    unittest.main()
