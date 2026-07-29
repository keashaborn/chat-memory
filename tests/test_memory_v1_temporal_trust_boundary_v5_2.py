from __future__ import annotations

import unittest

from scripts.memory_v1_relational_extraction_v5_local_provider import (
    _historical_state_before_source_temporal,
    _llama_cpp_output_schema,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    ProviderPacket,
    TrustedExtractionSource,
    _normalize_temporal,
)


SOURCE_TIME = "2026-07-21T12:00:00+00:00"


class TemporalTrustBoundaryV52Test(unittest.TestCase):
    @staticmethod
    def source() -> TrustedExtractionSource:
        content = "I previously had a pet."
        return TrustedExtractionSource.create(
            job_id="00000000-0000-4000-8000-000000000001",
            source_system="public.chat_log",
            source_external_id="00000000-0000-4000-8000-000000000002",
            source_sha256=(
                "2ae0ca8392f7cb733568246649dd1408e"
                "a497248c7666305add6c99076fb01d9"
            ),
            source_recorded_at=SOURCE_TIME,
            content=content,
        )

    def test_provider_schema_forces_trusted_anchor_false(self) -> None:
        schema = _llama_cpp_output_schema(
            ProviderPacket.model_json_schema()
        )
        anchored = schema["$defs"]["ProviderTemporal"]["properties"][
            "anchored_to_source_time"
        ]
        self.assertEqual(
            anchored,
            {"const": False, "type": "boolean"},
        )

    def test_compiler_leaves_historical_anchor_untrusted(self) -> None:
        temporal = _historical_state_before_source_temporal(self.source())
        self.assertFalse(temporal["anchored_to_source_time"])
        self.assertEqual(temporal["source_form"], "implicit_source_time")
        self.assertEqual(temporal["shape"], "open_interval")
        self.assertIsNone(temporal["instant_range"]["lower"])
        self.assertIsNone(temporal["instant_range"]["upper"])
        self.assertNotIn(
            "trusted_source_time_upper_bound",
            temporal["reason_codes"],
        )

    def test_server_adds_only_trusted_historical_upper_bound(self) -> None:
        temporal = _historical_state_before_source_temporal(self.source())
        normalized = _normalize_temporal(temporal, SOURCE_TIME)
        self.assertTrue(normalized["anchored_to_source_time"])
        self.assertIsNone(normalized["instant_range"]["lower"])
        self.assertEqual(
            normalized["instant_range"]["upper"],
            "2026-07-21T12:00:00.000000Z",
        )
        self.assertIn(
            "trusted_source_time_upper_bound",
            normalized["reason_codes"],
        )

    def test_provider_owned_trusted_anchor_still_fails_closed(self) -> None:
        temporal = _historical_state_before_source_temporal(self.source())
        temporal["anchored_to_source_time"] = True
        temporal["instant_range"]["upper"] = SOURCE_TIME
        with self.assertRaisesRegex(
            ValueError,
            "provider cannot assert trusted source-time anchoring",
        ):
            _normalize_temporal(temporal, SOURCE_TIME)

    def test_undated_occurrence_remains_unanchored(self) -> None:
        normalized = _normalize_temporal(
            {
                "semantic": "occurrence",
                "shape": "none",
                "basis": "none",
                "source_form": "none",
                "certainty": "unknown",
                "precision": "unknown",
                "instant": None,
                "calendar_range": None,
                "instant_range": None,
                "relative_offset": None,
                "recurrence": None,
                "anchored_to_source_time": False,
                "reason_codes": ["undated_occurrence"],
            },
            SOURCE_TIME,
        )
        self.assertFalse(normalized["anchored_to_source_time"])
        self.assertEqual(normalized["source_form"], "none")
        self.assertIsNone(normalized["instant"])
        self.assertIsNone(normalized["instant_range"])


if __name__ == "__main__":
    unittest.main()
