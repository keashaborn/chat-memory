from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "ops" / "retirements" / "20260819_legacy_public_tables_v1"


class LegacyPublicTablesRetirementV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.forward_path = PACKAGE_ROOT / "forward.pgsql"
        cls.forward = cls.forward_path.read_text(encoding="utf-8").lower()
        cls.package = json.loads(
            (PACKAGE_ROOT / "package.json").read_text(encoding="utf-8")
        )

    def test_forward_hash_binds_exact_sql(self) -> None:
        self.assertEqual(
            hashlib.sha256(self.forward_path.read_bytes()).hexdigest(),
            self.package["forward"]["sha256"],
        )

    def test_drop_scope_is_exact_and_non_cascading(self) -> None:
        expected = self.package["scope"]["drop_tables"]
        self.assertEqual(len(expected), 5)
        for table in expected:
            self.assertEqual(self.forward.count(f"drop table {table};"), 1)
        self.assertEqual(self.forward.count("drop table "), 5)
        self.assertNotIn("cascade", self.forward)
        self.assertNotIn("vb_form_", self.forward)
        self.assertNotIn("drop schema", self.forward)

    def test_forward_requires_recovery_and_dependency_proofs(self) -> None:
        for proof in self.package["session_proofs"]:
            self.assertIn(proof, self.forward)
        for gate in (
            "external_function_refs <> 0",
            "external_view_refs <> 0",
            "external_foreign_keys <> 0",
            "target_triggers <> 0",
            "row counts drifted",
        ):
            self.assertIn(gate, self.forward)
        self.assertEqual(self.package["recovery"]["rollback_mode"], "restore_only")
        self.assertFalse(self.package["recovery"]["sql_rollback_available"])


if __name__ == "__main__":
    unittest.main()
