#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from scripts.memory_v1_relational_v5_contract_test import (
    load_jsonl,
    validate_cases,
    validate_manifest,
    validate_schema,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "specs" / "memory_v1_relational_extraction_v5.schema.json"
CASES = ROOT / "evals" / "memory_v1_relational_extraction_v5_cases.jsonl"


class RelationalV5ContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        cls.cases = load_jsonl(CASES)

    def test_checked_in_contract_is_valid(self) -> None:
        validate_schema(copy.deepcopy(self.schema))
        validate_cases(copy.deepcopy(self.cases))

    def test_duplicate_source_fails_closed(self) -> None:
        cases = copy.deepcopy(self.cases)
        cases[1]["source"] = copy.deepcopy(cases[0]["source"])
        with self.assertRaisesRegex(AssertionError, "duplicate job/source"):
            validate_cases(cases)

    def test_source_order_drift_fails_closed(self) -> None:
        cases = copy.deepcopy(self.cases)
        cases[0], cases[1] = cases[1], cases[0]
        cases[0]["case_id"], cases[1]["case_id"] = "v5-01", "v5-02"
        cases[0]["ordinal"], cases[1]["ordinal"] = 1, 2
        with self.assertRaisesRegex(AssertionError, "newest-first"):
            validate_cases(cases)

    def test_owner_field_in_model_schema_fails_closed(self) -> None:
        schema = copy.deepcopy(self.schema)
        schema["properties"]["owner_user_id"] = {"type": "string"}
        with self.assertRaisesRegex(AssertionError, "forbidden authoritative"):
            validate_schema(schema)

    def test_scalar_salience_in_model_schema_fails_closed(self) -> None:
        schema = copy.deepcopy(self.schema)
        schema["$defs"]["observation"]["properties"]["salience"] = {
            "type": "number"
        }
        with self.assertRaisesRegex(AssertionError, "forbidden authoritative"):
            validate_schema(schema)

    def test_model_entity_resolution_action_fails_closed(self) -> None:
        schema = copy.deepcopy(self.schema)
        schema["$defs"]["entity_mention"]["properties"]["resolution_action"] = {
            "enum": ["link_existing"]
        }
        with self.assertRaisesRegex(AssertionError, "forbidden authoritative"):
            validate_schema(schema)

    def test_relative_as_precision_fails_closed(self) -> None:
        schema = copy.deepcopy(self.schema)
        schema["$defs"]["temporal"]["properties"]["precision"]["enum"].append(
            "relative"
        )
        with self.assertRaisesRegex(AssertionError, "source form"):
            validate_schema(schema)

    def test_surface_policy_drift_fails_closed(self) -> None:
        schema = copy.deepcopy(self.schema)
        schema["$defs"]["observation"]["properties"]["surface_policy"]["enum"] = [
            "legacy_policy"
        ]
        with self.assertRaisesRegex(AssertionError, "surface policies drifted"):
            validate_schema(schema)

    def test_manifest_drift_fails_closed(self) -> None:
        manifest = {
            "manifest_version": "test",
            "owner_user_id": "1240822d-ac9a-4096-95aa-e2b24d36ef50",
            "sources": [case["source"] for case in self.cases],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(AssertionError, "manifest digest mismatch"):
                validate_manifest(self.cases, path)


if __name__ == "__main__":
    unittest.main()
