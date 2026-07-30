from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class AssistantResponsePreferencesRouterV1Tests(unittest.TestCase):
    def test_api_is_authenticated_owner_scoped_and_postgres_backed(self) -> None:
        source = (
            ROOT / "rag_engine/assistant_response_preferences_router_v1.py"
        ).read_text(encoding="utf-8")
        self.assertIn("require_memory_actor_v1(req, str(owner_user_id))", source)
        self.assertIn("async with conn.transaction()", source)
        self.assertIn("set_preference_actor_v1(conn, owner)", source)
        self.assertNotIn("qdrant", source.lower())
        self.assertNotIn("/cards/", source)
        self.assertNotIn("vantage_id", source)
        self.assertIn('@router.post("/{owner_user_id}/compile")', source)
        self.assertIn('@router.post("/{owner_user_id}/approve")', source)
        self.assertIn("asyncio.to_thread", source)
        self.assertIn("store_compilation_candidate_v1", source)
        self.assertIn("approve_compilation_candidate_v1", source)

    def test_application_mounts_the_neutral_route(self) -> None:
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("assistant_response_preferences_router_v1", source)
        self.assertIn('prefix="/assistant-preferences"', source)

    def test_candidate_migration_enforces_owner_isolation(self) -> None:
        source = (
            ROOT
            / "ops/sql/20260730_assistant_response_preferences_v1.sql"
        ).read_text(encoding="utf-8")
        self.assertIn("ENABLE ROW LEVEL SECURITY", source)
        self.assertIn("FORCE ROW LEVEL SECURITY", source)
        self.assertIn("current_setting('app.user_id',true)", source)
        self.assertIn("GRANT SELECT,INSERT,UPDATE", source)
        self.assertNotIn("memory_raw", source)
        self.assertNotIn("qdrant", source.lower())

    def test_compiler_migration_is_owner_scoped_and_raw_text_is_not_runtime(self) -> None:
        source = (
            ROOT
            / "ops/sql/20260730_assistant_response_preference_compiler_v1.sql"
        ).read_text(encoding="utf-8")
        self.assertIn("ENABLE ROW LEVEL SECURITY", source)
        self.assertIn("FORCE ROW LEVEL SECURITY", source)
        self.assertIn("current_setting('app.user_id',true)", source)
        self.assertIn("preference_narrative", source)
        self.assertIn("assistant-preference-plan-v1:%", source)
        self.assertIn(
            "FOREIGN KEY (owner_user_id,active_compilation_candidate_id)",
            source,
        )
        self.assertNotIn("qdrant", source.lower())
        self.assertNotIn("memory_raw", source)

    def test_compiler_rollback_restores_the_editable_narrative(self) -> None:
        source = (
            ROOT
            / "ops/sql/20260730_assistant_response_preference_compiler_v1_rollback.sql"
        ).read_text(encoding="utf-8")
        restore = source.index(
            "SET custom_instructions=preference_narrative"
        )
        drop = source.index("DROP COLUMN IF EXISTS preference_narrative")
        self.assertLess(restore, drop)

    def test_v2_migration_widens_narrative_without_destructive_rollback(self) -> None:
        migration = (
            ROOT
            / "ops/sql/20260730_assistant_response_preference_compiler_v2.sql"
        ).read_text(encoding="utf-8")
        rollback = (
            ROOT
            / "ops/sql/20260730_assistant_response_preference_compiler_v2_rollback.sql"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "char_length(preference_narrative) BETWEEN 1 AND 8000",
            migration,
        )
        self.assertIn("Never projected directly into an answer prompt", migration)
        self.assertIn("rollback blocked", rollback)
        self.assertIn("char_length(preference_narrative) > 1200", rollback)
        self.assertNotIn("left(preference_narrative", rollback.lower())
        self.assertNotIn("substring(preference_narrative", rollback.lower())
        self.assertIn("char_length(source_narrative) <= 8000", migration)
        self.assertIn("cardinality(rule_ids) <= 8", migration)
        self.assertIn("cardinality(summary) <= 12", migration)
        self.assertIn("'assistant_preference_compiler_v2'", migration)
        self.assertIn("'contextual_playfulness'", migration)
        self.assertIn("'precise_plain_language'", migration)
        self.assertIn("'evidence_first_conclusions'", migration)
        self.assertIn("'information_dense'", migration)
        self.assertIn("v2 preference compilation candidates exist", rollback)
        self.assertIn("an active v2 preference compilation exists", rollback)


if __name__ == "__main__":
    unittest.main()
