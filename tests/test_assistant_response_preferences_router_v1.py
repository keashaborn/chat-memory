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


if __name__ == "__main__":
    unittest.main()
