from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "ops/sql/20260723_telemetry_rls_retention_v1.sql"


class TelemetryMigrationV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8").lower()

    def test_forces_owner_scoped_rls(self) -> None:
        self.assertIn("enable row level security", self.sql)
        self.assertIn("force row level security", self.sql)
        self.assertIn("telemetry_owner_policy", self.sql)
        self.assertIn("current_setting('app.user_id',true)", self.sql)
        self.assertIn("to brains_app", self.sql)

    def test_rejects_unowned_future_rows(self) -> None:
        self.assertIn("alter column actor_user_id set not null", self.sql)
        self.assertIn("telemetry_event_actor_user_id_uuid_check", self.sql)
        self.assertIn("revoke all on public.telemetry_event from public", self.sql)

    def test_retention_scope_is_fixed_and_not_public(self) -> None:
        self.assertIn("interval '30 days'", self.sql)
        self.assertIn("interval '90 days'", self.sql)
        self.assertIn("security definer", self.sql)
        self.assertIn(
            "revoke all on function memory.enforce_telemetry_retention_v1() from public",
            self.sql,
        )


if __name__ == "__main__":
    unittest.main()
