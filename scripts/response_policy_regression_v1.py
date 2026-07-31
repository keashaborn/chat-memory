#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys
from typing import Any

import httpx
from openai import APITimeoutError, OpenAI


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag_engine.response_policy_prompt_v0_2 import (  # noqa: E402
    render_response_policy_prompt_v0_2,
)
from rag_engine.response_policy_v0_2 import (  # noqa: E402
    ConversationRole,
    ResponsePolicyConversationMessageV0_2,
    ResponsePolicyInputV0_2,
    SafetyAssessmentV0_2,
    decide_response_policy_v0_2,
)
from rag_engine.server_response_signal_classifier_v0_2 import (  # noqa: E402
    OpenAIServerResponseSignalClassifierV0_2,
)


SCHEMA_VERSION = "response_policy_regression_v1"
DEFAULT_CASES = ROOT / "evals" / "response_policy_regression_v1_cases.jsonl"
DEFAULT_MODEL = "gpt-5.1"
_SAFETY_IDENTIFIER = "vs1_" + "9" * 60


def _provider_output(values: dict[str, Any] | None = None) -> dict[str, Any]:
    output: dict[str, Any] = {
        "domain_risk_gate": "pass",
        "categories": [],
        "safety_action_required": False,
        "fm_application_gate": "pass",
        "technical": False,
        "fm_explicit": False,
        "coaching": False,
        "ordinary_fm_relevant": False,
        "user_fm_opt_out": False,
        "technical_procedure_requested": False,
        "coaching_consent": False,
        "specific_experiment_consent": False,
        "experiment_reversible_and_proportionate": False,
        "experiment_measurement_defined": False,
        "experiment_adverse_indicators_defined": False,
        "experiment_stop_rule_defined": False,
        "direct_response_requested": False,
        "guided_reflection_requested": False,
        "behavioral_intervention_requested": False,
        "user_declines_questions": False,
        "material_clarification_required": False,
        "explicit_next_step_requested": False,
    }
    output.update(values or {})
    return output


class _FakeResponses:
    def __init__(
        self,
        *,
        model: str,
        kind: str,
        values: dict[str, Any] | None,
    ) -> None:
        self._model = model
        self._kind = kind
        self._values = values
        self.calls = 0

    def parse(self, **_: Any) -> dict[str, Any]:
        self.calls += 1
        if self._kind == "timeout":
            raise APITimeoutError(
                request=httpx.Request(
                    "POST",
                    "https://api.openai.com/v1/responses",
                )
            )
        if self._kind == "error":
            raise RuntimeError("synthetic_provider_contract_error")
        return {
            "status": "completed",
            "error": None,
            "incomplete_details": None,
            "id": f"resp_regression_{self.calls:03d}",
            "model": self._model,
            "output_parsed": _provider_output(self._values),
        }


class _FakeClient:
    def __init__(
        self,
        *,
        model: str,
        kind: str,
        values: dict[str, Any] | None,
    ) -> None:
        self.responses = _FakeResponses(
            model=model,
            kind=kind,
            values=values,
        )

    def with_options(self, **_: Any) -> "_FakeClient":
        return self


def load_cases(path: Path = DEFAULT_CASES) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        1,
    ):
        if not line.strip():
            continue
        case = json.loads(line)
        if case.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"line {line_number}: unsupported schema")
        case_id = str(case.get("id") or "").strip()
        if not case_id:
            raise ValueError(f"line {line_number}: missing id")
        if case_id in seen:
            raise ValueError(f"line {line_number}: duplicate id {case_id}")
        seen.add(case_id)
        if not isinstance(case.get("messages"), list) or not case["messages"]:
            raise ValueError(f"line {line_number}: messages are required")
        if not isinstance(case.get("expected"), dict):
            raise ValueError(f"line {line_number}: expected is required")
        cases.append(case)
    return cases


def _policy_input(case: dict[str, Any], *, repeat: int) -> ResponsePolicyInputV0_2:
    return ResponsePolicyInputV0_2.create(
        request_id=f"rp-reg-{case['id'].lower()}-{repeat}",
        conversation=tuple(
            ResponsePolicyConversationMessageV0_2(
                role=ConversationRole(message["role"]),
                content=message["content"],
            )
            for message in case["messages"]
        ),
    )


