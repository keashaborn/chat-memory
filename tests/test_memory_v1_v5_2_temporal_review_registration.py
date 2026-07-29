from __future__ import annotations

import copy
import unittest

from scripts.memory_v1_v5_2_temporal_review_registration import (
    TemporalReviewError,
    validate_temporal_transform,
)


def raw_temporal() -> dict:
    return {
        "semantic": "occurrence",
        "shape": "instant",
        "basis": "relative",
        "source_form": "relative",
        "certainty": "approximate",
        "precision": "year",
        "instant": None,
        "calendar_range": None,
        "instant_range": None,
        "relative_offset": {
            "direction": "past",
            "magnitude": 1.0,
            "unit": "year",
            "approximate": True,
            "anchor_source": "evidence_observed_at",
        },
        "recurrence": None,
        "anchored_to_source_time": False,
        "normalization_policy_version": "memory_temporal_normalization_v5",
        "reason_codes": ["reported_relative_year"],
    }


class TemporalReviewRegistrationTest(unittest.TestCase):
    def test_accepts_only_reviewed_relative_year_anchor(self):
        raw = raw_temporal()
        reviewed = copy.deepcopy(raw)
        reviewed["anchored_to_source_time"] = True
        reviewed["reason_codes"].extend(
            [
                "relative_year_anchored_to_source_time",
                "review_last_year_relative_source_anchor",
            ]
        )
        validate_temporal_transform(raw, reviewed)

    def test_rejects_changed_relative_magnitude(self):
        raw = raw_temporal()
        reviewed = copy.deepcopy(raw)
        reviewed["anchored_to_source_time"] = True
        reviewed["relative_offset"]["magnitude"] = 2.0
        reviewed["reason_codes"].extend(
            [
                "relative_year_anchored_to_source_time",
                "review_last_year_relative_source_anchor",
            ]
        )
        with self.assertRaisesRegex(TemporalReviewError, "more than the anchor"):
            validate_temporal_transform(raw, reviewed)

    def test_rejects_untrusted_source_temporal(self):
        raw = raw_temporal()
        raw["relative_offset"]["unit"] = "month"
        with self.assertRaisesRegex(TemporalReviewError, "source or reviewed"):
            validate_temporal_transform(raw, copy.deepcopy(raw))


if __name__ == "__main__":
    unittest.main()
