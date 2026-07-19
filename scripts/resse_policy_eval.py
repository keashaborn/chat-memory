#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag_engine.resse_runtime_policy import PolicyInput, decide_policy


DEFAULT_CASES = ROOT / "evals" / "resse_behavior_cases_v0_1.jsonl"


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


def evaluate_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    mode_counts: Counter[str] = Counter()
    closure_counts: Counter[str] = Counter()

    for case in cases:
        case_id = str(case["id"])
        expected = case["expected"]
        expected_routing = expected["routing"]
        decision = decide_policy(PolicyInput.from_mapping(case["input"]))
        mode_counts[decision.mode.value] += 1
        closure_counts[decision.closure.value] += 1

        checks = {
            "mode": (decision.mode.value, expected["mode"]),
            "closure": (decision.closure.value, expected["behavior"]["closure"]),
            "fm_lens": (decision.fm_lens.value, expected_routing["fm_lens"]),
            "fm_tiers": (list(decision.fm_tiers), expected_routing["fm_tiers"]),
            "memory_intent_owner": (
                decision.memory_intent_owner,
                expected_routing["memory_intent_owner"],
            ),
        }
        if "fm_max_hits" in expected_routing:
            checks["fm_max_hits"] = (
                decision.fm_max_hits,
                expected_routing["fm_max_hits"],
            )

        mismatches = {
            key: {"actual": actual, "expected": expected_value}
            for key, (actual, expected_value) in checks.items()
            if actual != expected_value
        }
        if mismatches:
            failures.append({"id": case_id, "mismatches": mismatches})

    return {
        "status": "pass" if not failures else "fail",
        "cases": len(cases),
        "passed": len(cases) - len(failures),
        "failed": len(failures),
        "critical": sum(bool(case.get("critical")) for case in cases),
        "mode_counts": dict(sorted(mode_counts.items())),
        "closure_counts": dict(sorted(closure_counts.items())),
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the isolated RESSE policy engine.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    args = parser.parse_args()
    result = evaluate_cases(load_cases(args.cases))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
