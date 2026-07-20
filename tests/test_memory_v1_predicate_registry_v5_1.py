from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.memory_v1_predicate_registry_v5_1 import (
    DEFAULT_INTEGRATION,
    build_composite,
    emit_install_sql,
    install_rows,
)


class PredicateRegistryV51IntegrationTest(unittest.TestCase):
    def test_composite_counts_and_runtime_gate(self) -> None:
        result = build_composite()
        self.assertEqual(
            result["counts"],
            {
                "active_predicate_count": 63,
                "legacy_predicate_count": 19,
                "total_contract_count": 82,
            },
        )
        self.assertFalse(result["payload"]["runtime_active"])
        self.assertEqual(
            result["payload"]["unknown_predicate_action"],
            "defer_unregistered_predicate",
        )

    def test_original_registry_is_not_modified(self) -> None:
        result = build_composite()
        bindings = result["payload"]["source_bindings"]
        self.assertEqual(
            bindings["base_registry_canonical_sha256"],
            "c7d6397c804699ae3afa4538fe6c9535b5b1e08c357ae3a18105b81035f5cacc",
        )

    def test_relationship_replacements_are_complete(self) -> None:
        result = build_composite()
        rows = {row["predicate"]: row for row in result["payload"]["active_predicates"]}
        self.assertEqual(
            set(rows["relationship.parent_of"]["subject_entity_types"]),
            {"person", "self"},
        )
        self.assertIn("relationship.spouse_of", rows)
        self.assertIn("social.experiences_tension_with", rows)
        self.assertNotIn("relationship.enemy_of", rows)

    def test_relationship_contracts_remain_entity_typed(self) -> None:
        result = build_composite()
        for row in result["payload"]["active_predicates"]:
            if "relationship_policy" not in row:
                continue
            self.assertIn(
                row["object_contract"],
                {"entity.animal", "entity.person", "entity.person_or_self"},
            )
            self.assertEqual(row["cardinality"], "many")

    def test_social_states_do_not_use_asserted_truth_modality(self) -> None:
        result = build_composite()
        rows = {row["predicate"]: row for row in result["payload"]["active_predicates"]}
        for name, row in rows.items():
            if not name.startswith("social."):
                continue
            self.assertNotIn("asserted", row["modalities"], name)
            self.assertIn("reported_observation", row["modalities"], name)

    def test_new_surface_policies_fail_closed_into_existing_enum(self) -> None:
        result = build_composite()
        allowed = {
            "direct_or_relevant",
            "mention_when_directly_relevant",
            "explicit_recall_only",
        }
        for row in result["payload"]["active_predicates"]:
            if "relationship_policy" in row:
                self.assertLessEqual(set(row["surface_policies"]), allowed)

    def test_source_artifact_drift_fails_closed(self) -> None:
        integration = json.loads(DEFAULT_INTEGRATION.read_text(encoding="utf-8"))
        integration["base_registry"]["artifact_sha256"] = "0" * 64
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", dir=DEFAULT_INTEGRATION.parent, delete=False
        ) as handle:
            json.dump(integration, handle)
            path = Path(handle.name)
        try:
            with self.assertRaisesRegex(AssertionError, "artifact hash mismatch"):
                build_composite(path)
        finally:
            path.unlink()

    def test_result_hash_is_bound(self) -> None:
        result = build_composite()
        self.assertTrue(result["hash_bound"])
        self.assertEqual(len(result["registry_sha256"]), 64)

    def test_install_rows_normalize_storage_contract(self) -> None:
        rows = install_rows()
        contracts = {row["predicate"]: row for row in rows["contracts"]}
        self.assertEqual(len(contracts), 82)
        self.assertEqual(len(rows["relationships"]), 41)
        self.assertEqual(contracts["identity.name_canonical"]["cardinality"], "one")
        self.assertEqual(contracts["relationship.friend_of"]["object_kind"], "entity")
        self.assertEqual(contracts["relationship.friend_of"]["cardinality"], "many")
        self.assertEqual(len(contracts["relationship.friend_of"]["contract_sha256"]), 64)

    def test_generated_migration_is_additive_and_runtime_disabled(self) -> None:
        sql = emit_install_sql()
        self.assertIn("'memory_predicate_registry_v5_1'", sql)
        self.assertIn("'memory_v1_relational_extraction_v5_1'", sql)
        self.assertIn("'proposed', false", sql)
        self.assertIn("relationship_predicate_contract_v5_1", sql)
        self.assertIn("reject_predicate_registry_v5_1_mutation", sql)
        for forbidden in (
            "INSERT INTO memory.observation",
            "INSERT INTO memory.claim",
            "INSERT INTO memory.projection",
        ):
            self.assertNotIn(forbidden, sql.lower())


if __name__ == "__main__":
    unittest.main()
