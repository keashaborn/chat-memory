from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "ops"
    / "sql"
    / "20260728_lifeswitch_usage_actor_registry_v1.sql"
)
ROLLBACK = (
    ROOT
    / "ops"
    / "sql"
    / "20260728_lifeswitch_usage_actor_registry_v1_rollback.sql"
)


class LifeSwitchUsageActorRegistryV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8").lower()
        cls.rollback = ROLLBACK.read_text(encoding="utf-8").lower()

    def test_registry_is_private_forced_rls_and_admin_read_only(self) -> None:
        self.assertIn(
            "create table lifeswitch_usage.ai_actor_registry_v1",
            self.sql,
        )
        self.assertIn("enable row level security", self.sql)
        self.assertIn("force row level security", self.sql)
        self.assertRegex(
            self.sql,
            re.compile(
                r"create policy ai_actor_registry_v1_admin_select.*?"
                r"to lifeswitch_usage_admin_v1.*?using \(true\)",
                re.DOTALL,
            ),
        )
        self.assertIn(
            "grant select on lifeswitch_usage.ai_actor_registry_v1",
            self.sql,
        )
        self.assertNotIn(
            "grant insert on lifeswitch_usage.ai_actor_registry_v1",
            self.sql,
        )
        self.assertIn(
            "revoke all on lifeswitch_usage.ai_actor_registry_v1 from public",
            self.sql,
        )

    def test_registry_is_content_free_append_only_and_seeds_canary(self) -> None:
        self.assertIn(
            "before update or delete on "
            "lifeswitch_usage.ai_actor_registry_v1",
            self.sql,
        )
        self.assertIn("'synthetic'", self.sql)
        self.assertIn("'system'", self.sql)
        self.assertIn("'voice_synthetic_canary'", self.sql)
        self.assertIn(
            "'1b8daceb-e78d-47a6-bfc0-2d6b8e24b33f'",
            self.sql,
        )
        for forbidden in (
            "prompt",
            "response_text",
            "food",
            "workout",
            "goal",
            "health_measurement",
            "exception_text",
            "client_metadata",
        ):
            self.assertNotIn(forbidden, self.sql)

    def test_rollback_removes_only_registry_objects(self) -> None:
        self.assertIn(
            "drop table if exists lifeswitch_usage.ai_actor_registry_v1",
            self.rollback,
        )
        self.assertNotIn("drop table if exists lifeswitch_usage.ai_usage_event_v1", self.rollback)


if __name__ == "__main__":
    unittest.main()
