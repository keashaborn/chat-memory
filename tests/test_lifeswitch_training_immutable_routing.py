from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "rag_engine" / "lifeswitch_training_router.py"
SERVICE = ROOT / "rag_engine" / "lifeswitch_training_log_service.py"


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

    def test_normal_reads_use_current_roots_and_frozen_roles(self) -> None:
        self.assertGreaterEqual(
            self.router.count("training_session_current_v"),
            5,
        )
        self.assertGreaterEqual(
            self.router.count("conditioning_session_current_v"),
            3,
        )
        frozen_role = (
            "coalesce(l.capture_role, l.exercise_role_snapshot, 'unknown')"
        )
        self.assertGreaterEqual(self.router.count(frozen_role), 10)
        self.assertNotRegex(
            self.router_lower,
            r"coalesce\([^\n]*exercise_role[^\n]*'strength'\)",
        )

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
