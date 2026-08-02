from __future__ import annotations

import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).parents[1]
MIGRATION = ROOT / "governed-migrations" / "lifeswitch_prior_answer_provenance_v1"


class LifeSwitchPriorAnswerProvenanceSqlV1Tests(unittest.TestCase):
    def test_forward_is_append_only_owner_scoped_and_least_privilege(self) -> None:
        sql = (MIGRATION / "forward.pgsql").read_text(encoding="utf-8").lower()
        self.assertIn("force row level security", sql)
        self.assertIn("authenticated_actor_user_id=owner_user_id", sql)
        self.assertIn("security_barrier=true", sql)
        self.assertIn("backend_pid=pg_catalog.pg_backend_pid()", sql)
        self.assertIn("current_setting('app.user_id',true)", sql)
        self.assertIn("current_setting('app.lifeswitch_owner_id',true)", sql)
        self.assertIn("l.source='backend/resse:assistant:v1'", sql)
        self.assertIn("public.digest(l.text,'sha256')", sql)
        self.assertIn("a.request_id_sha256=b.request_id_sha256", sql)
        self.assertIn(
            "a.conversation_snapshot_sha256=b.conversation_snapshot_sha256",
            sql,
        )
        self.assertIn("a.created_at=b.created_at", sql)
        self.assertIn("grant select,insert", sql)
        self.assertNotIn("grant all", sql)
        self.assertNotIn("to public", sql)
        self.assertNotIn("security definer", sql)
        self.assertNotIn("insert into", sql)

    def test_package_is_production_governed_and_rollback_is_exactly_bounded(self) -> None:
        package = json.loads((MIGRATION / "package.json").read_text(encoding="utf-8"))
        rollback = (MIGRATION / "rollback.pgsql").read_text(encoding="utf-8").lower()
        self.assertFalse(package["ci_only"])
        self.assertEqual(package["lane"], "lifeswitch_live_context")
        self.assertFalse(package["policy"]["data_operations_allowed"])
        self.assertIn("drop view lifeswitch_chat.read_prior_answer_lifeswitch_provenance_v1", rollback)
        self.assertIn("drop table lifeswitch_chat.final_answer_lifeswitch_provenance_receipt_v1", rollback)


if __name__ == "__main__":
    unittest.main()
