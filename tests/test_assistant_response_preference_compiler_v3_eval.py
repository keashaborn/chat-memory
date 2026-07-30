from __future__ import annotations

from pathlib import Path
import unittest

from evals.run_assistant_preference_compiler_v3 import load_cases, score


ROOT = Path(__file__).resolve().parents[1]


class AssistantResponsePreferenceCompilerV3EvalTests(unittest.TestCase):
    def test_case_catalog_is_complete_and_unique(self) -> None:
        cases = load_cases(
            ROOT / "evals" / "assistant_preference_compiler_v3_cases.jsonl"
        )
        self.assertEqual(len(cases), 8)
        identifiers = [str(item["case_id"]) for item in cases]
        self.assertEqual(len(identifiers), len(set(identifiers)))

    def test_score_accepts_required_subset_and_unrestricted_extra_rule(self) -> None:
        case = {
            "expected_settings": {"response_length": "concise"},
            "required_rule_ids": ["practical_focus"],
            "forbidden_rule_ids": ["contextual_playfulness"],
            "required_rejection_codes": [],
        }
        plan = {
            "response_length": "concise",
            "rule_ids": ["practical_focus", "information_dense"],
            "rejection_codes": [],
        }
        self.assertEqual(score(case, plan), [])

    def test_score_detects_missing_forbidden_and_setting_failures(self) -> None:
        case = {
            "expected_settings": {"response_format": "prose"},
            "required_rule_ids": ["practical_focus"],
            "forbidden_rule_ids": ["contextual_playfulness"],
            "required_rejection_codes": ["cannot_change_safety"],
        }
        plan = {
            "response_format": None,
            "rule_ids": ["contextual_playfulness"],
            "rejection_codes": [],
        }
        failures = score(case, plan)
        self.assertEqual(len(failures), 4)

    def test_migration_and_rollback_cover_v3_contract(self) -> None:
        migration = (
            ROOT
            / "ops"
            / "sql"
            / "20260730_assistant_response_preference_compiler_v3.sql"
        ).read_text(encoding="utf-8")
        rollback = (
            ROOT
            / "ops"
            / "sql"
            / "20260730_assistant_response_preference_compiler_v3_rollback.sql"
        ).read_text(encoding="utf-8")
        self.assertIn("cardinality(rule_ids) <= 12", migration)
        self.assertIn("cardinality(summary) <= 16", migration)
        self.assertIn("'calm_patient_tone'", migration)
        self.assertIn("'contextual_poetic_language'", migration)
        self.assertIn("'assistant_preference_compiler_v3'", migration)
        self.assertIn("rollback blocked: v3 preference", rollback)
        self.assertIn("active_compiler_version = 'assistant_preference_compiler_v3'", rollback)


if __name__ == "__main__":
    unittest.main()
