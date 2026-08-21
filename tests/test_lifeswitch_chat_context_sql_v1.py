from __future__ import annotations

import unittest
from pathlib import Path
import re


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
        self.assertIn("with inherit false,set true", lowered)
        self.assertIn("pg_catalog.pg_timezone_names", lowered)
        self.assertIn("security definer", lowered)
        self.assertIn("set search_path=''", lowered)
        self.assertIn("session_user <> 'brains_app'", lowered)
        self.assertIn(
            "to brains_app,lifeswitch_chat_reader_v1,lifeswitch_chat_binding_writer_v1",
            re.sub(r"\s+", " ", lowered),
        )
        self.assertIn("pg_catalog.pg_backend_pid()", lowered)
        self.assertIn("expires_at > pg_catalog.clock_timestamp()", lowered)
        self.assertNotIn("memory.", lowered)
        self.assertNotIn("qdrant", lowered)
        self.assertNotIn("grant update", lowered)
        self.assertNotIn("grant delete", lowered)

    def test_reader_has_no_direct_source_access(self) -> None:
        sql = APPLY.read_text().lower()
        self.assertRegex(
            re.sub(r"\s+", " ", sql),
            r"revoke all on all tables in schema lifeswitch_agentic,"
            r"lifeswitch_plan, lifeswitch_nutrition,lifeswitch_training,public "
            r"from lifeswitch_chat_reader_v1",
        )
        self.assertNotRegex(
            sql,
            r"grant\s+select(?:\s*\([^)]*\))?\s+on\s+(?:table\s+)?"
            r"(?:lifeswitch_agentic|lifeswitch_plan|lifeswitch_nutrition|"
            r"lifeswitch_training|public)\.",
        )

    def test_plan_gateway_whitelists_fields_and_excludes_private_document(self) -> None:
        sql = APPLY.read_text().lower()
        gateway = sql.split("create function lifeswitch_chat.read_plan_v1", 1)[1].split("create function lifeswitch_chat.read_nutrition_daily_v1", 1)[0]
        self.assertIn("'canonical_plan'::text", gateway)
        self.assertIn("lifeswitch_plan.plan_profile", gateway)
        self.assertNotIn("lifeswitch_agentic", gateway)
        self.assertNotIn("legacy_fallback", gateway)
        self.assertIn("whitelist_plan_document_v1", sql)
        self.assertIn("'nutrition_targets'", sql)
        self.assertIn("'training_targets'", sql)
        self.assertNotIn("coach_notes", sql)
        self.assertNotIn("source_snapshot", sql)
        self.assertNotIn("monitoring_rules", sql)
        self.assertNotIn("body_state", sql)

    def test_gateway_functions_are_fixed_path_security_definers(self) -> None:
        sql = APPLY.read_text().lower()
        for name in (
            "read_owner_timezone_v1",
            "read_plan_v1",
            "read_nutrition_daily_v1",
            "read_resistance_sessions_v1",
            "read_conditioning_sessions_v1",
            "read_measurement_observations_v1",
            "read_training_day_v1",
            "read_exercise_progression_v1",
        ):
            match = re.search(
                rf"create function lifeswitch_chat\.{name}\b(?P<body>.*?)\$\$;",
                sql,
                re.DOTALL,
            )
            self.assertIsNotNone(match, name)
            body = match.group("body")
            self.assertIn("security definer", body, name)
            self.assertIn("set search_path=''", body, name)

    def test_owner_context_is_opaque_pid_bound_and_short_lived(self) -> None:
        sql = APPLY.read_text().lower()
        self.assertIn("owner_read_context_v1", sql)
        self.assertIn("v_authenticated_owner <> p_owner_user_id", sql)
        self.assertIn("v_lifeswitch_owner <> p_owner_user_id", sql)
        self.assertIn("backend_pid=pg_catalog.pg_backend_pid()", sql)
        self.assertIn("interval '5 minutes'", sql)
        self.assertIn("grant execute on function", sql)

    def test_rollback_removes_only_candidate_objects(self) -> None:
        sql = ROLLBACK.read_text().lower()
        self.assertIn("drop schema if exists lifeswitch_chat", sql)
        self.assertIn("drop function if exists lifeswitch_chat.read_plan_v1", sql)
        self.assertIn("drop table if exists lifeswitch_chat.owner_read_context_v1", sql)
        self.assertNotIn("memory.", sql)
        self.assertNotIn("lifeswitch_nutrition.", sql)
        self.assertNotIn("lifeswitch_training.", sql)


if __name__ == "__main__":
    unittest.main()
