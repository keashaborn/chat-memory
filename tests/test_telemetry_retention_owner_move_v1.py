from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "ops/sql/20260819_telemetry_retention_owner_move_v1.sql"
ROLLBACK = ROOT / "ops/sql/20260819_telemetry_retention_owner_move_v1_rollback.sql"
WORKER = ROOT / "scripts/telemetry_retention_worker.py"


class TelemetryRetentionOwnerMoveV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.migration = MIGRATION.read_text(encoding="utf-8").lower()
        cls.rollback = ROLLBACK.read_text(encoding="utf-8").lower()
        cls.worker = WORKER.read_text(encoding="utf-8").lower()

    def test_forward_migration_uses_canonical_private_owner(self) -> None:
        self.assertIn(
            "function ai_operations.enforce_telemetry_retention_v1()",
            self.migration,
        )
        self.assertIn("security definer", self.migration)
        self.assertIn("set search_path=pg_catalog,public", self.migration)
        self.assertIn(
            "revoke all on function ai_operations.enforce_telemetry_retention_v1()\nfrom public",
            self.migration,
        )
        self.assertIn("to brains_app", self.migration)

    def test_forward_migration_moves_owner_without_executing_retention(self) -> None:
        create_at = self.migration.index(
            "create or replace function ai_operations.enforce_telemetry_retention_v1()"
        )
        drop_at = self.migration.index(
            "drop function memory.enforce_telemetry_retention_v1()"
        )
        self.assertLess(create_at, drop_at)
        self.assertNotIn(
            "select ai_operations.enforce_telemetry_retention_v1()",
            self.migration,
        )

    def test_worker_has_no_legacy_schema_dependency(self) -> None:
        self.assertIn(
            "select ai_operations.enforce_telemetry_retention_v1()",
            self.worker,
        )
        self.assertNotIn(
            "select memory.enforce_telemetry_retention_v1()",
            self.worker,
        )

    def test_rollback_restores_legacy_path_before_removing_canonical(self) -> None:
        restore_at = self.rollback.index(
            "create or replace function memory.enforce_telemetry_retention_v1()"
        )
        drop_at = self.rollback.index(
            "drop function ai_operations.enforce_telemetry_retention_v1()"
        )
        self.assertLess(restore_at, drop_at)


if __name__ == "__main__":
    unittest.main()
