from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any
from uuid import UUID

from rag_engine.assistant_response_preference_compiler_v1 import (
    OpenAIAssistantPreferenceCompilerV1,
)


OWNER = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        value = json.loads(raw)
        if not isinstance(value, dict) or not value.get("case_id"):
            raise ValueError(f"invalid case at line {number}")
        cases.append(value)
    return cases


def normalized_plan(candidate: Any) -> dict[str, Any]:
    return {
        "response_length": (
            candidate.response_length.value if candidate.response_length else None
        ),
        "technical_depth": (
            candidate.technical_depth.value if candidate.technical_depth else None
        ),
        "response_format": (
            candidate.response_format.value if candidate.response_format else None
        ),
        "conversation_style": (
            candidate.conversation_style.value
            if candidate.conversation_style
            else None
        ),
        "rule_ids": sorted(item.value for item in candidate.rule_ids),
        "rejection_codes": sorted(
            item.value for item in candidate.rejected_reason_codes
        ),
    }


def score(case: dict[str, Any], plan: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for field, expected in case.get("expected_settings", {}).items():
        if plan.get(field) != expected:
            failures.append(
                f"{field}: expected {expected!r}, got {plan.get(field)!r}"
            )
    rules = set(plan["rule_ids"])
    required_rules = set(case.get("required_rule_ids", []))
    forbidden_rules = set(case.get("forbidden_rule_ids", []))
    missing_rules = sorted(required_rules - rules)
    extra_forbidden = sorted(forbidden_rules & rules)
    if missing_rules:
        failures.append(f"missing rules: {missing_rules}")
    if extra_forbidden:
        failures.append(f"forbidden rules: {extra_forbidden}")
    rejections = set(plan["rejection_codes"])
    required_rejections = set(case.get("required_rejection_codes", []))
    missing_rejections = sorted(required_rejections - rejections)
    if missing_rejections:
        failures.append(f"missing rejections: {missing_rejections}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path("evals/assistant_preference_compiler_v3_cases.jsonl"),
    )
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--model")
    args = parser.parse_args()
    if args.repeats < 1 or args.repeats > 5:
        parser.error("--repeats must be between 1 and 5")

    compiler = OpenAIAssistantPreferenceCompilerV1(model=args.model)
    case_results: list[dict[str, Any]] = []
    all_passed = True
    for case in load_cases(args.cases):
        plans: list[dict[str, Any]] = []
        failures: list[str] = []
        for repeat in range(args.repeats):
            candidate = compiler.compile(
                owner_user_id=OWNER,
                source_revision=0,
                narrative=str(case["narrative"]),
            )
            plan = normalized_plan(candidate)
            plans.append(plan)
            failures.extend(
                f"run {repeat + 1}: {failure}"
                for failure in score(case, plan)
            )
        stable = all(plan == plans[0] for plan in plans[1:])
        if not stable:
            failures.append("plans differed across repeated compilations")
        passed = not failures
        all_passed = all_passed and passed
        case_results.append(
            {
                "case_id": case["case_id"],
                "passed": passed,
                "stable": stable,
                "failures": failures,
                "plans": plans,
            }
        )

    counts = Counter(
        "passed" if result["passed"] else "failed" for result in case_results
    )
    output = {
        "contract": "assistant_preference_compiler_v3_eval_v1",
        "case_count": len(case_results),
        "repeat_count": args.repeats,
        "passed": counts["passed"],
        "failed": counts["failed"],
        "database_writes": 0,
        "results": case_results,
    }
    json.dump(output, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
