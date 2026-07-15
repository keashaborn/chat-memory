#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from scripts.memory_v1_predicate_registry_v5_test import (
    validate_case_coverage,
    validate_registry,
)


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "specs" / "memory_v1_predicate_registry_v5.json"
CASES = ROOT / "evals" / "memory_v1_relational_extraction_v5_cases.jsonl"


class PredicateRegistryV5Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = json.loads(REGISTRY.read_text(encoding="utf-8"))

    def test_checked_in_registry_is_valid(self) -> None:
        registry = copy.deepcopy(self.registry)
        validate_registry(registry)
        validate_case_coverage(registry, CASES)

    def test_owner_field_fails_closed(self) -> None:
        registry = copy.deepcopy(self.registry)
        registry["predicates"][0]["owner_user_id"] = "model-authored"
        with self.assertRaisesRegex(AssertionError, "forbidden authoritative"):
            validate_registry(registry)

    def test_scalar_salience_fails_closed(self) -> None:
        registry = copy.deepcopy(self.registry)
        registry["predicates"][0]["salience"] = 1.0
        with self.assertRaisesRegex(AssertionError, "forbidden authoritative"):
            validate_registry(registry)

    def test_legacy_extraction_fails_closed(self) -> None:
        registry = copy.deepcopy(self.registry)
        registry["legacy_compatibility"][0]["extraction_allowed"] = True
        with self.assertRaisesRegex(AssertionError, "legacy extraction"):
            validate_registry(registry)

    def test_response_preference_content_surface_fails_closed(self) -> None:
        registry = copy.deepcopy(self.registry)
        row = next(
            item
            for item in registry["predicates"]
            if item["predicate"] == "preference.response"
        )
        row["surface_policies"] = ["direct_or_relevant"]
        with self.assertRaisesRegex(AssertionError, "zero-token"):
            validate_registry(registry)

    def test_project_scope_policy_fails_closed(self) -> None:
        registry = copy.deepcopy(self.registry)
        row = next(
            item
            for item in registry["predicates"]
            if item["predicate"] == "project.current_state"
        )
        row["manual_review_rules"] = []
        with self.assertRaisesRegex(AssertionError, "project scope review"):
            validate_registry(registry)

    def test_unregistered_case_predicate_fails_closed(self) -> None:
        registry = copy.deepcopy(self.registry)
        with tempfile.TemporaryDirectory() as directory:
            cases = Path(directory) / "cases.jsonl"
            cases.write_text(
                json.dumps(
                    {
                        "expected": {
                            "required_predicate_families": ["unknown.predicate"]
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(AssertionError, "missing from registry"):
                validate_case_coverage(registry, cases)

    def test_self_object_type_fails_outside_narrow_person_contract(self) -> None:
        registry = copy.deepcopy(self.registry)
        registry["object_contracts"]["entity.person"]["entity_types"].append("self")
        with self.assertRaisesRegex(AssertionError, "self object type is not narrowly governed"):
            validate_registry(registry)


if __name__ == "__main__":
    unittest.main()
