from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from scripts.memory_v1_predicate_runtime_profile_v2 import (
    load_runtime_profile_v2,
)
from scripts.memory_v1_relational_extraction_v5_local_provider import (
    LocalLlamaCppProvider,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    TrustedExtractionSource,
    _normalize_temporal,
    validate_and_normalize,
)


ROOT = Path(__file__).resolve().parents[1]


class ZeroCallTransport:
    external_model_calls = 0
    local_model_calls = 0


class EmploymentNormalizationTest(unittest.TestCase):
    def test_started_employment_does_not_assert_currentness_or_dates(self) -> None:
        content = (
            "I started working for the wisconsin early autism project."
        )
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        registry = json.loads(profile.registry_path.read_text(encoding="utf-8"))
        schema = json.loads(profile.schema_path.read_text(encoding="utf-8"))
        source = TrustedExtractionSource.create(
            job_id="00000000-0000-4000-8000-000000000001",
            source_system="public.chat_log",
            source_external_id="00000000-0000-4000-8000-000000000002",
            source_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            source_recorded_at="2026-07-21T12:00:00+00:00",
            content=content,
        )
        provider = LocalLlamaCppProvider(
            model="qwen3-14b-local-extractor",
            model_file_sha256="5" * 64,
            runtime_revision="zero-call-test",
            registry=registry,
            transport=ZeroCallTransport(),
        )
        validated = validate_and_normalize(
            provider,
            source=source,
            registry=registry,
            schema=schema,
            allowed_provider_versions={"local_llama_cpp": "v1"},
            max_external_model_calls=0,
        )
        self.assertEqual(provider.local_model_calls, 0)
        self.assertEqual(
            provider.last_audit["policy_guard_code"],
            "explicit_started_employment",
        )
        packet = validated.normalized_packet
        self.assertEqual(len(packet["entity_mentions"]), 2)
        employer = packet["entity_mentions"][1]
        self.assertEqual(employer["entity_type"], "organization")
        self.assertEqual(
            employer["name_text"].casefold(),
            "Wisconsin Early Autism Project".casefold(),
        )
        self.assertEqual(len(packet["observations"]), 1)
        observation = packet["observations"][0]
        self.assertEqual(observation["predicate"], "employment.worked_for")
        self.assertEqual(observation["subject_entity_ref"], "e00")
        self.assertEqual(
            observation["object"],
            {"kind": "entity", "entity_ref": "e01"},
        )
        temporal = observation["temporal"]
        self.assertEqual(temporal["semantic"], "state_validity")
        self.assertEqual(temporal["shape"], "none")
        self.assertEqual(temporal["basis"], "none")
        self.assertEqual(temporal["source_form"], "none")
        self.assertEqual(temporal["certainty"], "unknown")
        self.assertEqual(temporal["precision"], "unknown")
        self.assertIsNone(temporal["instant"])
        self.assertIsNone(temporal["calendar_range"])
        self.assertIsNone(temporal["instant_range"])
        self.assertIsNone(temporal["relative_offset"])
        self.assertIsNone(temporal["recurrence"])
        self.assertFalse(temporal["anchored_to_source_time"])

    def test_unmarked_current_state_still_anchors_to_source_time(self) -> None:
        temporal = _normalize_temporal(
            {
                "semantic": "state_validity",
                "shape": "none",
                "basis": "none",
                "source_form": "implicit_source_time",
                "certainty": "unknown",
                "precision": "unknown",
                "instant": None,
                "calendar_range": None,
                "instant_range": None,
                "relative_offset": None,
                "recurrence": None,
                "anchored_to_source_time": False,
                "normalization_policy_version": (
                    "memory_temporal_normalization_v5"
                ),
                "reason_codes": ["implicit_source_time"],
            },
            "2026-07-21T12:00:00+00:00",
        )
        self.assertEqual(temporal["shape"], "open_interval")
        self.assertTrue(temporal["anchored_to_source_time"])
        self.assertEqual(
            temporal["instant_range"]["lower"],
            "2026-07-21T12:00:00.000000Z",
        )


if __name__ == "__main__":
    unittest.main()
