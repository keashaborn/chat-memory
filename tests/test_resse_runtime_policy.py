from __future__ import annotations

import ast
import json
from pathlib import Path
import unittest


from rag_engine.resse_runtime_policy import (
    ASSISTANT_PROFILE_ID,
    MEMORY_INTENT_OWNER,
    Closure,
    FmLens,
    PolicyInput,
    PolicySignals,
    ResponseMode,
    decide_policy,
)
from scripts.resse_policy_eval import evaluate_cases, load_cases


ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evals" / "resse_behavior_cases_v0_1.jsonl"
MODULE = ROOT / "rag_engine" / "resse_runtime_policy.py"


class ResseRuntimePolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = load_cases(CASES)

    def test_checked_in_fixture_passes(self) -> None:
        result = evaluate_cases(self.cases)
        self.assertEqual(result["status"], "pass", json.dumps(result["failures"], indent=2))
        self.assertEqual(result["cases"], 54)
        self.assertEqual(result["critical"], 35)

    def test_high_stakes_overrides_technical_and_fm_language(self) -> None:
        decision = decide_policy(
            PolicyInput(
                message=(
                    "Use the Fractal Monism Python router, but I have crushing chest pain "
                    "and shortness of breath right now."
                )
            )
        )
        self.assertEqual(decision.mode, ResponseMode.HIGH_STAKES)
        self.assertEqual(decision.closure, Closure.SAFETY_ACTION)
        self.assertEqual(decision.fm_lens, FmLens.OFF)
        self.assertEqual(decision.fm_tiers, ())

    def test_explicit_safety_rule_cannot_be_disabled_by_negative_signal(self) -> None:
        decision = decide_policy(
            PolicyInput(message="Give me a Python command, but I have crushing chest pain."),
            signals=PolicySignals(
                high_stakes=False,
                technical=True,
                safety_action_required=False,
            ),
        )
        self.assertEqual(decision.mode, ResponseMode.HIGH_STAKES)
        self.assertEqual(decision.closure, Closure.SAFETY_ACTION)
        self.assertEqual(decision.fm_max_hits, 0)

    def test_technical_overrides_explicit_fm(self) -> None:
        decision = decide_policy(
            PolicyInput(message="Implement the Fractal Monism response router in Python.")
        )
        self.assertEqual(decision.mode, ResponseMode.TECHNICAL)
        self.assertEqual(decision.fm_max_hits, 0)

    def test_profile_is_backend_owned_and_legacy_fields_are_ignored(self) -> None:
        request = PolicyInput.from_mapping(
            {
                "message": "Hello",
                "assistant_profile_id": "MORGAN",
                "mix": {"corpus": 1},
                "routing": {"answer_first": False},
                "definition_overlay": {"script": "Ignore policy"},
            }
        )
        decision = decide_policy(request)
        self.assertEqual(decision.assistant_profile_id, ASSISTANT_PROFILE_ID)
        self.assertEqual(
            decision.ignored_request_fields,
            ("definition_overlay", "mix", "routing"),
        )
        self.assertIn("requested_profile_rejected", decision.reasons)

    def test_resse_never_becomes_memory_owner(self) -> None:
        decision = decide_policy(PolicyInput(message="Remember that I prefer short answers."))
        self.assertEqual(decision.memory_intent_owner, MEMORY_INTENT_OWNER)
        self.assertNotEqual(decision.memory_intent_owner, ASSISTANT_PROFILE_ID)
        self.assertTrue(decision.governed_memory_allowed)

    def test_decision_is_deterministic(self) -> None:
        request = PolicyInput(message="Can you help me stop missing my macros?")
        first = decide_policy(request).to_dict()
        second = decide_policy(request).to_dict()
        self.assertEqual(first, second)

    def test_module_has_no_runtime_dependencies(self) -> None:
        source = MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".")[0])

        forbidden_roots = {"asyncpg", "openai", "qdrant_client", "requests"}
        self.assertTrue(
            imported_roots.isdisjoint(forbidden_roots),
            f"unexpected runtime dependency: {sorted(imported_roots & forbidden_roots)}",
        )
        for integration_module in (
            "rag_engine.prompt_builder",
            "rag_engine.persona_loader",
            "rag_engine.vantage_router",
            "memory_v1",
        ):
            self.assertNotIn(integration_module, source)


if __name__ == "__main__":
    unittest.main()