def _actual(
    case: dict[str, Any],
    *,
    client: Any,
    model: str,
    repeat: int,
) -> tuple[dict[str, Any], str]:
    policy_input = _policy_input(case, repeat=repeat)
    classification = OpenAIServerResponseSignalClassifierV0_2(
        client,
        model=model,
        safety_identifier=_SAFETY_IDENTIFIER,
    ).classify(policy_input)
    decision = decide_response_policy_v0_2(
        policy_input,
        safety_assessment=SafetyAssessmentV0_2.create(policy_input),
        signals=classification.signals,
    )
    prompt = render_response_policy_prompt_v0_2(decision)
    return (
        {
            "assessment_gate": classification.assessment.gate.value,
            "assessment_outcome": classification.assessment.outcome.value,
            "categories": [
                category.value
                for category in classification.assessment.categories
            ],
            "safety_action_required": (
                classification.assessment.safety_action_required
            ),
            "provider_calls": classification.assessment.provider_call_count,
            "mode": decision.response_mode.value,
            "interaction": decision.interaction.value,
            "closure": decision.closure.value,
            "question_policy": decision.question_policy.value,
            "fm": decision.fm_effective_level.value,
            "intervention_authorized": decision.intervention_authorized,
            "fm_ir_020_eligible": decision.fm_ir_020_eligible,
        },
        prompt.content,
    )


def _mismatches(
    case: dict[str, Any],
    actual: dict[str, Any],
    prompt: str,
    *,
    live: bool = False,
) -> dict[str, Any]:
    mismatch: dict[str, Any] = {}
    expected_fields = dict(case["expected"])
    if live:
        expected_fields.update(case.get("live_expected") or {})
    allowed_fields = case.get("live_allowed") or {} if live else {}
    for field, expected in expected_fields.items():
        if field in allowed_fields:
            if actual.get(field) not in allowed_fields[field]:
                mismatch[field] = {
                    "expected_one_of": allowed_fields[field],
                    "actual": actual.get(field),
                }
            continue
        if actual.get(field) != expected:
            mismatch[field] = {
                "expected": expected,
                "actual": actual.get(field),
            }
    prompt_contract = case.get("prompt") or {}
    for value in prompt_contract.get("must_include", []):
        if value not in prompt:
            mismatch[f"prompt_missing:{value}"] = {
                "expected": True,
                "actual": False,
            }
    for value in prompt_contract.get("must_not_include", []):
        if value in prompt:
            mismatch[f"prompt_forbidden:{value}"] = {
                "expected": False,
                "actual": True,
            }
    return mismatch


