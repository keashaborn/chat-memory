
from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "ops" / "retirements" / "20260819_legacy_memory_v1"


class LegacyMemoryRetirementV1Tests(unittest.TestCase):
    def test_retirement_is_blocked_and_contains_no_destructive_sql(self) -> None:
        forward_path = PACKAGE_ROOT / "forward.pgsql"
        forward = forward_path.read_text(encoding="utf-8").lower()
        package = json.loads((PACKAGE_ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["status"], "blocked_pending_reconciliation_and_dependency_proof")
        self.assertFalse(package["scope"]["drop_statements_present"])
        self.assertEqual(hashlib.sha256(forward_path.read_bytes()).hexdigest(), package["forward"]["sha256"])
        self.assertIn("raise exception", forward)
        self.assertNotIn("drop schema", forward)
        self.assertNotIn("delete from", forward)
        self.assertNotIn("truncate", forward)

    def test_obsolete_qdrant_evaluator_is_absent(self) -> None:
        self.assertFalse((ROOT / "bin" / "eval_all_users.sh").exists())


if __name__ == "__main__":
    unittest.main()
