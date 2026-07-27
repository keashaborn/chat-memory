from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "ops" / "sql" / "20260727_lifeswitch_recovery_adjustment_v1.sql"


class RecoveryAdjustmentMigrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")
        cls.lower = cls.sql.lower()

    def test_isolated_history_preserving_table(self) -> None:
        self.assertRegex(
            self.lower,
            r"create table\s+lifeswitch_agentic\.recovery_adjustments\s*\(",
        )
        self.assertNotIn("on delete cascade", self.lower)
        self.assertNotIn("grant all", self.lower)
        self.assertIn("recovery_adjustments_protect_history", self.lower)
        self.assertIn("history-preserving", self.lower)

    def test_periods_and_owner_queries_are_indexed(self) -> None:
        self.assertIn("recovery_adjustments_owner_history_idx", self.lower)
        self.assertIn("recovery_adjustments_owner_nutrition_period_idx", self.lower)
        self.assertIn("recovery_adjustments_owner_strength_period_idx", self.lower)
        for domain in ("nutrition", "strength"):
            self.assertRegex(
                self.lower,
                rf"{domain}_ends_on\s*>=\s*{domain}_starts_on",
            )

    def test_stop_lifecycle_is_atomic(self) -> None:
        self.assertRegex(
            self.lower,
            r"num_nonnulls\s*\(\s*stopped_on,\s*stopped_by_actor_user_id,\s*stopped_at\s*\)\s+in\s+\(0,\s*3\)",
        )


if __name__ == "__main__":
    unittest.main()