def evaluate_deterministic(
    cases: list[dict[str, Any]],
    *,
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    lane_counts: Counter[str] = Counter()
    outcome_counts: Counter[str] = Counter()
    for case in cases:
        lane_counts[case["lane"]] += 1
        provider = case["provider"]
        client = _FakeClient(
            model=model,
            kind=provider["kind"],
            values=provider.get("values"),
        )
        actual, prompt = _actual(
            case,
            client=client,
            model=model,
            repeat=1,
        )
        outcome_counts[actual["mode"]] += 1
        mismatch = _mismatches(case, actual, prompt)
        if mismatch:
            failures.append(
                {
                    "id": case["id"],
                    "lane": case["lane"],
                    "critical": bool(case.get("critical")),
                    "known_gap": bool(case.get("known_gap")),
                    "mismatches": mismatch,
                }
            )
    known_failure_ids = sorted(
        failure["id"] for failure in failures if failure["known_gap"]
    )
    unexpected_failure_ids = sorted(
        failure["id"] for failure in failures if not failure["known_gap"]
    )
    resolved_gap_ids = sorted(
        case["id"]
        for case in cases
        if case.get("known_gap")
        and case["id"] not in known_failure_ids
    )
    return {
        "contract_version": SCHEMA_VERSION,
        "execution": "deterministic",
        "model": model,
        "cases": len(cases),
        "critical_cases": sum(bool(case.get("critical")) for case in cases),
        "passed": len(cases) - len(failures),
        "failed": len(failures),
        "known_failure_ids": known_failure_ids,
        "unexpected_failure_ids": unexpected_failure_ids,
        "resolved_gap_ids": resolved_gap_ids,
        "lane_counts": dict(sorted(lane_counts.items())),
        "mode_counts": dict(sorted(outcome_counts.items())),
        "failures": failures,
    }


def evaluate_live_provider(
    cases: list[dict[str, Any]],
    *,
    model: str,
    repeats: int,
    selected_ids: set[str] | None = None,
) -> dict[str, Any]:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is unavailable to the audit process")
    selected = [
        case
        for case in cases
        if case.get("live_provider")
        and (selected_ids is None or case["id"] in selected_ids)
    ]
    if selected_ids is not None:
        missing = sorted(
            selected_ids - {case["id"] for case in selected}
        )
        if missing:
            raise ValueError(
                f"requested live-provider ids are unavailable: {missing}"
            )
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    runs: list[dict[str, Any]] = []
    for repeat in range(1, repeats + 1):
        for case in selected:
            actual, prompt = _actual(
                case,
                client=client,
                model=model,
                repeat=repeat,
            )
            mismatch = _mismatches(case, actual, prompt, live=True)
            runs.append(
                {
                    "id": case["id"],
                    "repeat": repeat,
                    "known_gap": bool(
                        case.get("known_gap") or case.get("known_live_gap")
                    ),
                    "actual": actual,
                    "mismatches": mismatch,
                }
            )
    unstable_ids: list[str] = []
    for case in selected:
        allowed_fields = set((case.get("live_allowed") or {}).keys())
        fingerprints = {
            json.dumps(
                {
                    field: value
                    for field, value in run["actual"].items()
                    if field not in allowed_fields
                },
                sort_keys=True,
            )
            for run in runs
            if run["id"] == case["id"]
        }
        if len(fingerprints) != 1:
            unstable_ids.append(case["id"])
    failures = [run for run in runs if run["mismatches"]]
    known_case_ids = {
        case["id"]
        for case in selected
        if case.get("known_gap") or case.get("known_live_gap")
    }
    failure_ids = {run["id"] for run in failures}
    known_failure_ids = sorted(failure_ids.intersection(known_case_ids))
    unexpected_failure_ids = sorted(failure_ids - known_case_ids)
    known_unstable_ids = sorted(set(unstable_ids).intersection(known_case_ids))
    unexpected_unstable_ids = sorted(set(unstable_ids) - known_case_ids)
    return {
        "contract_version": SCHEMA_VERSION,
        "execution": "live_provider_no_database_write",
        "model": model,
        "store": False,
        "selected_cases": len(selected),
        "repeats": repeats,
        "provider_runs": len(runs),
        "passed_runs": len(runs) - len(failures),
        "failed_runs": len(failures),
        "known_failure_ids": known_failure_ids,
        "unexpected_failure_ids": unexpected_failure_ids,
        "known_unstable_ids": known_unstable_ids,
        "unexpected_unstable_ids": unexpected_unstable_ids,
        "runs": runs,
    }


def _write_result(path: Path | None, result: dict[str, Any]) -> None:
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if path is not None:
        path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the content-free response-policy regression v1 audit."
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--live-provider", action="store_true")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument(
        "--ids",
        help="comma-separated live-provider case ids",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-known-gaps", action="store_true")
    args = parser.parse_args()
    if args.repeats < 1 or args.repeats > 3:
        raise SystemExit("--repeats must be between 1 and 3")
    cases = load_cases(args.cases)
    if args.live_provider:
        result = evaluate_live_provider(
            cases,
            model=args.model,
            repeats=args.repeats,
            selected_ids=(
                {
                    item.strip()
                    for item in args.ids.split(",")
                    if item.strip()
                }
                if args.ids
                else None
            ),
        )
        _write_result(args.output, result)
        if (
            result["unexpected_failure_ids"]
            or result["unexpected_unstable_ids"]
        ):
            return 1
        if (
            result["known_failure_ids"]
            or result["known_unstable_ids"]
        ) and not args.allow_known_gaps:
            return 2
        return 0
    result = evaluate_deterministic(cases, model=args.model)
    _write_result(args.output, result)
    if result["unexpected_failure_ids"]:
        return 1
    if result["known_failure_ids"] and not args.allow_known_gaps:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
