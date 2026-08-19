from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "ops" / "retirements" / "20260819_legacy_memory_v1"


class LegacyMemoryRetirementV1Tests(unittest.TestCase):
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

    def test_forward_is_fail_closed_and_exactly_scoped(self) -> None:
        for proof in (
            "lifeswitch.legacy_memory_backup_sha256",
            "lifeswitch.legacy_memory_restore_receipt_sha256",
            "lifeswitch.legacy_memory_cron_retired",
            "conversation_sync_private.zep_turn_outbox",
            "ai_operations.enforce_telemetry_retention_v1()",
        ):
            self.assertIn(proof, self.forward)
        self.assertIn("memory_tables <> 160", self.forward)
        self.assertIn("ingest_tables <> 7", self.forward)
        self.assertIn("external_function_refs <> 0", self.forward)
        self.assertIn("legacy_triggers <> 0", self.forward)
        self.assertIn("nonterminal_ingest <> 0", self.forward)
        self.assertIn("nonterminal_erasure <> 0", self.forward)
        self.assertEqual(
            self.forward.count("drop schema memory_ingest_private cascade;"), 1
        )
        self.assertEqual(self.forward.count("drop schema memory cascade;"), 1)
        self.assertNotIn("drop database", self.forward)
        self.assertNotIn("drop schema public", self.forward)

    def test_package_requires_restore_only_recovery(self) -> None:
        recovery = self.package["recovery"]
        self.assertEqual(recovery["rollback_mode"], "restore_only")
        self.assertFalse(recovery["sql_rollback_available"])
        self.assertTrue(recovery["exact_row_manifest_required"])
        self.assertTrue(
            recovery["restore_into_disposable_database_required"]
        )
        self.assertEqual(
            self.package["scope"]["drop_schemas"],
            ["memory_ingest_private", "memory"],
        )

    def test_obsolete_qdrant_evaluator_is_absent(self) -> None:
        self.assertFalse((ROOT / "bin" / "eval_all_users.sh").exists())
        tracked_scripts = [
            path
            for path in ROOT.rglob("eval_all_users.sh")
            if "docs/history" not in path.as_posix()
        ]
        self.assertEqual(tracked_scripts, [])


if __name__ == "__main__":
    unittest.main()
