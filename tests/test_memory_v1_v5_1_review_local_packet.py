from __future__ import annotations

import copy
import hashlib
import unittest

from scripts.memory_v1_v5_1_review_local_packet import (
    LocalPacketReviewError,
    normalize_review_packet,
)


CONTENT = "My mother died back in March 2023."


def packet() -> dict:
    span = {
        "start": 0,
        "end": len(CONTENT),
        "span_sha256": hashlib.sha256(CONTENT.encode()).hexdigest(),
    }
    return {
        "observations": [
            {
                "observation_ref": "o00",
                "source_spans": [span],
                "temporal": {
                    "basis": "calendar",
                    "source_form": "partial_absolute",
                    "anchored_to_source_time": False,
                    "calendar_range": {
                        "lower": "2023-03-01",
                        "upper": "2023-04-01",
                        "bounds": "[)",
                    },
                    "reason_codes": ["explicit_month_and_year"],
                },
            }
        ]
    }


class ReviewPacketNormalizationTest(unittest.TestCase):
    def test_explicit_year_repairs_source_form_without_mutating_input(self) -> None:
        source = packet()
        original = copy.deepcopy(source)
        reviewed, transformations = normalize_review_packet(source, CONTENT)
        self.assertEqual(source, original)
        self.assertEqual(
            reviewed["observations"][0]["temporal"]["source_form"], "absolute"
        )
        self.assertFalse(
            reviewed["observations"][0]["temporal"]["anchored_to_source_time"]
        )
        self.assertEqual(len(transformations), 1)
        self.assertNotIn(CONTENT, str(transformations))

    def test_yearless_source_is_not_rewritten(self) -> None:
        content = "My mother died back in March."
        value = packet()
        value["observations"][0]["source_spans"][0] = {
            "start": 0,
            "end": len(content),
            "span_sha256": hashlib.sha256(content.encode()).hexdigest(),
        }
        reviewed, transformations = normalize_review_packet(value, content)
        self.assertEqual(
            reviewed["observations"][0]["temporal"]["source_form"],
            "partial_absolute",
        )
        self.assertEqual(transformations, [])

    def test_source_span_hash_mismatch_fails_closed(self) -> None:
        value = packet()
        value["observations"][0]["source_spans"][0]["span_sha256"] = "0" * 64
        with self.assertRaisesRegex(LocalPacketReviewError, "span hash mismatch"):
            normalize_review_packet(value, CONTENT)

    def test_explicit_year_must_match_normalized_range(self) -> None:
        value = packet()
        value["observations"][0]["temporal"]["calendar_range"]["lower"] = (
            "2024-03-01"
        )
        with self.assertRaisesRegex(LocalPacketReviewError, "year differs"):
            normalize_review_packet(value, CONTENT)


if __name__ == "__main__":
    unittest.main()
