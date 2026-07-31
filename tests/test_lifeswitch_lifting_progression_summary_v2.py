from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APPLY = ROOT / "ops/sql/20260731_lifeswitch_lifting_progression_summary_v2.sql"
ROLLBACK = ROOT / "ops/sql/20260731_lifeswitch_lifting_progression_summary_v2_rollback.sql"


class LifeSwitchLiftingProgressionSummaryV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.apply = APPLY.read_text(encoding="utf-8").lower()
        cls.flat = re.sub(r"\s+", " ", cls.apply)
        cls.rollback = ROLLBACK.read_text(encoding="utf-8").lower()

    def test_gateway_is_additive_owner_bound_and_fixed_path(self) -> None:
        self.assertIn(
            "create function lifeswitch_chat.read_lifting_progression_summary_v2",
            self.flat,
        )
        self.assertIn("security definer", self.apply)
        self.assertIn("set search_path=''", self.apply)
        self.assertIn("resolve_owner_read_context_v1(p_context_id)", self.apply)
        self.assertIn("session.owner_user_id=v_owner_user_id", self.apply)
        self.assertIn("log.owner_user_id=session.owner_user_id", self.apply)
        self.assertIn("role_resolution.owner_user_id=log.owner_user_id", self.apply)
        self.assertNotIn(
            "drop function lifeswitch_chat.read_lifting_progression_summary_v1",
            self.apply,
        )

    def test_gateway_reuses_canonical_role_and_returns_comparison_inputs(self) -> None:
        self.assertEqual(
            self.apply.count("training_set_effective_role_v1 role_resolution"),
            1,
        )
        self.assertIn("role_resolution.effective_role='strength'", self.apply)
        for field in (
            "first_set_count",
            "latest_set_count",
            "first_average_load",
            "latest_average_load",
        ):
            self.assertIn(field, self.apply)
        self.assertIn("limit 12", self.apply)
        self.assertIn("(p_end_date-p_start_date)>366", self.flat)
        self.assertNotIn("my_exercise", self.apply)
        self.assertNotIn("workout_template", self.apply)

    def test_reader_role_has_execute_only(self) -> None:
        self.assertIn(
            "grant execute on function lifeswitch_chat.read_lifting_progression_summary_v2",
            self.flat,
        )
        self.assertIn("from public,brains_app", self.flat)
        self.assertNotIn("grant select", self.apply)

    def test_rollback_removes_only_v2_gateway(self) -> None:
        self.assertIn(
            "drop function lifeswitch_chat.read_lifting_progression_summary_v2",
            self.rollback,
        )
        self.assertNotIn("drop table", self.rollback)
        self.assertNotIn("drop view", self.rollback)
        self.assertNotIn("memory.", self.rollback)
        self.assertNotIn("qdrant", self.rollback)


if __name__ == "__main__":
    unittest.main()
