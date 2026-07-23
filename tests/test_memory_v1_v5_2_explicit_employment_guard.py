from __future__ import annotations

import hashlib
import unittest

from scripts.memory_v1_relational_extraction_v5_local_provider import (
    SEMANTIC_V5_2_REGISTRY_VERSION,
    _deterministic_policy_packet,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    TrustedExtractionSource,
)


class ExplicitEmploymentGuardTest(unittest.TestCase):
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

    def test_started_employment_is_exact_and_undated(self) -> None:
        result = _deterministic_policy_packet(
            self.source(
                "I started working for the Wisconsin Early Autism Project."
            ),
            registry_version=SEMANTIC_V5_2_REGISTRY_VERSION,
        )
        self.assertIsNotNone(result)
        packet, guard_code = result
        self.assertEqual(guard_code, "explicit_started_employment")
        value = packet.model_dump(mode="json")
        self.assertEqual(value["comparison_hints"], [])
        self.assertEqual(value["deferrals"], [])
        self.assertEqual(value["packet_findings"], [])
        self.assertEqual(
            [
                (
                    item["entity_ref"],
                    item["entity_type"],
                    item["mention_kind"],
                    item["name_text"],
                )
                for item in value["entity_mentions"]
            ],
            [
                ("e00", "self", "self_reference", None),
                (
                    "e01",
                    "organization",
                    "named",
                    "Wisconsin Early Autism Project",
                ),
            ],
        )
        self.assertEqual(len(value["observations"]), 1)
        observation = value["observations"][0]
        self.assertEqual(observation["predicate"], "employment.worked_for")
        self.assertEqual(observation["subject_entity_ref"], "e00")
        self.assertEqual(
            observation["object"], {"kind": "entity", "entity_ref": "e01"}
        )
        temporal = observation["temporal"]
        self.assertEqual(temporal["semantic"], "state_validity")
        self.assertEqual(temporal["shape"], "none")
        self.assertEqual(temporal["certainty"], "unknown")
        self.assertIsNone(temporal["instant"])
        self.assertIsNone(temporal["calendar_range"])
        self.assertIsNone(temporal["instant_range"])
        self.assertIsNone(temporal["relative_offset"])
        self.assertIsNone(temporal["recurrence"])
        self.assertFalse(temporal["anchored_to_source_time"])

    def test_non_named_employer_is_not_deterministically_promoted(self) -> None:
        result = _deterministic_policy_packet(
            self.source("I started working for my father."),
            registry_version=SEMANTIC_V5_2_REGISTRY_VERSION,
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
