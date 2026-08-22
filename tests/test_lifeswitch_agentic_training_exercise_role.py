from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "ops" / "sql" / "20260720_lifeswitch_training_exercise_role.sql"
WRITER = ROOT / "ops" / "sql" / "20260723_lifeswitch_training_writer_api.sql"
EXERCISE_ROUTES = ROOT / "seebx" / "capabilities" / "training" / "exercises.py"
SESSION_ROUTES = ROOT / "seebx" / "capabilities" / "training" / "sessions.py"
EXERCISES_ADAPTER = ROOT / "seebx" / "adapters" / "lifeswitch_training_exercises_postgres.py"
SESSIONS_ADAPTER = ROOT / "seebx" / "adapters" / "lifeswitch_training_sessions_postgres.py"


class TrainingExerciseRoleContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8").lower()
        cls.writer = WRITER.read_text(encoding="utf-8").lower()
        cls.exercise_routes = EXERCISE_ROUTES.read_text(encoding="utf-8")
        cls.session_routes = SESSION_ROUTES.read_text(encoding="utf-8")
        cls.exercises_adapter = EXERCISES_ADAPTER.read_text(encoding="utf-8")
        cls.sessions_adapter = SESSIONS_ADAPTER.read_text(encoding="utf-8")

    def test_migration_adds_bounded_roles_without_deleting_logs(self) -> None:
        self.assertIn("add column if not exists exercise_role text", self.sql)
        self.assertIn("exercise_role in ('strength', 'rehab')", self.sql)
        self.assertIn("add column if not exists exercise_role_snapshot text", self.sql)
        self.assertIn("create table if not exists lifeswitch_training.my_exercise_role_event", self.sql)
        self.assertIn("my_exercise_role_event is append-only", self.sql)
        for forbidden in ("delete from", "drop table", "truncate "):
            self.assertNotIn(forbidden, self.sql)

    def test_role_is_server_resolved_when_sets_are_logged(self) -> None:
        self.assertIn(
            "exercise_role must be strength or rehab", self.exercise_routes
        )
        self.assertIn(
            "select to_jsonb(e), e.exercise_role into v_exercise, v_role",
            self.writer,
        )
        self.assertIn("v_role, v_role, v_load_unit", self.writer)
        self.assertIn("my_exercise_role_event", self.exercises_adapter)
        self.assertNotIn('raw_set.get("exercise_role")', self.session_routes)

    def test_session_list_publishes_deterministic_role_summaries(self) -> None:
        self.assertIn("as strength_set_count", self.sessions_adapter)
        self.assertIn("as strength_exercise_count", self.sessions_adapter)
        self.assertIn("as strength_volume", self.sessions_adapter)
        self.assertIn("as rehab_set_count", self.sessions_adapter)
        self.assertIn("as rehab_exercise_count", self.sessions_adapter)
        self.assertIn("as rehab_volume", self.sessions_adapter)
        self.assertIn("end as session_role", self.sessions_adapter)
        self.assertIn("as counts_toward_strength", self.sessions_adapter)
        self.assertIn("training_set_effective_role_v1", self.sessions_adapter)
        self.assertIn("role_resolution.effective_role='strength'", self.sessions_adapter)
        self.assertIn("role_resolution.effective_role='rehab'", self.sessions_adapter)

    def test_session_set_list_preserves_raw_role_and_adds_effective_role(self) -> None:
        self.assertIn(
            "coalesce(l.capture_role, l.exercise_role_snapshot, 'unknown') as exercise_role",
            self.sessions_adapter,
        )
        self.assertIn("role_resolution.effective_role", self.sessions_adapter)
        self.assertIn("role_resolution.resolution_source", self.sessions_adapter)
        self.assertIn("role_resolution.role_conflict", self.sessions_adapter)


if __name__ == "__main__":
    unittest.main()
