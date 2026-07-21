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

from rag_engine.resse_runtime_policy import ResponseMode
from rag_engine.resse_user_preferences import (
    PreferenceSelectionContext,
    build_preference_envelope,
)


DEFAULT_CASES = ROOT / "evals" / "resse_preference_cases_v0_1.jsonl"


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


def _context(raw: Mapping[str, Any]) -> PreferenceSelectionContext:
    return PreferenceSelectionContext(
        response_mode=ResponseMode(str(raw["response_mode"])),
        nickname_relevant=bool(raw.get("nickname_relevant", False)),
        occupation_relevant=bool(raw.get("occupation_relevant", False)),
        more_about_you_relevant=bool(raw.get("more_about_you_relevant", False)),
        custom_instructions_relevant=bool(raw.get("custom_instructions_relevant", False)),
    )


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


def evaluate_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    mode_counts: Counter[str] = Counter()

    for case in cases:
        case_id = str(case["id"])
        context = _context(case["context"])
        decision = build_preference_envelope(case["input"], context).to_dict()
        mode_counts[context.response_mode.value] += 1
        mismatches = _subset_mismatches(decision, case["expected"])
        if mismatches:
            failures.append({"id": case_id, "mismatches": mismatches})

    return {
        "status": "pass" if not failures else "fail",
        "cases": len(cases),
        "passed": len(cases) - len(failures),
        "failed": len(failures),
        "critical": sum(bool(case.get("critical")) for case in cases),
        "mode_counts": dict(sorted(mode_counts.items())),
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate isolated RESSE preferences.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    args = parser.parse_args()
    result = evaluate_cases(load_cases(args.cases))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
