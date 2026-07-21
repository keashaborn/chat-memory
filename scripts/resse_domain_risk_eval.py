from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag_engine.response_policy_v0_2 import (
    ConversationRole,
    ResponsePolicyConversationMessageV0_2,
    ResponsePolicyInputV0_2,
    SafetyAssessmentV0_2,
    decide_response_policy_v0_2,
)
from rag_engine.server_response_signal_classifier_v0_2 import (
    OpenAIServerResponseSignalClassifierV0_2,
)


DEFAULT_CASES = ROOT / "evals" / "resse_domain_risk_cases_v0_2.jsonl"
MODEL = "gpt-5.1"


class _Responses:
    def __init__(self, parsed: dict[str, Any]) -> None:
        self.parsed = parsed
        self.calls = 0

    def parse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {
            "status": "completed",
            "error": None,
            "incomplete_details": None,
            "id": f"eval-response-{self.calls}",
            "model": MODEL,
            "output_parsed": self.parsed,
        }


class _Client:
    def __init__(self, parsed: dict[str, Any]) -> None:
        self.responses = _Responses(parsed)

    def with_options(self, **kwargs: Any) -> "_Client":
        return self


def _provider_output(case: dict[str, Any]) -> dict[str, Any]:
    source = case["provider"]
    return {
        "domain_risk_gate": source["gate"],
        "categories": source["categories"],
        "safety_action_required": source["action"],
        "fm_application_gate": (
            "triggered" if source["gate"] == "triggered" else source["gate"]
        ),
        "technical": bool(source.get("technical")),
        "fm_explicit": bool(source.get("fm_explicit")),
        "coaching": bool(source.get("coaching")),
        "ordinary_fm_relevant": bool(source.get("ordinary_fm_relevant")),
        "user_fm_opt_out": bool(source.get("user_fm_opt_out")),
        "technical_procedure_requested": bool(
            source.get("technical_procedure_requested")
        ),
        "coaching_consent": bool(source.get("coaching_consent")),
        "material_clarification_required": bool(
            source.get("material_clarification_required")
        ),
        "explicit_next_step_requested": bool(
            source.get("explicit_next_step_requested")
        ),
    }


def evaluate(path: Path) -> dict[str, Any]:
    cases = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    failures: list[dict[str, Any]] = []
    critical = 0
    for case in cases:
        critical += int(bool(case["critical"]))
        policy_input = ResponsePolicyInputV0_2.create(
            request_id=f"domain-eval-{case['id'].lower()}",
            conversation=tuple(
                ResponsePolicyConversationMessageV0_2(
                    role=ConversationRole(item["role"]),
                    content=item["content"],
                )
                for item in case["messages"]
            ),
        )
        client = _Client(_provider_output(case))
        classification = OpenAIServerResponseSignalClassifierV0_2(
            client,
            model=MODEL,
            safety_identifier="vs1_" + "e" * 60,
        ).classify(policy_input)
        decision = decide_response_policy_v0_2(
            policy_input,
            safety_assessment=SafetyAssessmentV0_2.create(policy_input),
            signals=classification.signals,
        )
        actual = {
            "mode": decision.response_mode.value,
            "fm": decision.fm_effective_level.value,
            "closure": decision.closure.value,
            "provider_calls": client.responses.calls,
        }
        if actual != case["expected"]:
            failures.append(
                {"id": case["id"], "expected": case["expected"], "actual": actual}
            )
    return {
        "cases": len(cases),
        "critical": critical,
        "passed": len(cases) - len(failures),
        "failed": len(failures),
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    args = parser.parse_args()
    result = evaluate(args.cases)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
