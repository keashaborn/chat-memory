from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APPLY = ROOT / "ops/sql/20260731_lifeswitch_chat_context_v1.sql"
ROLLBACK = ROOT / "ops/sql/20260731_lifeswitch_chat_context_v1_rollback.sql"


class LifeSwitchChatContextSqlV1Tests(unittest.TestCase):
    def test_apply_is_restricted_and_does_not_touch_memory_or_qdrant(self) -> None:
        sql = APPLY.read_text()
        lowered = sql.lower()
        self.assertIn("lifeswitch_chat_reader_v1 nologin", lowered)
        self.assertIn("lifeswitch_chat_binding_writer_v1 nologin", lowered)
        self.assertIn("enable row level security", lowered)
        self.assertIn("force row level security", lowered)
        self.assertIn("app.lifeswitch_owner_id", lowered)
        self.assertNotIn("memory.", lowered)
        self.assertNotIn("qdrant", lowered)
        self.assertNotIn("grant update", lowered)
        self.assertNotIn("grant delete", lowered)

    def test_apply_omits_free_form_notes_and_raw_snapshots_from_grants(self) -> None:
        sql = APPLY.read_text().lower()
        grant_section = sql.split("create table lifeswitch_chat.final_answer", 1)[0]
        self.assertNotIn("coach_notes", grant_section)
        self.assertNotIn("source_snapshot", grant_section)
        self.assertNotIn("notes", grant_section)

    def test_rollback_removes_only_candidate_objects(self) -> None:
        sql = ROLLBACK.read_text().lower()
        self.assertIn("drop schema if exists lifeswitch_chat", sql)
        self.assertNotIn("memory.", sql)
        self.assertNotIn("lifeswitch_nutrition.", sql)
        self.assertNotIn("lifeswitch_training.", sql)


if __name__ == "__main__":
    unittest.main()
