from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import unittest
from pathlib import Path

from rag_engine.lifeswitch_data_plan_v1 import create_lifeswitch_data_plan_v1
from rag_engine.lifeswitch_data_plan_v2 import LifeSwitchDataPlanV2
from rag_engine.lifeswitch_query_signals_v2 import LifeSwitchQuerySignalsV2
from rag_engine.lifeswitch_temporal_semantics_v1 import LifeSwitchTemporalResolutionV1


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "specs" / "lifeswitch" / "query_temporal_v2_contract_manifest.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class LifeSwitchDataPlanV2CompatibilityTests(unittest.TestCase):
    def test_v1_behavior_remains_unchanged_for_reference_queries(self) -> None:
        today = dt.date(2026, 8, 1)
        expected = {
            "Who won America's Next Top Model in 2015?": ("OFF", None),
            "What are my macros?": ("NUTRITION_RANGE", (dt.date(2026, 7, 12), dt.date(2026, 8, 1))),
            "For each day from July 18 through July 31, show my protein and whether I completed strength training.": ("DAILY_STATUS_RANGE", (dt.date(2026, 7, 18), dt.date(2026, 7, 31))),
            "Show my recent progress on Decline Bench Press": ("EXERCISE_PROGRESSION", (dt.date(2026, 5, 10), dt.date(2026, 8, 1))),
        }
        for query, (intent, dates) in expected.items():
            with self.subTest(query=query):
                value = create_lifeswitch_data_plan_v1(query, today=today)
                self.assertEqual(value.intent, intent)
                if dates is None:
                    self.assertIsNone(value.window)
                else:
                    self.assertEqual((value.window.start_date, value.window.end_date), dates)

    def test_live_runtime_modules_do_not_import_v2(self) -> None:
        new_modules = {
            "lifeswitch_temporal_semantics_v1.py",
            "lifeswitch_query_signals_v2.py",
            "lifeswitch_data_plan_v2.py",
        }
        needles = (
            "lifeswitch_temporal_semantics_v1",
            "lifeswitch_query_signals_v2",
            "lifeswitch_data_plan_v2",
        )
        offenders = []
        for path in sorted((ROOT / "rag_engine").glob("*.py")):
            if path.name in new_modules:
                continue
            text = path.read_text(encoding="utf-8")
            if any(needle in text for needle in needles):
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_json_schemas_exactly_match_models(self) -> None:
        pairs = (
            (LifeSwitchTemporalResolutionV1, ROOT / "specs" / "lifeswitch" / "temporal_window_v1.schema.json"),
            (LifeSwitchQuerySignalsV2, ROOT / "specs" / "lifeswitch" / "query_signals_v2.schema.json"),
            (LifeSwitchDataPlanV2, ROOT / "specs" / "lifeswitch" / "data_plan_v2.schema.json"),
        )
        for model, path in pairs:
            with self.subTest(path=path.name):
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")), model.model_json_schema())

    def test_manifest_binds_every_candidate_input_except_itself(self) -> None:
        value = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(value["contract_version"], "lifeswitch_query_temporal_v2_contract_manifest_v1")
        expected_paths = {
            "rag_engine/lifeswitch_temporal_semantics_v1.py",
            "rag_engine/lifeswitch_query_signals_v2.py",
            "rag_engine/lifeswitch_data_plan_v2.py",
            "tests/test_lifeswitch_temporal_semantics_v1.py",
            "tests/test_lifeswitch_query_signals_v2.py",
            "tests/test_lifeswitch_data_plan_v2.py",
            "tests/test_lifeswitch_data_plan_v2_compatibility.py",
            "tests/fixtures/lifeswitch_query_temporal_v2_cases.json",
            "specs/lifeswitch/temporal_window_v1.schema.json",
            "specs/lifeswitch/query_signals_v2.schema.json",
            "specs/lifeswitch/data_plan_v2.schema.json",
        }
        self.assertEqual(set(value["files"]), expected_paths)
        for relative, expected_hash in value["files"].items():
            self.assertRegex(expected_hash, r"^[0-9a-f]{64}$")
            self.assertEqual(sha256(ROOT / relative), expected_hash)

    def test_fixture_contains_only_synthetic_content_free_expectations(self) -> None:
        path = ROOT / "tests" / "fixtures" / "lifeswitch_query_temporal_v2_cases.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        forbidden_keys = {"owner_user_id", "thread_id", "email", "token", "raw_rows", "answer"}
        for case in value["cases"]:
            self.assertFalse(forbidden_keys.intersection(case))
        self.assertIsNone(re.search(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", path.read_text(encoding="utf-8"), re.IGNORECASE))


if __name__ == "__main__":
    unittest.main()
