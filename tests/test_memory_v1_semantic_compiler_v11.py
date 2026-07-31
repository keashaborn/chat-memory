from __future__ import annotations

import unittest

from scripts.memory_v1_relational_extraction_v5_provider import (
    TrustedExtractionSource,
    _normalize_temporal,
)


class SemanticCompilerV11Test(unittest.TestCase):
    def test_observed_time_anchors_state_instead_of_later_ingestion(self) -> None:
        source = TrustedExtractionSource.create(
            job_id="862f5ab6-a037-44b0-be54-1b8c8c25f941",
            source_system="public.chat_log",
            source_external_id="681ab38d-a742-463c-ad26-c74c65eacaa9",
            source_sha256=(
                "7404bd391ec5a376d51ccd4fd0ede2e2780e20e77f704563748a4edff35ced6b"
            ),
            source_recorded_at="2026-07-31T11:24:23.478195Z",
            source_observed_at="2026-07-30T21:39:53.840736Z",
            content=(
                "Now Jerry is in assisted living. He has pretty severe dementia. "
                "He only remembers for about three seconds."
            ),
        )
        temporal = _normalize_temporal(
            {
                "shape": "open_interval",
                "basis": "none",
                "source_form": "implicit_source_time",
                "certainty": "bounded",
                "precision": "exact",
                "instant": None,
                "calendar_range": None,
                "instant_range": None,
                "relative_offset": None,
                "recurrence": None,
                "anchored_to_source_time": False,
                "semantic": "state_validity",
                "reason_codes": ["relationship_known_current_at_source_time"],
            },
            source.trusted_source_time,
        )
        self.assertEqual(
            temporal["instant_range"]["lower"],
            "2026-07-30T21:39:53.840736Z",
        )
        self.assertNotEqual(
            temporal["instant_range"]["lower"], source.source_recorded_at
        )


if __name__ == "__main__":
    unittest.main()
