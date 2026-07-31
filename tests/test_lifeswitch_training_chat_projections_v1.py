from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APPLY = ROOT / "ops/sql/20260731_lifeswitch_training_chat_projections_v1.sql"
ROLLBACK = (
    ROOT / "ops/sql/20260731_lifeswitch_training_chat_projections_v1_rollback.sql"
)


class LifeSwitchTrainingChatProjectionsV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.apply = APPLY.read_text().lower()
        self.flat = re.sub(r"\s+", " ", self.apply)
        self.rollback = ROLLBACK.read_text().lower()

    def test_gateways_are_owner_bound_fixed_path_security_definers(self) -> None:
        for name in (
            "read_exercise_frequency_v1",
            "read_lifting_progression_summary_v1",
        ):
            match = re.search(
                rf"create function lifeswitch_chat\.{name}\b(?P<body>.*?)\$\$;",
                self.apply,
                re.DOTALL,
            )
            self.assertIsNotNone(match, name)
            body = match.group("body")
            self.assertIn("security definer", body)
            self.assertIn("set search_path=''", body)
            self.assertIn("resolve_owner_read_context_v1(p_context_id)", body)
            self.assertIn("session.owner_user_id=v_owner_user_id", body)
            self.assertIn("log.owner_user_id=session.owner_user_id", body)
            self.assertIn("role_resolution.owner_user_id=log.owner_user_id", body)

    def test_gateways_reuse_canonical_roles_and_are_bounded(self) -> None:
        self.assertEqual(
            self.apply.count("training_set_effective_role_v1 role_resolution"),
            2,
        )
        self.assertIn("role_resolution.effective_role='strength'", self.apply)
        self.assertEqual(self.apply.count("limit 12"), 2)
        self.assertEqual(self.apply.count("pg_catalog.left"), 2)
        self.assertIn("(p_end_date-p_start_date)>366", self.flat)
        self.assertNotIn("my_exercise", self.apply)
        self.assertNotIn("workout_template", self.apply)

    def test_reader_role_has_execute_only(self) -> None:
        self.assertIn(
            "grant execute on function lifeswitch_chat.read_exercise_frequency_v1",
            self.flat,
        )
        self.assertIn(
            "grant execute on function lifeswitch_chat.read_lifting_progression_summary_v1",
            self.flat,
        )
        self.assertIn("from public,brains_app", self.flat)
        self.assertNotIn("grant select", self.apply)

    def test_rollback_removes_only_new_gateways(self) -> None:
        self.assertIn("drop function lifeswitch_chat.read_exercise_frequency_v1", self.rollback)
        self.assertIn(
            "drop function lifeswitch_chat.read_lifting_progression_summary_v1",
            self.rollback,
        )
        self.assertNotIn("drop table", self.rollback)
        self.assertNotIn("drop view", self.rollback)
        self.assertNotIn("memory.", self.rollback)
        self.assertNotIn("qdrant", self.rollback)


if __name__ == "__main__":
    unittest.main()
