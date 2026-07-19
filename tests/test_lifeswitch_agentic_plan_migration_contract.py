from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "ops" / "sql" / "20260719_lifeswitch_agentic_plan_foundation.sql"


class PlanMigrationContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")
        cls.lower = cls.sql.lower()

    def test_migration_is_isolated_from_memory_schema(self) -> None:
        executable = "\n".join(
            line for line in self.lower.splitlines() if not line.lstrip().startswith("--")
        )
        self.assertNotRegex(executable, r"\bmemory\.")
        self.assertNotIn("qdrant", executable)
        self.assertIn("create schema if not exists lifeswitch_agentic", executable)

    def test_no_destructive_or_cascade_operation(self) -> None:
        forbidden = (
            "drop table",
            "drop schema",
            "delete from",
            "truncate ",
            "on delete cascade",
            "grant all",
        )
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, self.lower)

    def test_required_tables_and_one_active_index_exist(self) -> None:
        for table in (
            "plan_versions",
            "plan_owner_state",
            "plan_revisions",
            "plan_revision_changes",
            "plan_revision_events",
        ):
            with self.subTest(table=table):
                self.assertRegex(
                    self.lower,
                    rf"create table\s+lifeswitch_agentic\.{re.escape(table)}\s*\(",
                )
        self.assertIn("plan_versions_one_active_owner_idx", self.lower)
        self.assertRegex(self.lower, r"where\s+status\s*=\s*'active'")

    def test_history_guards_and_restrictive_foreign_keys_exist(self) -> None:
        self.assertIn("plan_versions_protect_history", self.lower)
        self.assertIn("plan_revisions_protect_lifecycle", self.lower)
        self.assertIn("plan_revision_events_append_only", self.lower)
        references = re.findall(r"references\s+lifeswitch_agentic\.", self.lower)
        restricts = re.findall(r"on delete restrict", self.lower)
        self.assertGreaterEqual(len(references), 7)
        self.assertEqual(len(references), len(restricts))

    def test_initial_plan_and_owner_only_activation_provenance_are_represented(self) -> None:
        self.assertIn("'initial_plan'", self.lower)
        self.assertIn("base_plan_version_id is null", self.lower)
        self.assertIn("'owner_approval'", self.lower)
        self.assertIn("activated_by_actor_user_id is not null", self.lower)


if __name__ == "__main__":
    unittest.main()
