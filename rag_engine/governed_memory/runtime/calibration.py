from __future__ import annotations

"""Deterministic semantic-score calibration with an explicit approval gate."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..contracts import (
    ContractViolation,
    canonical_sha256,
    require_exact_int,
    require_key,
    require_sha256,
)


SCORE_SCALE = 1_000_000
CALIBRATION_SCHEMA_VERSION = "governed-memory-retrieval-calibration-v1"
CALIBRATION_ARTIFACT_DOMAIN = "governed_memory.retrieval_calibration_artifact"
CALIBRATION_CASES_DOMAIN = "governed_memory.retrieval_calibration_cases"
CALIBRATION_ARTIFACT_FIELDS = (
    "approval_receipt_sha256",
    "artifact_sha256",
    "cases_sha256",
    "holdout_manifest_sha256",
    "metrics",
    "retrieval_enabled",
    "schema_version",
    "score_scale",
    "status",
    "threshold_micros",
)
CALIBRATION_METRIC_FIELDS = (
    "case_count",
    "false_negative_count",
    "false_positive_count",
    "true_negative_count",
    "true_positive_count",
)


def _fixed_decimal(value: object, *, code: str, allow_float: bool) -> Decimal:
    if isinstance(value, bool) or (
        not allow_float and not isinstance(value, (str, Decimal, int))
    ):
        raise ContractViolation(code)
    if allow_float and not isinstance(value, (str, Decimal, int, float)):
        raise ContractViolation(code)
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ContractViolation(code) from exc
    if not parsed.is_finite() or parsed < 0 or parsed > 1:
        raise ContractViolation(code)
    return parsed


def fixed_score_to_micros(value: object) -> int:
    """Parse an exact holdout score; binary floats are deliberately forbidden."""

    parsed = _fixed_decimal(
        value,
        code="invalid_calibration_score",
        allow_float=False,
    )
    scaled = parsed * SCORE_SCALE
    integral = scaled.to_integral_value()
    if scaled != integral:
        raise ContractViolation("calibration_score_precision_exceeded")
    return int(integral)


def qdrant_score_to_micros(value: object) -> int:
    """Round a Qdrant wire score to the fixed calibration scale."""

    parsed = _fixed_decimal(
        value,
        code="invalid_qdrant_score",
        allow_float=True,
    )
    return int(
        (parsed * SCORE_SCALE).to_integral_value(rounding=ROUND_HALF_EVEN)
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class CalibrationCase:
    case_id: str
    expected_relevant: bool
    score_micros: int

    def __post_init__(self) -> None:
        require_key(self.case_id, "invalid_calibration_case_id")
        if type(self.expected_relevant) is not bool:
            raise ContractViolation("invalid_calibration_case_label")
        require_exact_int(
            self.score_micros,
            code="invalid_calibration_score",
            maximum=SCORE_SCALE,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "CalibrationCase":
        if not isinstance(value, Mapping) or tuple(sorted(value)) != (
            "case_id",
            "expected_relevant",
            "score",
        ):
            raise ContractViolation("invalid_calibration_case")
        return cls(
            case_id=require_key(value["case_id"], "invalid_calibration_case_id"),
            expected_relevant=value["expected_relevant"],  # type: ignore[arg-type]
            score_micros=fixed_score_to_micros(value["score"]),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CalibrationDecision:
    retrieval_enabled: bool
    threshold_micros: int | None
    artifact_sha256: str | None
    reason_code: str

    def __post_init__(self) -> None:
        if type(self.retrieval_enabled) is not bool:
            raise ContractViolation("invalid_calibration_decision")
        require_key(self.reason_code, "invalid_calibration_reason")
        if self.retrieval_enabled:
            require_exact_int(
                self.threshold_micros,
                code="invalid_calibration_threshold",
                maximum=SCORE_SCALE,
            )
            require_sha256(
                self.artifact_sha256,
                "invalid_calibration_artifact_sha256",
            )
        elif self.threshold_micros is not None:
            require_exact_int(
                self.threshold_micros,
                code="invalid_calibration_threshold",
                maximum=SCORE_SCALE,
            )


def _metrics(cases: Sequence[CalibrationCase], threshold: int) -> dict[str, int]:
    counts = {
        "case_count": len(cases),
        "false_negative_count": 0,
        "false_positive_count": 0,
        "true_negative_count": 0,
        "true_positive_count": 0,
    }
    for item in cases:
        predicted = item.score_micros >= threshold
        if item.expected_relevant and predicted:
            counts["true_positive_count"] += 1
        elif item.expected_relevant:
            counts["false_negative_count"] += 1
        elif predicted:
            counts["false_positive_count"] += 1
        else:
            counts["true_negative_count"] += 1
    return counts


def _f1_fraction(metrics: Mapping[str, int]) -> tuple[int, int]:
    true_positive = metrics["true_positive_count"]
    numerator = 2 * true_positive
    denominator = (
        numerator
        + metrics["false_positive_count"]
        + metrics["false_negative_count"]
    )
    return numerator, denominator or 1


def _candidate_better(
    threshold: int,
    metrics: Mapping[str, int],
    best_threshold: int,
    best_metrics: Mapping[str, int],
) -> bool:
    numerator, denominator = _f1_fraction(metrics)
    best_numerator, best_denominator = _f1_fraction(best_metrics)
    cross = numerator * best_denominator - best_numerator * denominator
    if cross != 0:
        return cross > 0
    tie = (
        -metrics["false_positive_count"],
        -metrics["false_negative_count"],
        threshold,
    )
    best_tie = (
        -best_metrics["false_positive_count"],
        -best_metrics["false_negative_count"],
        best_threshold,
    )
    return tie > best_tie


def calibration_artifact_sha256(value: Mapping[str, object]) -> str:
    if not isinstance(value, Mapping):
        raise ContractViolation("invalid_calibration_artifact")
    material = dict(value)
    material.pop("artifact_sha256", None)
    return canonical_sha256(CALIBRATION_ARTIFACT_DOMAIN, material)


def build_candidate_calibration(
    cases: Sequence[CalibrationCase | Mapping[str, object]],
    *,
    holdout_manifest_sha256: str,
) -> dict[str, object]:
    manifest_hash = require_sha256(
        holdout_manifest_sha256,
        "invalid_calibration_holdout_manifest",
    )
    normalized = tuple(
        item
        if isinstance(item, CalibrationCase)
        else CalibrationCase.from_mapping(item)
        for item in cases
    )
    if len(normalized) < 2 or len(normalized) > 10_000:
        raise ContractViolation("invalid_calibration_case_count")
    if tuple(item.case_id for item in normalized) != tuple(
        sorted({item.case_id for item in normalized})
    ):
        raise ContractViolation("calibration_cases_not_sorted_unique")
    if {item.expected_relevant for item in normalized} != {False, True}:
        raise ContractViolation("calibration_labels_not_both_present")

    thresholds = sorted({item.score_micros for item in normalized})
    best_threshold = thresholds[0]
    best_metrics = _metrics(normalized, best_threshold)
    for threshold in thresholds[1:]:
        metrics = _metrics(normalized, threshold)
        if _candidate_better(
            threshold,
            metrics,
            best_threshold,
            best_metrics,
        ):
            best_threshold = threshold
            best_metrics = metrics

    case_material = [
        {
            "case_id": item.case_id,
            "expected_relevant": item.expected_relevant,
            "score_micros": item.score_micros,
        }
        for item in normalized
    ]
    artifact: dict[str, object] = {
        "approval_receipt_sha256": None,
        "cases_sha256": canonical_sha256(
            CALIBRATION_CASES_DOMAIN,
            case_material,
        ),
        "holdout_manifest_sha256": manifest_hash,
        "metrics": best_metrics,
        "retrieval_enabled": False,
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "score_scale": SCORE_SCALE,
        "status": "candidate_unapproved",
        "threshold_micros": best_threshold,
    }
    artifact["artifact_sha256"] = calibration_artifact_sha256(artifact)
    return artifact


def _validate_metrics(value: object) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or tuple(sorted(value)) != CALIBRATION_METRIC_FIELDS:
        raise ContractViolation("invalid_calibration_metrics")
    metrics = {
        key: require_exact_int(
            value[key],
            code="invalid_calibration_metric",
            maximum=10_000,
        )
        for key in CALIBRATION_METRIC_FIELDS
    }
    if metrics["case_count"] != sum(
        metrics[key]
        for key in CALIBRATION_METRIC_FIELDS
        if key != "case_count"
    ):
        raise ContractViolation("calibration_metric_count_mismatch")
    return metrics


def calibration_decision_from_artifact(
    value: Mapping[str, object],
    *,
    expected_artifact_sha256: str | None = None,
    expected_approval_receipt_sha256: str | None = None,
) -> CalibrationDecision:
    if not isinstance(value, Mapping) or tuple(sorted(value)) != CALIBRATION_ARTIFACT_FIELDS:
        raise ContractViolation("invalid_calibration_artifact")
    if value["schema_version"] != CALIBRATION_SCHEMA_VERSION:
        raise ContractViolation("invalid_calibration_schema_version")
    if value["score_scale"] != SCORE_SCALE:
        raise ContractViolation("invalid_calibration_score_scale")
    supplied_hash = require_sha256(
        value["artifact_sha256"],
        "invalid_calibration_artifact_sha256",
    )
    if supplied_hash != calibration_artifact_sha256(value):
        raise ContractViolation("calibration_artifact_sha256_mismatch")
    metrics = _validate_metrics(value["metrics"])
    status = value["status"]
    if status in {"unapproved", "candidate_unapproved"}:
        if value["retrieval_enabled"] is not False or value["approval_receipt_sha256"] is not None:
            raise ContractViolation("unapproved_calibration_enabled")
        threshold = value["threshold_micros"]
        if threshold is not None:
            require_exact_int(
                threshold,
                code="invalid_calibration_threshold",
                maximum=SCORE_SCALE,
            )
        return CalibrationDecision(
            retrieval_enabled=False,
            threshold_micros=threshold if isinstance(threshold, int) else None,
            artifact_sha256=supplied_hash,
            reason_code="calibration_unapproved",
        )
    if status != "approved":
        raise ContractViolation("invalid_calibration_status")
    if value["retrieval_enabled"] is not True or metrics is None:
        raise ContractViolation("approved_calibration_not_enabled")
    threshold = require_exact_int(
        value["threshold_micros"],
        code="invalid_calibration_threshold",
        maximum=SCORE_SCALE,
    )
    require_sha256(
        value["holdout_manifest_sha256"],
        "invalid_calibration_holdout_manifest",
    )
    require_sha256(
        value["cases_sha256"],
        "invalid_calibration_cases_sha256",
    )
    approval_receipt_sha256 = require_sha256(
        value["approval_receipt_sha256"],
        "invalid_calibration_approval_receipt",
    )
    expected_artifact_hash = require_sha256(
        expected_artifact_sha256,
        "calibration_expected_artifact_sha256_required",
    )
    expected_approval_hash = require_sha256(
        expected_approval_receipt_sha256,
        "calibration_expected_approval_receipt_sha256_required",
    )
    if expected_artifact_hash != supplied_hash:
        raise ContractViolation("calibration_expected_artifact_sha256_mismatch")
    if expected_approval_hash != approval_receipt_sha256:
        raise ContractViolation(
            "calibration_expected_approval_receipt_sha256_mismatch"
        )
    return CalibrationDecision(
        retrieval_enabled=True,
        threshold_micros=threshold,
        artifact_sha256=supplied_hash,
        reason_code="calibration_approved",
    )


def _closed_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in pairs:
        if key in result:
            raise ContractViolation("duplicate_calibration_json_key")
        result[key] = item
    return result


def load_calibration_decision(
    path: Path,
    *,
    expected_artifact_sha256: str | None = None,
    expected_approval_receipt_sha256: str | None = None,
) -> CalibrationDecision:
    """Fail closed: missing, malformed, or unapproved artifacts disable retrieval."""

    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return CalibrationDecision(
            retrieval_enabled=False,
            threshold_micros=None,
            artifact_sha256=None,
            reason_code="calibration_absent",
        )
    try:
        if len(raw) > 65_536:
            raise ContractViolation("calibration_artifact_too_large")
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_closed_json_object)
        if not isinstance(parsed, Mapping):
            raise ContractViolation("invalid_calibration_artifact")
        return calibration_decision_from_artifact(
            parsed,
            expected_artifact_sha256=expected_artifact_sha256,
            expected_approval_receipt_sha256=(
                expected_approval_receipt_sha256
            ),
        )
    except (ContractViolation, UnicodeDecodeError, json.JSONDecodeError):
        return CalibrationDecision(
            retrieval_enabled=False,
            threshold_micros=None,
            artifact_sha256=None,
            reason_code="calibration_invalid",
        )


__all__ = [
    "CALIBRATION_SCHEMA_VERSION",
    "CalibrationCase",
    "CalibrationDecision",
    "SCORE_SCALE",
    "build_candidate_calibration",
    "calibration_artifact_sha256",
    "calibration_decision_from_artifact",
    "fixed_score_to_micros",
    "load_calibration_decision",
    "qdrant_score_to_micros",
]
