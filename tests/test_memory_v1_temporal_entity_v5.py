#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from scripts.memory_v1_relational_v5_contract_test import load_jsonl
from scripts.memory_v1_temporal_entity_v5_test import (
    validate_cases,
    validate_resolution_schema,
    validate_temporal_value,
)


ROOT = Path(__file__).resolve().parents[1]
RESOLUTION_SCHEMA = ROOT / "specs" / "memory_v1_entity_resolution_review_v5.schema.json"
BASE_CASES = ROOT / "evals" / "memory_v1_relational_extraction_v5_cases.jsonl"
CASES = ROOT / "evals" / "memory_v1_temporal_entity_resolution_v5_cases.jsonl"


def temporal(**overrides):
    value = {
        "semantic": "none",
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
    value.update(overrides)
    return value


class TemporalEntityV5Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.resolution_schema = json.loads(
            RESOLUTION_SCHEMA.read_text(encoding="utf-8")
        )
        cls.base_cases = load_jsonl(BASE_CASES)
        cls.cases = load_jsonl(CASES)

    def test_checked_in_contract_is_valid(self) -> None:
        validate_resolution_schema(copy.deepcopy(self.resolution_schema))
        validate_cases(copy.deepcopy(self.cases), copy.deepcopy(self.base_cases))

    def test_owner_in_resolution_schema_fails_closed(self) -> None:
        schema = copy.deepcopy(self.resolution_schema)
        schema["properties"]["owner_user_id"] = {"type": "string"}
        with self.assertRaisesRegex(AssertionError, "forbidden resolver"):
            validate_resolution_schema(schema)

    def test_scalar_entity_score_fails_closed(self) -> None:
        schema = copy.deepcopy(self.resolution_schema)
        schema["$defs"]["feature_vector"]["properties"]["score"] = {
            "type": "number"
        }
        with self.assertRaisesRegex(AssertionError, "forbidden resolver"):
            validate_resolution_schema(schema)

    def test_cross_owner_style_auto_link_fixture_fails_closed(self) -> None:
        cases = copy.deepcopy(self.cases)
        cases[0]["expected"]["allowed_auto_link_bindings"] = ["foreign:entity"]
        with self.assertRaisesRegex(AssertionError, "unsafe auto-link"):
            validate_cases(cases, copy.deepcopy(self.base_cases))

    def test_unresolved_project_creation_fails_closed(self) -> None:
        cases = copy.deepcopy(self.cases)
        cases[1]["expected"]["project_creation_forbidden"] = False
        with self.assertRaisesRegex(AssertionError, "unresolved project"):
            validate_cases(cases, copy.deepcopy(self.base_cases))

    def test_month_calendar_range_is_valid(self) -> None:
        validate_temporal_value(
            temporal(
                semantic="occurrence",
                shape="bounded_interval",
                basis="calendar",
                source_form="partial_absolute",
                certainty="bounded",
                precision="month",
                calendar_range={
                    "lower": "2026-03-01",
                    "upper": "2026-04-01",
                    "bounds": "[)",
                },
                anchored_to_source_time=True,
                reason_codes=["year_anchored_to_source_time"],
            )
        )

    def test_relative_offset_cannot_fabricate_instant(self) -> None:
        value = temporal(
            semantic="occurrence",
            shape="instant",
            basis="relative",
            source_form="relative",
            certainty="approximate",
            precision="year",
            instant="2024-07-14T17:00:00+00:00",
            relative_offset={
                "direction": "past",
                "magnitude": 2,
                "unit": "year",
                "approximate": True,
                "anchor_source": "evidence_observed_at",
            },
            anchored_to_source_time=True,
            reason_codes=["approximate_relative_offset"],
        )
        with self.assertRaisesRegex(AssertionError, "relative temporal form"):
            validate_temporal_value(value)

    def test_calendar_value_cannot_be_timestamp(self) -> None:
        value = temporal(
            semantic="occurrence",
            shape="instant",
            basis="calendar",
            source_form="absolute",
            certainty="exact",
            precision="day",
            instant="2026-07-01T00:00:00+00:00",
        )
        with self.assertRaisesRegex(AssertionError, "calendar temporal form"):
            validate_temporal_value(value)

    def test_range_must_be_half_open(self) -> None:
        value = temporal(
            semantic="state_validity",
            shape="open_interval",
            basis="calendar",
            source_form="absolute",
            certainty="bounded",
            precision="day",
            calendar_range={"lower": "2026-07-01", "upper": None, "bounds": "[]"},
        )
        with self.assertRaisesRegex(AssertionError, "half-open"):
            validate_temporal_value(value)


if __name__ == "__main__":
    unittest.main()
