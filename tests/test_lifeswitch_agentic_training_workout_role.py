from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "ops" / "sql" / "20260726_lifeswitch_training_workout_role.sql"
WRITER = ROOT / "ops" / "sql" / "20260723_lifeswitch_training_writer_api.sql"
ROUTER = ROOT / "seebx" / "capabilities" / "training" / "routes.py"
TEMPLATES = (
    ROOT / "seebx" / "adapters" / "lifeswitch_training_templates_postgres.py"
)
SESSIONS = (
    ROOT / "seebx" / "adapters" / "lifeswitch_training_sessions_postgres.py"
)


class TrainingWorkoutRoleContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8").lower()
        cls.writer = WRITER.read_text(encoding="utf-8").lower()
        cls.router = ROUTER.read_text(encoding="utf-8").lower()
        cls.templates = TEMPLATES.read_text(encoding="utf-8").lower()
        cls.sessions = SESSIONS.read_text(encoding="utf-8").lower()

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
        self.assertIn("unclassified_session_count", self.templates)
        self.assertIn(
            "/workout_templates/{workout_template_id}/classify_historical_sessions",
            self.router,
        )
        self.assertIn("set_workout_template_role", self.templates)
        self.assertIn("classify_unclassified_training_sessions", self.templates)
        self.assertIn("idempotency-key", self.router)

    def test_session_role_is_derived_from_canonical_set_roles(self) -> None:
        classification = self.sessions.split("), classified as (", 1)[1].split(
            "select classified.*", 1
        )[0]
        self.assertNotIn("when historical_workout_role", classification)
        self.assertNotIn("when workout_role_snapshot", classification)
        self.assertIn(
            "when strength_set_count > 0 and rehab_set_count > 0 then 'mixed'",
            classification,
        )
        self.assertIn("role_event.assigned_role as historical_workout_role", self.sessions)
        self.assertIn("base.workout_role_snapshot", self.sessions)
        self.assertIn("session_role in ('strength', 'mixed') as counts_toward_strength", self.sessions)

    def test_session_summary_uses_canonical_effective_role_projection(self) -> None:
        self.assertIn("training_set_effective_role_v1", self.sessions)
        self.assertGreaterEqual(
            self.sessions.count("role_resolution.effective_role='strength'"),
            4,
        )
        self.assertGreaterEqual(
            self.sessions.count("role_resolution.effective_role='rehab'"),
            3,
        )
        self.assertNotIn(
            "coalesce(nullif(l.capture_role, 'unknown')", self.router
        )
        self.assertIn("role_conflict_set_count", self.sessions)
        self.assertIn("unknown_role_set_count", self.sessions)
        self.assertIn("training_session_role_event_resolved_set_count", self.sessions)


if __name__ == "__main__":
    unittest.main()
