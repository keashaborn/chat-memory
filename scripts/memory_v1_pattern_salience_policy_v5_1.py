from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


POLICY_VERSION = "memory_v1_epistemic_pattern_salience_policy_v5_1"
CONTRACT_VERSION = "memory_v1_epistemic_pattern_salience_v5_1"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = (
    ROOT / "specs/memory_v1_epistemic_pattern_salience_v5_1.schema.json"
)
DEFAULT_CASES = ROOT / "evals/memory_v1_pattern_salience_v5_1_cases.jsonl"
PATTERN_KINDS = {
    "recurrence",
    "persistence",
    "transition",
    "trend",
    "co_occurrence",
    "sequence",
}
FORBIDDEN_CANONICAL_PROPERTIES = {
    "owner_user_id",
    "truth",
    "truth_score",
    "truth_confidence",
    "confidence",
    "salience",
    "final_score",
    "overall_score",
    "net_support_score",
}
SALIENCE_DIMENSIONS = {
    "importance",
    "frequency",
    "recency",
    "emotional_significance",
    "goal_relevance",
    "future_utility",
    "retrieval_utility",
    "contradiction_pressure",
}
SIGNAL_EFFECTS = {
    "passage_of_time": {"recency"},
    "retrieval_selected": {"retrieval_history"},
    "explicitly_helpful": {"retrieval_history", "retrieval_utility"},
    "emotional_disclosure": {"emotional_significance"},
    "independent_recurrence": {"frequency"},
}


@dataclass(frozen=True)
class PatternInput:
    pattern_kind: str
    eligible_occurrence_count: int
    independent_episode_count: int
    distinct_temporal_bucket_count: int
    span_days: int
    counterexample_count: int
    explicit_owner_pattern_report: bool
    transient_identity_risk: bool
    structured_domain_authoritative: bool
    third_party_scope: bool


@dataclass(frozen=True)
class PatternDecision:
    disposition: str
    pattern_state: str
    automatic_review_eligible: bool
    reason_codes: tuple[str, ...]
    identity_inference_forbidden: bool = True
    causal_inference_forbidden: bool = True


def stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _property_names(schema: dict[str, Any]) -> set[str]:
    output: set[str] = set()
    for node in _walk(schema):
        if isinstance(node, dict) and isinstance(node.get("properties"), dict):
            output.update(node["properties"])
    return output


def validate_schema_contract(schema: dict[str, Any]) -> None:
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise AssertionError("unexpected JSON Schema draft")
    if schema.get("additionalProperties") is not False:
        raise AssertionError("assessment packet must be closed")
    properties = schema.get("properties", {})
    if properties.get("contract_version", {}).get("const") != CONTRACT_VERSION:
        raise AssertionError("assessment contract version mismatch")
    if properties.get("policy_version", {}).get("const") != POLICY_VERSION:
        raise AssertionError("assessment policy version mismatch")
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        raise AssertionError("assessment schema definitions missing")
    for node in _walk(schema):
        if isinstance(node, dict) and node.get("type") == "object":
            if node.get("additionalProperties") is not False:
                raise AssertionError("all assessment objects must be closed")
        if isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
                raise AssertionError("only local schema references are allowed")
            if ref.removeprefix("#/$defs/") not in definitions:
                raise AssertionError("schema reference is unresolved")
    forbidden = _property_names(schema) & FORBIDDEN_CANONICAL_PROPERTIES
    if forbidden:
        raise AssertionError(
            f"forbidden canonical assessment properties: {sorted(forbidden)}"
        )
    salience = definitions.get("salience_features", {}).get("properties", {})
    actual_dimensions = set(salience) - {
        "method_version",
        "signal_manifest_sha256",
    }
    if actual_dimensions != SALIENCE_DIMENSIONS:
        raise AssertionError("salience dimension set mismatch")
    pattern = definitions.get("pattern_assessment", {}).get("properties", {})
    for field in ("identity_inference_forbidden", "causal_inference_forbidden"):
        if pattern.get(field, {}).get("const") is not True:
            raise AssertionError(f"{field} must remain true")


def _validate_pattern_input(value: PatternInput) -> None:
    if value.pattern_kind not in PATTERN_KINDS:
        raise ValueError("unknown pattern kind")
    for field in (
        "eligible_occurrence_count",
        "independent_episode_count",
        "distinct_temporal_bucket_count",
        "span_days",
        "counterexample_count",
    ):
        current = getattr(value, field)
        if isinstance(current, bool) or not isinstance(current, int) or current < 0:
            raise ValueError(f"{field} must be a non-negative integer")
    if value.independent_episode_count > value.eligible_occurrence_count:
        raise ValueError("independent episodes exceed eligible occurrences")


