from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "seebx" / "capabilities" / "training" / "routes.py"
SERVICE = ROOT / "seebx" / "adapters" / "lifeswitch_training_writes_postgres.py"


class TrainingImmutableRoutingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.router = ROUTER.read_text(encoding="utf-8")
        cls.router_lower = cls.router.lower()
        cls.service = SERVICE.read_text(encoding="utf-8")
        cls.service_lower = cls.service.lower()

    def test_router_has_no_raw_observation_mutations(self) -> None:
        observation_tables = (
            "training_session",
            "training_set_log",
            "training_set_log_segment",
            "conditioning_session_log",
        )
        for table in observation_tables:
            with self.subTest(table=table):
                self.assertNotRegex(
                    self.router_lower,
                    rf"\b(?:insert\s+into|update|delete\s+from)\s+"
                    rf"(?:\{{schema\}}\.)?{table}\b",
                )

    def test_writes_require_idempotency_and_explicit_units(self) -> None:
        self.assertGreaterEqual(
            self.router.count('Header(..., alias="Idempotency-Key")'),
            4,
        )
        self.assertIn('load_unit not in {"lb", "kg"}', self.router)
        self.assertIn('"distance_value"', self.router)
        self.assertIn('"distance_unit"', self.router)
        self.assertNotIn(
            "name, notes, started_at, finished_at, load_unit, is_active",
            self.router,
        )

    def test_normal_reads_use_current_roots_and_canonical_roles(self) -> None:
        self.assertGreaterEqual(
            self.router.count("training_session_current_v"),
            5,
        )
        self.assertGreaterEqual(
            self.router.count("conditioning_session_current_v"),
            3,
        )
        self.assertGreaterEqual(
            self.router.count("training_set_effective_role_v1"),
            3,
        )
        self.assertNotIn(
            "coalesce(nullif(l.capture_role, 'unknown')",
            self.router,
        )
        self.assertIn(
            "coalesce(l.capture_role, l.exercise_role_snapshot, 'unknown') "
            "as exercise_role",
            self.router,
        )
        self.assertNotRegex(
            self.router_lower,
            r"coalesce\([^\n]*exercise_role[^\n]*'strength'\)",
        )

    def test_session_summary_groups_every_projected_view_column(self) -> None:
        session_rollup = self.router.split("session_rollup as (", 1)[1].split(
            "), classified as (", 1
        )[0]
        grouping = session_rollup.rsplit("group by", 1)[1]
        for projected_column in (
            "s.training_session_id",
            "s.owner_user_id",
            "s.day",
            "s.workout_template_id",
            "s.name",
            "s.notes",
            "s.started_at",
            "s.finished_at",
            "s.is_active",
            "s.created_at",
            "s.updated_at",
            "base.workout_role_snapshot",
            "role_event.assigned_role",
        ):
            self.assertIn(projected_column, grouping)

    def test_completed_child_mutations_are_retired(self) -> None:
        retired_message = (
            "completed sessions are immutable; submit an aggregate correction"
        )
        self.assertGreaterEqual(self.router.count(retired_message), 5)
        self.assertGreaterEqual(self.router.count("status_code=410"), 6)

    def test_service_calls_security_definer_contract_only(self) -> None:
        for writer in (
            "create_training_session",
            "correct_training_session",
            "void_training_session",
            "create_conditioning_session",
            "correct_conditioning_session",
            "void_conditioning_session",
        ):
            with self.subTest(writer=writer):
                self.assertIn(f".{writer}(", self.service)
        self.assertNotRegex(
            self.service_lower,
            r"\b(?:insert\s+into|update|delete\s+from)\b",
        )


if __name__ == "__main__":
    unittest.main()
