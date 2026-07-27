#!/usr/bin/env python3
from __future__ import annotations

import unittest

from scripts.memory_v1_relational_extraction_v5_local_provider import (
    LocalProviderAdapterError,
)
from scripts.memory_v1_v5_local_inference_canary import rejection_code
from scripts.memory_v1_v5_local_inference_scheduler import sanitized_batch_summary
from scripts.memory_v1_v5_local_outcome_policy import classify_rejection_code


class OutcomePolicyTest(unittest.TestCase):
    def test_record_outcomes_never_affect_circuit(self) -> None:
        cases = {
            "context_coreference_unresolved": "deferred",
            "insufficient_evidence": "skipped",
            "nothing_durable_to_stage": "skipped",
            "ordinary_semantic_rejection": "skipped",
            "sensitive_manual_review": "review_required",
            "entity_resolution_unresolved": "review_required",
            "predicate_review_required": "review_required",
            "local_validation_rejected": "skipped",
        }
        for code, disposition in cases.items():
            with self.subTest(code=code):
                policy = classify_rejection_code(code)
                self.assertEqual(policy.outcome_class, "record_terminal")
                self.assertEqual(policy.disposition, disposition)
                self.assertFalse(policy.circuit_impact)

    def test_systemic_and_unknown_fail_closed(self) -> None:
        threshold = classify_rejection_code("local_transport_timeout")
        self.assertEqual(threshold.outcome_class, "systemic_threshold")
        self.assertTrue(threshold.circuit_impact)
        self.assertFalse(threshold.immediate_open)

        immediate = classify_rejection_code("local_rls_invariant_failed")
        self.assertEqual(immediate.outcome_class, "systemic_immediate")
        self.assertTrue(immediate.immediate_open)

        unknown = classify_rejection_code("new_unregistered_failure")
        self.assertEqual(unknown.outcome_class, "systemic_unclassified")
        self.assertEqual(unknown.normalized_reason_code, "unclassified_failure")
        self.assertTrue(unknown.immediate_open)

    def test_validation_errors_receive_sanitized_operational_codes(self) -> None:
        self.assertEqual(
            rejection_code(ValueError("unregistered predicate: example")),
            "unregistered_predicate",
        )
        structured = LocalProviderAdapterError(
            "invalid_structured_output",
            retryable=False,
        )
        self.assertEqual(
            rejection_code(
                structured,
                audit={"validation_error_types": ["missing"]},
            ),
            "local_structured_required_field_missing",
        )
        self.assertEqual(
            rejection_code(
                structured,
                audit={"validation_error_types": ["extra_forbidden"]},
            ),
            "local_structured_extra_field",
        )
        self.assertEqual(
            rejection_code(RuntimeError("internal validator failure")),
            "local_validation_internal_error",
        )

    def test_scheduler_summary_separates_record_and_systemic_outcomes(self) -> None:
        summary = sanitized_batch_summary(
            [
                {"outcome": "accepted", "local_model_calls": 1},
                {
                    "outcome": "rejected",
                    "rejection_code": "context_coreference_unresolved",
                    "local_model_calls": 1,
                },
                {
                    "outcome": "rejected",
                    "rejection_code": "local_transport_timeout",
                    "local_model_calls": 1,
                },
            ]
        )
        self.assertEqual(
            summary["outcome_class_counts"],
            {
                "record_terminal": 1,
                "success": 1,
                "systemic_threshold": 1,
            },
        )
        self.assertEqual(
            summary["operational_reason_counts"],
            {
                "context_reference_unresolved": 1,
                "local_transport_timeout": 1,
            },
        )
        self.assertEqual(summary["disposition_counts"], {"deferred": 1})


if __name__ == "__main__":
    unittest.main()