def assess_pattern(value: PatternInput) -> PatternDecision:
    _validate_pattern_input(value)
    reasons: set[str] = set()
    if value.transient_identity_risk:
        reasons.add("identity_inference_forbidden")
    if value.structured_domain_authoritative:
        reasons.add("authoritative_structured_domain_route")
        return PatternDecision(
            "structured_aggregate",
            "insufficient",
            False,
            tuple(sorted(reasons)),
        )
    if value.third_party_scope:
        reasons.add("third_party_pattern_requires_review")
        return PatternDecision(
            "manual_review",
            "insufficient",
            False,
            tuple(sorted(reasons)),
        )
    if value.pattern_kind == "transition":
        reasons.add("transition_requires_reconciliation")
        return PatternDecision(
            "manual_review",
            "ended",
            False,
            tuple(sorted(reasons)),
        )
    if value.pattern_kind in {"co_occurrence", "sequence"}:
        reasons.add("causal_inference_forbidden")
        reasons.add(
            "association_requires_manual_review"
            if value.pattern_kind == "co_occurrence"
            else "sequence_requires_manual_review"
        )
        state = (
            "emerging"
            if value.eligible_occurrence_count >= 3
            and value.independent_episode_count >= 2
            and value.distinct_temporal_bucket_count >= 2
            else "insufficient"
        )
        return PatternDecision(
            "manual_review",
            state,
            False,
            tuple(sorted(reasons)),
        )
    if value.pattern_kind == "trend":
        reasons.add("trend_requires_manual_review")
        return PatternDecision(
            "manual_review",
            "emerging" if value.eligible_occurrence_count >= 3 else "insufficient",
            False,
            tuple(sorted(reasons)),
        )
    if value.explicit_owner_pattern_report:
        reasons.add("explicit_owner_pattern_report")
        if value.counterexample_count:
            reasons.add("pattern_counterexamples_present")
            return PatternDecision(
                "manual_review",
                "contested",
                False,
                tuple(sorted(reasons)),
            )
        return PatternDecision(
            "pattern_review",
            "emerging",
            True,
            tuple(sorted(reasons)),
        )

    if value.eligible_occurrence_count < 3:
        reasons.add("insufficient_distinct_occurrences")
    if value.independent_episode_count < 2:
        reasons.add("insufficient_independent_episodes")
        if value.eligible_occurrence_count > 1:
            reasons.add("same_episode_repetition_collapsed")
    if value.distinct_temporal_bucket_count < 2:
        reasons.add("insufficient_temporal_buckets")
    if value.transient_identity_risk and value.span_days < 7:
        reasons.add("insufficient_transient_state_span")
    blockers = {
        "insufficient_distinct_occurrences",
        "insufficient_independent_episodes",
        "insufficient_temporal_buckets",
        "insufficient_transient_state_span",
    }
    if reasons & blockers:
        return PatternDecision(
            "no_pattern",
            "insufficient",
            False,
            tuple(sorted(reasons)),
        )
    if value.counterexample_count:
        reasons.add("pattern_counterexamples_present")
        return PatternDecision(
            "manual_review",
            "contested",
            False,
            tuple(sorted(reasons)),
        )
    reasons.add("inferred_recurrence_threshold_met")
    return PatternDecision(
        "pattern_review",
        "emerging",
        True,
        tuple(sorted(reasons)),
    )


def signal_effects(
    event_type: str,
    *,
    explicit_evidence_captured: bool,
) -> frozenset[str]:
    if event_type == "explicitly_confirmed":
        values = {"retrieval_history", "retrieval_utility"}
        if explicit_evidence_captured:
            values.add("evidence_support")
        return frozenset(values)
    if event_type == "corrected":
        values = {"retrieval_history", "contradiction_pressure"}
        if explicit_evidence_captured:
            values.add("evidence_opposition")
        return frozenset(values)
    if event_type not in SIGNAL_EFFECTS:
        raise ValueError("unknown retrieval/salience signal type")
    return frozenset(SIGNAL_EFFECTS[event_type])


def load_cases(path: str | Path = DEFAULT_CASES) -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    case_ids = [row.get("case_id") for row in rows]
    if len(case_ids) != len(set(case_ids)):
        raise AssertionError("pattern/salience case IDs must be unique")
    return rows


def evaluate_cases(rows: list[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    for row in rows:
        expected = row["expected"]
        if row["case_type"] == "pattern":
            decision = assess_pattern(PatternInput(**row["input"]))
            if decision.disposition != expected["disposition"]:
                failures.append(f"{row['case_id']}:disposition")
            if decision.pattern_state != expected["pattern_state"]:
                failures.append(f"{row['case_id']}:pattern_state")
            if (
                decision.automatic_review_eligible
                != expected["automatic_review_eligible"]
            ):
                failures.append(f"{row['case_id']}:automatic_review_eligible")
            if not set(expected["required_reasons"]) <= set(decision.reason_codes):
                failures.append(f"{row['case_id']}:required_reasons")
        elif row["case_type"] == "signal":
            effects = signal_effects(**row["input"])
            if effects != frozenset(expected["allowed_changes"]):
                failures.append(f"{row['case_id']}:allowed_changes")
            if effects & set(expected["forbidden_changes"]):
                failures.append(f"{row['case_id']}:forbidden_changes")
        else:
            failures.append(f"{row.get('case_id')}:unknown_case_type")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA))
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    args = parser.parse_args()
    schema = json.loads(Path(args.schema).read_text(encoding="utf-8"))
    validate_schema_contract(schema)
    rows = load_cases(args.cases)
    failures = evaluate_cases(rows)
    print(
        stable_json(
            {
                "policy_version": POLICY_VERSION,
                "schema_sha256": sha256(schema),
                "cases_sha256": sha256(rows),
                "case_count": len(rows),
                "failure_count": len(failures),
                "failures": failures,
            }
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
