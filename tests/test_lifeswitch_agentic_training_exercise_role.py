from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "ops" / "sql" / "20260720_lifeswitch_training_exercise_role.sql"
ROUTER = ROOT / "rag_engine" / "lifeswitch_training_router.py"


class TrainingExerciseRoleContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8").lower()
        cls.router = ROUTER.read_text(encoding="utf-8")

    def test_migration_adds_bounded_roles_without_deleting_logs(self) -> None:
        self.assertIn("add column if not exists exercise_role text", self.sql)
        self.assertIn("exercise_role in ('strength', 'rehab')", self.sql)
        self.assertIn("add column if not exists exercise_role_snapshot text", self.sql)
        self.assertIn("create table if not exists lifeswitch_training.my_exercise_role_event", self.sql)
        self.assertIn("my_exercise_role_event is append-only", self.sql)
        for forbidden in ("delete from", "drop table", "truncate "):
            self.assertNotIn(forbidden, self.sql)

    def test_role_is_server_resolved_when_sets_are_logged(self) -> None:
        self.assertIn("exercise_role must be strength or rehab", self.router)
        self.assertGreaterEqual(self.router.count("select exercise_role"), 2)
        self.assertGreaterEqual(self.router.count("exercise_role_snapshot"), 5)
        self.assertIn("my_exercise_role_event", self.router)
        self.assertNotIn('raw_set.get("exercise_role")', self.router)


if __name__ == "__main__":
    unittest.main()
