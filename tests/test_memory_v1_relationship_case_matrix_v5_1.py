from __future__ import annotations

import json
import unittest
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evals/memory_v1_relationship_v5_1_cases.jsonl"
REGISTRY = ROOT / "specs/memory_v1_relationship_registry_v5_1.json"


def load_cases() -> list[dict]:
    return [json.loads(line) for line in CASES.read_text().splitlines()]


class RelationshipCaseMatrixV51Test(unittest.TestCase):
    def test_matrix_is_complete_and_deterministic(self) -> None:
        rows = load_cases()
        self.assertEqual(len(rows), 120)
        self.assertEqual(
            [row["case_id"] for row in rows],
            [f"rel-v5_1-{ordinal:03d}" for ordinal in range(1, 121)],
        )
        self.assertEqual(len({row["text"] for row in rows}), len(rows))

    def test_all_expected_predicates_are_governed(self) -> None:
        registry = json.loads(REGISTRY.read_text())
        active = {row["predicate"] for row in registry["predicates"]}
        known_forbidden = set(registry["forbidden_predicates"])
        positive: Counter[str] = Counter()
        forbidden: Counter[str] = Counter()
        for row in load_cases():
            expected = row["expected"]
            required = expected.get("required_predicates", [])
            if expected.get("predicate") is not None:
                required = [expected["predicate"], *required]
            self.assertFalse(set(required) - active, row["case_id"])
            self.assertFalse(
                set(expected.get("forbidden_predicates", []))
                - active
                - known_forbidden,
                row["case_id"],
            )
            positive.update(required)
            forbidden.update(expected.get("forbidden_predicates", []))
        self.assertEqual(set(positive), active)
        self.assertGreaterEqual(sum(forbidden.values()), 50)
        self.assertGreaterEqual(len(forbidden), 25)

    def test_policy_dimensions_are_explicit(self) -> None:
        allowed_outcomes = {
            "defer",
            "extract",
            "extract_multiple",
            "no_relationship",
            "supersede_state",
        }
        allowed_directions = {
            None,
            "mixed",
            "named_to_self",
            "self_to_named",
            "unordered_self_named",
        }
        allowed_temporal = {
            "closed_or_bounded",
            "dynamic_open",
            "event_independent",
            "mixed",
            "none",
            "open",
        }
        for row in load_cases():
            expected = row["expected"]
            self.assertIn(expected["outcome"], allowed_outcomes)
            self.assertIn(expected["edge_direction"], allowed_directions)
            self.assertIn(expected["temporal_expectation"], allowed_temporal)
            self.assertIsInstance(expected["manual_review"], bool)
            self.assertIsInstance(expected["forbidden_predicates"], list)


if __name__ == "__main__":
    unittest.main()
