from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "ops/migrations/20260820_conversation_source_v2_view_v1"
)
LEGACY_SOURCE = "backend/resse:assistant:v1"
CANONICAL_SOURCE = "backend/seebx:assistant:v2"
VIEW = "lifeswitch_chat.read_prior_answer_lifeswitch_provenance_v1"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def select_projection(sql: str) -> str:
    match = re.search(
        rf"create or replace view {re.escape(VIEW)}.*?\bas\s+(select.*?)"
        r"\s+from lifeswitch_chat\.owner_read_context_v1",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        raise AssertionError("view projection is absent")
    return " ".join(match.group(1).split())


class ConversationSourceV2ViewMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.package = json.loads(
            (MIGRATION / "package.json").read_text(encoding="utf-8")
        )
        cls.forward = (MIGRATION / "forward.pgsql").read_text(encoding="utf-8")
        cls.rollback = (MIGRATION / "rollback.pgsql").read_text(encoding="utf-8")

    def test_package_binds_exact_sources_and_file_bytes(self) -> None:
        self.assertEqual(self.package["status"], "candidate_not_applied")
        self.assertEqual(self.package["contract"]["historical_source"], LEGACY_SOURCE)
        self.assertEqual(self.package["contract"]["canonical_source"], CANONICAL_SOURCE)
        for action in ("forward", "rollback"):
            record = self.package["inputs"][action]
            self.assertEqual(sha256(ROOT / record["path"]), record["sha256"])
        self.assertFalse(self.package["execution"]["production_database_mutated"])
        self.assertFalse(self.package["execution"]["deployment_authorized"])

    def test_forward_expands_only_the_exact_source_allowlist(self) -> None:
        lowered = self.forward.lower()
        self.assertIn("security_barrier=true", lowered)
        self.assertIn("current_setting('app.user_id',true)", lowered)
        self.assertIn("current_setting('app.lifeswitch_owner_id',true)", lowered)
        self.assertIn(LEGACY_SOURCE, self.forward)
        self.assertIn(CANONICAL_SOURCE, self.forward)
        self.assertIn("revoke all", lowered)
        self.assertNotIn("grant all", lowered)
        for forbidden in ("insert into", "update ", "delete from", "drop table"):
            self.assertNotIn(forbidden, lowered)

    def test_rollback_is_guarded_after_canonical_writes(self) -> None:
        lowered = self.rollback.lower()
        self.assertIn(CANONICAL_SOURCE, self.rollback)
        self.assertIn("raise exception", lowered)
        self.assertIn("expanded historical reader must remain installed", lowered)
        restored_view = lowered.split("create or replace view", 1)[1]
        self.assertIn(LEGACY_SOURCE, restored_view)
        self.assertNotIn(CANONICAL_SOURCE, restored_view)

    def test_forward_and_rollback_preserve_the_exact_projection(self) -> None:
        self.assertEqual(
            select_projection(self.forward),
            select_projection(self.rollback),
        )


if __name__ == "__main__":
    unittest.main()
