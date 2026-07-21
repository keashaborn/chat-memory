from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "ops" / "sql" / "20260726_lifeswitch_training_workout_role.sql"
WRITER = ROOT / "ops" / "sql" / "20260723_lifeswitch_training_writer_api.sql"
ROUTER = ROOT / "rag_engine" / "lifeswitch_training_router.py"


class TrainingWorkoutRoleContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8").lower()
        cls.writer = WRITER.read_text(encoding="utf-8").lower()
        cls.router = ROUTER.read_text(encoding="utf-8").lower()

    def test_migration_adds_bounded_roles_and_append_only_audit(self) -> None:
        self.assertIn("add column if not exists workout_role text", self.sql)
        self.assertIn("workout_role in ('strength', 'rehab')", self.sql)
        self.assertIn("add column if not exists workout_role_snapshot text", self.sql)
        self.assertIn("create table if not exists lifeswitch_training.workout_template_role_event", self.sql)
        self.assertIn("create table if not exists lifeswitch_training.training_session_role_event", self.sql)
        self.assertIn("workout role events are append-only", self.sql)
        for forbidden in ("delete from", "drop table", "truncate "):
            self.assertNotIn(forbidden, self.sql)

    def test_historical_classification_is_explicit_and_non_destructive(self) -> None:
        self.assertIn("classify_unclassified_training_sessions", self.sql)
        self.assertIn("s.workout_role_snapshot is null", self.sql)
        self.assertIn("not exists (", self.sql)
        self.assertIn("training_session_role_event", self.sql)
        self.assertIn("coalesce(l.capture_role, l.exercise_role_snapshot, 'unknown') in ('strength', 'rehab')", self.sql)
        self.assertNotIn("update lifeswitch_training.training_session", self.sql)

    def test_completed_session_requires_and_snapshots_template_role(self) -> None:
        self.assertIn("workout template must be classified before completing a session", self.writer)
        self.assertIn("workout_role_snapshot", self.writer)
        self.assertIn("v_workout_role", self.writer)

    def test_workouts_api_exposes_role_and_confirmed_historical_action(self) -> None:
        self.assertIn("unclassified_session_count", self.router)
        self.assertIn("/workout_templates/{workout_template_id}/classify_historical_sessions", self.router)
        self.assertIn("set_workout_template_role", self.router)
        self.assertIn("classify_unclassified_training_sessions", self.router)
        self.assertIn("idempotency-key", self.router)

    def test_session_role_precedence_is_deterministic(self) -> None:
        historical = self.router.index("when historical_workout_role in ('strength', 'rehab')")
        snapshot = self.router.index("when workout_role_snapshot in ('strength', 'rehab')")
        derived = self.router.index("when strength_set_count > 0 and rehab_set_count > 0 then 'mixed'")
        self.assertLess(historical, snapshot)
        self.assertLess(snapshot, derived)
        self.assertIn("session_role in ('strength', 'mixed') as counts_toward_strength", self.router)

    def test_session_summary_uses_explicit_session_role_only_as_set_role_fallback(self) -> None:
        strength_fallback = (
            "coalesce(nullif(l.capture_role, 'unknown'), "
            "nullif(l.exercise_role_snapshot, 'unknown'), role_event.assigned_role, "
            "base.workout_role_snapshot, 'unknown')='strength'"
        )
        rehab_fallback = (
            "coalesce(nullif(l.capture_role, 'unknown'), "
            "nullif(l.exercise_role_snapshot, 'unknown'), role_event.assigned_role, "
            "base.workout_role_snapshot, 'unknown')='rehab'"
        )
        self.assertEqual(self.router.count(strength_fallback), 3)
        self.assertEqual(self.router.count(rehab_fallback), 3)


if __name__ == "__main__":
    unittest.main()
