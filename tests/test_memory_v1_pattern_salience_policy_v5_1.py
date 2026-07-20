from __future__ import annotations

import copy
import json
import unittest

from scripts.memory_v1_pattern_salience_policy_v5_1 import (
    DEFAULT_CASES,
    DEFAULT_SCHEMA,
    PatternInput,
    assess_pattern,
    evaluate_cases,
    load_cases,
    signal_effects,
    validate_schema_contract,
)


class PatternSaliencePolicyV51Test(unittest.TestCase):
    def test_schema_is_closed_and_multidimensional(self) -> None:
        schema = json.loads(DEFAULT_SCHEMA.read_text(encoding="utf-8"))
        validate_schema_contract(schema)

    def test_all_governed_cases_pass(self) -> None:
        rows = load_cases(DEFAULT_CASES)
        self.assertEqual(len(rows), 18)
        self.assertEqual(evaluate_cases(rows), [])

    def test_one_transient_state_is_not_a_pattern(self) -> None:
        decision = assess_pattern(
            PatternInput(
                pattern_kind="recurrence",
                eligible_occurrence_count=1,
                independent_episode_count=1,
                distinct_temporal_bucket_count=1,
                span_days=0,
                counterexample_count=0,
                explicit_owner_pattern_report=False,
                transient_identity_risk=True,
                structured_domain_authoritative=False,
                third_party_scope=False,
            )
        )
        self.assertEqual(decision.disposition, "no_pattern")
        self.assertFalse(decision.automatic_review_eligible)
        self.assertTrue(decision.identity_inference_forbidden)

    def test_same_episode_repetition_is_not_independent_evidence(self) -> None:
        decision = assess_pattern(
            PatternInput(
                pattern_kind="recurrence",
                eligible_occurrence_count=6,
                independent_episode_count=1,
                distinct_temporal_bucket_count=1,
                span_days=0,
                counterexample_count=0,
                explicit_owner_pattern_report=False,
                transient_identity_risk=True,
                structured_domain_authoritative=False,
                third_party_scope=False,
            )
        )
        self.assertEqual(decision.disposition, "no_pattern")
        self.assertIn("same_episode_repetition_collapsed", decision.reason_codes)

    def test_recurrence_across_weeks_is_reviewable_not_identity(self) -> None:
        decision = assess_pattern(
            PatternInput(
                pattern_kind="recurrence",
                eligible_occurrence_count=3,
                independent_episode_count=3,
                distinct_temporal_bucket_count=3,
                span_days=21,
                counterexample_count=0,
                explicit_owner_pattern_report=False,
                transient_identity_risk=True,
                structured_domain_authoritative=False,
                third_party_scope=False,
            )
        )
        self.assertEqual(decision.disposition, "pattern_review")
        self.assertEqual(decision.pattern_state, "emerging")
        self.assertTrue(decision.automatic_review_eligible)
        self.assertTrue(decision.identity_inference_forbidden)
        self.assertTrue(decision.causal_inference_forbidden)

    def test_retrieval_exposure_does_not_change_evidence_support(self) -> None:
        effects = signal_effects(
            "retrieval_selected", explicit_evidence_captured=False
        )
        self.assertEqual(effects, frozenset({"retrieval_history"}))
        self.assertNotIn("evidence_support", effects)
        self.assertNotIn("retrieval_utility", effects)

    def test_explicit_correction_adds_opposition_not_support(self) -> None:
        effects = signal_effects("corrected", explicit_evidence_captured=True)
        self.assertIn("evidence_opposition", effects)
        self.assertIn("contradiction_pressure", effects)
        self.assertNotIn("evidence_support", effects)

    def test_scalar_salience_property_is_rejected(self) -> None:
        schema = json.loads(DEFAULT_SCHEMA.read_text(encoding="utf-8"))
        modified = copy.deepcopy(schema)
        modified["properties"]["salience"] = {"type": "number"}
        with self.assertRaisesRegex(
            AssertionError, "forbidden canonical assessment properties"
        ):
            validate_schema_contract(modified)

    def test_truth_property_is_rejected(self) -> None:
        schema = json.loads(DEFAULT_SCHEMA.read_text(encoding="utf-8"))
        modified = copy.deepcopy(schema)
        modified["properties"]["truth"] = {"type": "boolean"}
        with self.assertRaisesRegex(
            AssertionError, "forbidden canonical assessment properties"
        ):
            validate_schema_contract(modified)


if __name__ == "__main__":
    unittest.main()
