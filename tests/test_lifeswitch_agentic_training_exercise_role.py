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

    def test_session_list_publishes_deterministic_role_summaries(self) -> None:
        self.assertIn("as strength_set_count", self.router)
        self.assertIn("as strength_exercise_count", self.router)
        self.assertIn("as strength_volume", self.router)
        self.assertIn("as rehab_set_count", self.router)
        self.assertIn("as rehab_exercise_count", self.router)
        self.assertIn("as rehab_volume", self.router)
        self.assertIn("end as session_role", self.router)
        self.assertIn("as counts_toward_strength", self.router)
        self.assertIn(
            "coalesce(l.exercise_role_snapshot, me.exercise_role, 'strength')",
            self.router,
        )

    def test_session_set_list_resolves_legacy_roles(self) -> None:
        self.assertIn(
            "coalesce(l.exercise_role_snapshot, me.exercise_role, 'strength') as exercise_role",
            self.router,
        )


if __name__ == "__main__":
    unittest.main()
