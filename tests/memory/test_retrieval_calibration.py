from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from rag_engine.governed_memory.contracts import ContractViolation
from rag_engine.governed_memory.runtime.calibration import (
    CALIBRATION_ARTIFACT_FIELDS,
    CalibrationCase,
    build_candidate_calibration,
    calibration_artifact_sha256,
    calibration_decision_from_artifact,
    fixed_score_to_micros,
    load_calibration_decision,
)


ROOT = Path(__file__).resolve().parents[2]
UNAPPROVED = (
    ROOT
    / "ops"
    / "governed_memory"
    / "calibration"
    / "semantic_retrieval.unapproved.json"
)
HASH = "a" * 64
APPROVAL_HASH = "b" * 64


class RetrievalCalibrationTests(unittest.TestCase):
    def cases(self) -> tuple[CalibrationCase, ...]:
        return (
            CalibrationCase(
                case_id="case.a",
                expected_relevant=True,
                score_micros=fixed_score_to_micros("0.910000"),
            ),
            CalibrationCase(
                case_id="case.b",
                expected_relevant=False,
                score_micros=fixed_score_to_micros("0.810000"),
            ),
            CalibrationCase(
                case_id="case.c",
                expected_relevant=True,
                score_micros=fixed_score_to_micros("0.800000"),
            ),
            CalibrationCase(
                case_id="case.d",
                expected_relevant=False,
                score_micros=fixed_score_to_micros("0.200000"),
            ),
        )

    def approved_artifact(self) -> dict[str, object]:
        approved = build_candidate_calibration(
            self.cases(),
            holdout_manifest_sha256=HASH,
        )
        approved["status"] = "approved"
        approved["retrieval_enabled"] = True
        approved["approval_receipt_sha256"] = APPROVAL_HASH
        approved["artifact_sha256"] = calibration_artifact_sha256(approved)
        return approved

    def test_fixed_point_scores_reject_binary_float_and_excess_precision(self) -> None:
        self.assertEqual(fixed_score_to_micros("0.123456"), 123_456)
        with self.assertRaisesRegex(ContractViolation, "invalid_calibration_score"):
            fixed_score_to_micros(0.5)
        with self.assertRaisesRegex(
            ContractViolation,
            "calibration_score_precision_exceeded",
        ):
            fixed_score_to_micros("0.1234567")

    def test_harness_is_byte_material_deterministic_and_never_self_approves(self) -> None:
        first = build_candidate_calibration(
            self.cases(),
            holdout_manifest_sha256=HASH,
        )
        second = build_candidate_calibration(
            self.cases(),
            holdout_manifest_sha256=HASH,
        )
        self.assertEqual(first, second)
        self.assertEqual(first["threshold_micros"], 800_000)
        self.assertEqual(first["status"], "candidate_unapproved")
        self.assertFalse(first["retrieval_enabled"])
        self.assertIsNone(first["approval_receipt_sha256"])
        self.assertEqual(tuple(sorted(first)), CALIBRATION_ARTIFACT_FIELDS)
        decision = calibration_decision_from_artifact(first)
        self.assertFalse(decision.retrieval_enabled)
        self.assertEqual(decision.reason_code, "calibration_unapproved")

    def test_missing_invalid_and_checked_in_unapproved_artifacts_disable_retrieval(self) -> None:
        with TemporaryDirectory() as directory:
            missing = load_calibration_decision(Path(directory) / "missing.json")
            self.assertFalse(missing.retrieval_enabled)
            self.assertEqual(missing.reason_code, "calibration_absent")
            invalid_path = Path(directory) / "invalid.json"
            invalid_path.write_text('{"x":1,"x":2}', encoding="utf-8")
            invalid = load_calibration_decision(invalid_path)
            self.assertFalse(invalid.retrieval_enabled)
            self.assertEqual(invalid.reason_code, "calibration_invalid")

        checked = load_calibration_decision(UNAPPROVED)
        self.assertFalse(checked.retrieval_enabled)
        self.assertEqual(checked.reason_code, "calibration_unapproved")

    def test_only_independently_bound_external_approval_enables_retrieval(
        self,
    ) -> None:
        approved = self.approved_artifact()
        artifact_hash = str(approved["artifact_sha256"])
        with self.assertRaisesRegex(
            ContractViolation,
            "calibration_expected_artifact_sha256_required",
        ):
            calibration_decision_from_artifact(approved)
        with self.assertRaisesRegex(
            ContractViolation,
            "calibration_expected_approval_receipt_sha256_required",
        ):
            calibration_decision_from_artifact(
                approved,
                expected_artifact_sha256=artifact_hash,
            )
        with self.assertRaisesRegex(
            ContractViolation,
            "calibration_expected_artifact_sha256_mismatch",
        ):
            calibration_decision_from_artifact(
                approved,
                expected_artifact_sha256="c" * 64,
                expected_approval_receipt_sha256=APPROVAL_HASH,
            )
        with self.assertRaisesRegex(
            ContractViolation,
            "calibration_expected_approval_receipt_sha256_mismatch",
        ):
            calibration_decision_from_artifact(
                approved,
                expected_artifact_sha256=artifact_hash,
                expected_approval_receipt_sha256="c" * 64,
            )
        decision = calibration_decision_from_artifact(
            approved,
            expected_artifact_sha256=artifact_hash,
            expected_approval_receipt_sha256=APPROVAL_HASH,
        )
        self.assertTrue(decision.retrieval_enabled)
        self.assertEqual(decision.threshold_micros, 800_000)

        with TemporaryDirectory() as directory:
            path = Path(directory) / "approved.json"
            path.write_text(
                json.dumps(approved, sort_keys=True),
                encoding="utf-8",
            )
            unbound = load_calibration_decision(path)
            self.assertFalse(unbound.retrieval_enabled)
            self.assertEqual(unbound.reason_code, "calibration_invalid")
            mismatched = load_calibration_decision(
                path,
                expected_artifact_sha256=artifact_hash,
                expected_approval_receipt_sha256="c" * 64,
            )
            self.assertFalse(mismatched.retrieval_enabled)
            self.assertEqual(mismatched.reason_code, "calibration_invalid")
            bound = load_calibration_decision(
                path,
                expected_artifact_sha256=artifact_hash,
                expected_approval_receipt_sha256=APPROVAL_HASH,
            )
            self.assertTrue(bound.retrieval_enabled)

        tampered = dict(approved)
        tampered["threshold_micros"] = 799_999
        with self.assertRaisesRegex(
            ContractViolation,
            "calibration_artifact_sha256_mismatch",
        ):
            calibration_decision_from_artifact(
                tampered,
                expected_artifact_sha256=artifact_hash,
                expected_approval_receipt_sha256=APPROVAL_HASH,
            )

    def test_artifact_contains_no_holdout_examples_or_text(self) -> None:
        artifact = json.loads(UNAPPROVED.read_text(encoding="utf-8"))
        self.assertEqual(tuple(sorted(artifact)), CALIBRATION_ARTIFACT_FIELDS)
        self.assertNotIn("examples", artifact)
        self.assertNotIn("queries", artifact)
        self.assertNotIn("text", artifact)


if __name__ == "__main__":
    unittest.main()
