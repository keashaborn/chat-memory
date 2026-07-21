#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag_engine.resse_decision_bundle import (
    ProfileRelevanceSignals,
    decide_resse_bundle,
)
from rag_engine.resse_runtime_policy import PolicyInput, PolicySignals


DEFAULT_CASES = ROOT / "evals" / "resse_composite_cases_v0_1.jsonl"


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        case_id = str(value.get("id") or "").strip()
        if not case_id:
            raise ValueError(f"line {line_number}: missing id")
        if case_id in seen:
            raise ValueError(f"line {line_number}: duplicate id {case_id}")
        seen.add(case_id)
        cases.append(value)
    return cases


def _subset_mismatches(
    actual: Any,
    expected: Any,
    *,
    path: str = "$",
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return [{"path": path, "actual": actual, "expected": expected}]
        for key, expected_value in expected.items():
            child = f"{path}.{key}"
            if key not in actual:
                failures.append({"path": child, "actual": "<missing>", "expected": expected_value})
                continue
            failures.extend(_subset_mismatches(actual[key], expected_value, path=child))
        return failures
    if actual != expected:
        failures.append({"path": path, "actual": actual, "expected": expected})
    return failures


def _signals(raw: Mapping[str, Any]) -> PolicySignals:
    allowed = PolicySignals.__dataclass_fields__.keys()
    return PolicySignals(**{key: raw[key] for key in allowed if key in raw})


def _relevance(raw: Mapping[str, Any]) -> ProfileRelevanceSignals:
    allowed = ProfileRelevanceSignals.__dataclass_fields__.keys()
    return ProfileRelevanceSignals(**{key: bool(raw[key]) for key in allowed if key in raw})


def evaluate_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    mode_counts: Counter[str] = Counter()
    coverage_counts: Counter[str] = Counter()

    for case in cases:
        request = PolicyInput.from_mapping(case.get("request", {}))
        result = decide_resse_bundle(
            request,
            preference_payload=case.get("preferences", {}),
            policy_signals=_signals(case.get("signals", {})),
            relevance=_relevance(case.get("relevance", {})),
        ).to_dict()
        mode_counts[result["runtime"]["mode"]] += 1
        coverage_counts.update(str(value) for value in case.get("coverage", []))
        mismatches = _subset_mismatches(result, case["expected"])
        if mismatches:
            failures.append({"id": case["id"], "mismatches": mismatches})

    return {
        "status": "pass" if not failures else "fail",
        "cases": len(cases),
        "passed": len(cases) - len(failures),
        "failed": len(failures),
        "critical": sum(bool(case.get("critical")) for case in cases),
        "mode_counts": dict(sorted(mode_counts.items())),
        "coverage_counts": dict(sorted(coverage_counts.items())),
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate isolated RESSE composite decisions.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    args = parser.parse_args()
    result = evaluate_cases(load_cases(args.cases))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
