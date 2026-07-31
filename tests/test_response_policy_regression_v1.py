from __future__ import annotations

import ast
from pathlib import Path
import unittest

from scripts.response_policy_regression_v1 import (
    DEFAULT_CASES,
    evaluate_deterministic,
    load_cases,
)


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "response_policy_regression_v1.py"


class ResponsePolicyRegressionV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = load_cases(DEFAULT_CASES)
        cls.result = evaluate_deterministic(cls.cases)

    def test_catalog_is_versioned_unique_and_bounded(self) -> None:
        self.assertEqual(len(self.cases), 52)
        self.assertEqual(
            len({case["id"] for case in self.cases}),
            len(self.cases),
        )
        self.assertEqual(
            sum(bool(case.get("live_provider")) for case in self.cases),
            35,
        )
        self.assertTrue(
            all(
                case["critical"]
                for case in self.cases
                if case["id"].startswith("SAFE-")
            )
        )

    def test_hardened_runtime_resolves_all_catalog_requirements(self) -> None:
        self.assertEqual(self.result["unexpected_failure_ids"], [])
        self.assertEqual(self.result["known_failure_ids"], [])
        self.assertEqual(self.result["resolved_gap_ids"], [])
        self.assertEqual(self.result["passed"], 52)

    def test_live_provider_expectations_have_no_accepted_gap(self) -> None:
        self.assertEqual(
            sorted(
                case["id"]
                for case in self.cases
                if case.get("known_live_gap")
            ),
            [],
        )
        for case in self.cases:
            for field, allowed in (case.get("live_allowed") or {}).items():
                self.assertIn(field, case["expected"])
                self.assertIsInstance(allowed, list)
                self.assertGreaterEqual(len(allowed), 1)

    def test_runner_has_no_database_qdrant_or_runtime_mutation_dependency(self) -> None:
        tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(
                    alias.name.split(".")[0] for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".")[0])
        self.assertTrue(
            imported_roots.isdisjoint(
                {
                    "asyncpg",
                    "psycopg",
                    "psycopg2",
                    "qdrant_client",
                    "requests",
                }
            )
        )
        source = RUNNER.read_text(encoding="utf-8")
        for forbidden in (
            "INSERT ",
            "UPDATE ",
            "DELETE ",
            "/response/query",
            "/vantage/query",
        ):
            self.assertNotIn(forbidden, source)

    def test_audit_output_is_content_free(self) -> None:
        rendered = str(self.result)
        for case in self.cases:
            for message in case["messages"]:
                self.assertNotIn(message["content"], rendered)


if __name__ == "__main__":
    unittest.main()
