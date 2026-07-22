from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.memory_v1_predicate_registry_v5_2 import (
    DEFAULT_REGISTRY,
    REGISTRY_CANONICAL_SHA256,
    build_composite,
    emit_install_sql,
    install_rows,
    provider_registry,
)


class PredicateRegistryV52InstallerTest(unittest.TestCase):
    def test_registry_is_hash_locked_and_complete(self) -> None:
        result = build_composite()
        self.assertEqual(
            result["counts"],
            {
                "active_predicate_count": 66,
                "legacy_predicate_count": 19,
                "total_contract_count": 85,
            },
        )
        self.assertTrue(result["hash_bound"])
        self.assertEqual(result["registry_sha256"], REGISTRY_CANONICAL_SHA256)
        self.assertFalse(result["payload"]["runtime_active"])

    def test_hash_drift_fails_closed(self) -> None:
        value = json.loads(DEFAULT_REGISTRY.read_text(encoding="utf-8"))
        value["predicates"][0]["description"] += " changed"
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", dir=DEFAULT_REGISTRY.parent, delete=False
        ) as handle:
            json.dump(value, handle)
            path = Path(handle.name)
        try:
            with self.assertRaisesRegex(AssertionError, "artifact hash mismatch"):
                build_composite(path)
        finally:
            path.unlink()

    def test_install_rows_include_new_semantic_lanes(self) -> None:
        rows = install_rows()
        contracts = {row["predicate"]: row for row in rows["contracts"]}
        self.assertEqual(len(contracts), 85)
        self.assertEqual(len(rows["relationships"]), 41)
        self.assertEqual(len(rows["source_bindings"]), 1)
        self.assertEqual(
            rows["source_bindings"][0]["source_name"], "canonical_registry"
        )
        self.assertEqual(contracts["education.attended"]["object_kind"], "entity")
        self.assertEqual(contracts["employment.worked_for"]["object_kind"], "entity")
        self.assertEqual(contracts["stance.reported"]["object_kind"], "literal")
        self.assertEqual(
            contracts["stance.reported"]["contract"]["projection_classes"],
            ["reported_stance"],
        )

    def test_install_sql_is_deterministic_additive_and_inactive(self) -> None:
        one = emit_install_sql()
        two = emit_install_sql()
        self.assertEqual(one, two)
        self.assertEqual(hashlib.sha256(one.encode()).hexdigest(), hashlib.sha256(two.encode()).hexdigest())
        self.assertIn("'memory_predicate_registry_v5_2'", one)
        self.assertIn("'memory_v1_relational_extraction_v5_2'", one)
        self.assertIn("'proposed', false", one)
        self.assertIn("relationship_predicate_contract_v5_2", one)
        self.assertIn("canonical_registry", one)
        for forbidden in (
            "insert into memory.observation",
            "insert into memory.claim",
            "insert into memory.projection",
        ):
            self.assertNotIn(forbidden, one.lower())

    def test_provider_registry_is_exact_canonical_artifact(self) -> None:
        registry = provider_registry()
        self.assertEqual(registry["registry_version"], "memory_predicate_registry_v5_2")
        self.assertEqual(len(registry["predicates"]), 66)
        self.assertEqual(len(registry["legacy_compatibility"]), 19)
        self.assertFalse(registry["runtime_active"])


if __name__ == "__main__":
    unittest.main()
