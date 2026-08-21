from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "ops/retirements/20260819_legacy_memory_v1"


class LegacyMemoryRetirementV1Tests(unittest.TestCase):
    def test_retirement_is_executable_but_fail_closed_and_unauthorized(self) -> None:
        forward_path = PACKAGE_ROOT / "forward.pgsql"
        forward = forward_path.read_text(encoding="utf-8").lower()
        package = json.loads((PACKAGE_ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["status"], "candidate_not_applied")
        self.assertFalse(package["execution"]["retirement_authorized"])
        self.assertFalse(package["execution"]["production_database_mutated"])
        self.assertTrue(package["scope"]["drop_statements_present"])
        self.assertEqual(hashlib.sha256(forward_path.read_bytes()).hexdigest(), package["forward"]["sha256"])
        self.assertIn("drop schema memory_ingest_private cascade", forward)
        self.assertIn("drop schema memory cascade", forward)
        for gate in (
            "legacy_memory_backup_sha256", "legacy_memory_restore_receipt_sha256",
            "legacy_attestation_reconciliation_receipt_sha256",
            "legacy_attestation_quarantine_ciphertext_sha256",
            "legacy_attestation_key_custody_receipt_sha256",
            "legacy_memory_dependency_catalog_sha256",
            "legacy memory queues are not terminal",
            "external legacy dependencies remain",
            "pg_identify_object",
            "legacy attestation reconciliation is incomplete",
        ):
            self.assertIn(gate, forward)
        self.assertIn("pg_advisory_xact_lock", forward)
        self.assertTrue(forward.rstrip().endswith("commit;"))

    def test_obsolete_qdrant_evaluator_is_absent(self) -> None:
        self.assertFalse((ROOT / "bin" / "eval_all_users.sh").exists())


if __name__ == "__main__":
    unittest.main()
