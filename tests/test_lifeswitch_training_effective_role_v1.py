from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SQL_DIR = ROOT / "ops" / "sql"
PROJECTION = SQL_DIR / "20260731_lifeswitch_training_effective_role_v1.sql"
PROJECTION_ROLLBACK = (
    SQL_DIR / "20260731_lifeswitch_training_effective_role_v1_rollback.sql"
)
CHAT_FOLLOWUP = (
    SQL_DIR / "20260731_lifeswitch_training_effective_role_v1_chat_followup.sql"
)
CHAT_ROLLBACK = (
    SQL_DIR
    / "20260731_lifeswitch_training_effective_role_v1_chat_followup_rollback.sql"
)
PLAN_CONTEXT = ROOT / "seebx" / "adapters" / "plan_observation_postgres.py"
TRAINING_ROUTER = ROOT / "seebx" / "capabilities" / "training" / "routes.py"
TRAINING_SESSIONS = ROOT / "seebx" / "adapters" / "lifeswitch_training_sessions_postgres.py"


class TrainingEffectiveRoleV1ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.projection = PROJECTION.read_text(encoding="utf-8").lower()
        cls.projection_flat = re.sub(r"\s+", " ", cls.projection)
        cls.projection_rollback = PROJECTION_ROLLBACK.read_text(
            encoding="utf-8"
        ).lower()
        cls.chat = CHAT_FOLLOWUP.read_text(encoding="utf-8").lower()
        cls.chat_rollback = CHAT_ROLLBACK.read_text(encoding="utf-8").lower()
        cls.plan = PLAN_CONTEXT.read_text(encoding="utf-8").lower()
        cls.router = TRAINING_ROUTER.read_text(encoding="utf-8").lower()
        cls.sessions = TRAINING_SESSIONS.read_text(encoding="utf-8").lower()

    def test_projection_is_owner_paired_and_non_mutating(self) -> None:
        self.assertIn(
            "session.owner_user_id=log.owner_user_id", self.projection_flat
        )
        self.assertIn(
            "role_event.owner_user_id=session.owner_user_id",
            self.projection_flat,
        )
        for forbidden in ("update ", "delete ", "insert ", "truncate "):
            self.assertNotIn(forbidden, self.projection)

    def test_conflict_detection_precedes_resolution(self) -> None:
        effective_case = self.projection.index("end as effective_role")
        conflict = self.projection.index(
            "when evidence.distinct_role_count > 1 then 'unknown'"
        )
        capture = self.projection.index("candidates.capture_role", conflict)
        exercise = self.projection.index(
            "candidates.exercise_role_snapshot", capture
        )
        event = self.projection.index(
            "candidates.training_session_role_event_role", exercise
        )
        workout = self.projection.index(
            "candidates.workout_role_snapshot", event
        )
        self.assertLess(conflict, effective_case)
        self.assertLess(capture, exercise)
        self.assertLess(exercise, event)
        self.assertLess(event, workout)
        self.assertIn("then 'conflict'", self.projection)
        self.assertIn("else 'unresolved'", self.projection)

    def test_mutable_roles_are_not_candidates(self) -> None:
        self.assertNotIn("my_exercise", self.projection)
        self.assertNotIn("workout_template", self.projection)
        self.assertIn("training_session_role_event", self.projection)
        self.assertIn("workout_role_snapshot", self.projection)

    def test_projection_privileges_are_least_privilege(self) -> None:
        self.assertIn("security_invoker=true", self.projection_flat)
        self.assertIn(
            "from public,lifeswitch_chat_reader_v1,"
            "lifeswitch_chat_binding_writer_v1",
            self.projection_flat,
        )
        self.assertIn(
            "grant select on lifeswitch_training."
            "training_set_effective_role_v1 to brains_app",
            self.projection_flat,
        )

    def test_chat_followup_consumes_only_the_canonical_projection(self) -> None:
        self.assertEqual(
            self.chat.count(
                "join lifeswitch_training.training_set_effective_role_v1"
            ),
            2,
        )
        self.assertNotIn("nullif(log.capture_role", self.chat)
        self.assertNotIn("my_exercise", self.chat)
        self.assertNotIn("workout_template", self.chat)
        self.assertIn("role_resolution.effective_role='strength'", self.chat)
        self.assertIn("role_resolution.effective_role='unknown'", self.chat)

    def test_followup_preserves_function_signatures_and_reader_boundary(self) -> None:
        normalized = re.sub(r"\s+", " ", self.chat)
        for signature in (
            "read_resistance_sessions_v1(uuid,date,date)",
            "read_exercise_progression_v1(uuid,date,date,text)",
        ):
            self.assertIn(signature, normalized)
        self.assertIn("security definer", self.chat)
        self.assertIn("set search_path=''", self.chat)
        self.assertNotRegex(
            normalized,
            r"grant select .* to lifeswitch_chat_reader_v1",
        )

    def test_plan_and_router_reference_projection_not_fallback_expression(self) -> None:
        self.assertIn("training_set_effective_role_v1", self.plan)
        self.assertIn("training_set_effective_role_v1", self.sessions)
        fallback = (
            "coalesce(nullif(l.capture_role, 'unknown'), "
            "nullif(l.exercise_role_snapshot, 'unknown')"
        )
        self.assertNotIn(fallback, self.sessions)

    def test_set_wire_keeps_raw_fields_and_adds_effective_provenance(self) -> None:
        self.assertIn("l.exercise_role_snapshot, l.capture_role", self.sessions)
        self.assertIn(
            "coalesce(l.capture_role, l.exercise_role_snapshot, 'unknown') "
            "as exercise_role",
            self.sessions,
        )
        self.assertIn("role_resolution.effective_role", self.sessions)
        self.assertIn("role_resolution.resolution_source", self.sessions)
        self.assertIn("role_resolution.role_conflict", self.sessions)

    def test_rollbacks_are_bounded_and_restore_raw_gateway_logic(self) -> None:
        self.assertIn(
            "drop view lifeswitch_training.training_set_effective_role_v1",
            self.projection_rollback,
        )
        self.assertNotIn("drop table", self.projection_rollback)
        self.assertIn("log.capture_role='strength'", self.chat_rollback)
        self.assertNotIn(
            "training_set_effective_role_v1", self.chat_rollback
        )


if __name__ == "__main__":
    unittest.main()
