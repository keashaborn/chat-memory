from __future__ import annotations

import copy
import unittest

from scripts.memory_v1_v5_stage_preflight import _temporal_persistence_compatible


def empty_temporal(semantic: str = "none") -> dict:
    return {
        "semantic": semantic,
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
        "normalization_policy_version": "memory_temporal_normalization_v5",
        "reason_codes": [],
    }


class TemporalPersistenceCompatibilityTest(unittest.TestCase):
    def test_unknown_occurrence_and_plan_retain_semantics(self) -> None:
        self.assertTrue(_temporal_persistence_compatible(empty_temporal("occurrence")))
        self.assertTrue(_temporal_persistence_compatible(empty_temporal("planned_time")))
        self.assertTrue(_temporal_persistence_compatible(empty_temporal("state_validity")))
        self.assertFalse(
            _temporal_persistence_compatible(empty_temporal("observation_time"))
        )

    def test_partial_calendar_requires_trusted_source_anchor(self) -> None:
        value = empty_temporal("occurrence")
        value.update(
            {
                "shape": "bounded_interval",
                "basis": "calendar",
                "source_form": "partial_absolute",
                "certainty": "approximate",
                "precision": "month",
                "calendar_range": {
                    "lower": "2026-03-01",
                    "upper": "2026-04-01",
                    "bounds": "[)",
                },
                "anchored_to_source_time": True,
            }
        )
        self.assertTrue(_temporal_persistence_compatible(value))
        invalid = copy.deepcopy(value)
        invalid["anchored_to_source_time"] = False
        self.assertFalse(_temporal_persistence_compatible(invalid))

    def test_relative_state_may_be_an_open_interval(self) -> None:
        value = empty_temporal("state_validity")
        value.update(
            {
                "shape": "open_interval",
                "basis": "relative",
                "source_form": "relative",
                "certainty": "bounded",
                "relative_offset": {
                    "direction": "past",
                    "magnitude": 1.0,
                    "unit": "year",
                    "approximate": True,
                    "anchor_source": "evidence_observed_at",
                },
                "anchored_to_source_time": True,
            }
        )
        self.assertTrue(_temporal_persistence_compatible(value))
        invalid = copy.deepcopy(value)
        invalid["shape"] = "bounded_interval"
        self.assertFalse(_temporal_persistence_compatible(invalid))


if __name__ == "__main__":
    unittest.main()
