from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "governed-migrations" / "memory_v1_projection_outbox_release_authority_v1"
FUNCTION = ROOT / "governed-function-creations" / "memory_projection_outbox_release_v1"


class ProjectionOutboxReleaseContractV1Tests(unittest.TestCase):
    def test_schema_change_is_narrow_and_rollback_is_data_safe(self) -> None:
        forward = (SCHEMA / "forward.pgsql").read_text(encoding="utf-8")
        rollback = (SCHEMA / "rollback.pgsql").read_text(encoding="utf-8")
        self.assertIn("release_projection_outbox_v1", forward)
        self.assertNotIn("GRANT UPDATE", forward.upper())
        self.assertNotIn("DELETE FROM", forward.upper())
        self.assertNotIn("TRUNCATE", forward.upper())
        self.assertIn("DROP CONSTRAINT relational_operation_request_operation_check", rollback)
        self.assertNotIn("release_projection_outbox_v1'::text", rollback)

    def test_function_binds_exact_item_hash_owner_and_current_claim(self) -> None:
        sql = "\n".join(
            (
                (
                    FUNCTION / "release_owner_projection_outbox_v1-forward.pgsql"
                ).read_text(encoding="utf-8"),
                (
                    FUNCTION
                    / "release_owner_projection_outbox_write_v1-forward.pgsql"
                ).read_text(encoding="utf-8"),
            )
        )
        for contract in (
            "session_user<>'brains_app'",
            "memory.current_actor_user_id()",
            "p_outbox_id",
            "p_claim_id",
            "p_expected_payload_sha256",
            "p_release_manifest_sha256",
            "target.status<>'pending'",
            "target.attempts<>0",
            "target.available_at<>'infinity'::timestamptz",
            "claim.status='supported'",
            "release_projection_outbox_v1",
            "dispatch_scope','exact_item_only'",
        ):
            self.assertIn(contract, sql)
        upper = sql.upper()
        self.assertNotIn("DELETE FROM", upper)
        self.assertNotIn("TRUNCATE", upper)
        self.assertNotIn("EXECUTE FORMAT", upper)

    def test_packages_are_dependency_bound_and_function_is_new_only(self) -> None:
        schema = json.loads((SCHEMA / "package.json").read_text(encoding="utf-8"))
        function = json.loads((FUNCTION / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["lane"], "personal_memory")
        self.assertFalse(schema["policy"]["data_operations_allowed"])
        self.assertEqual(
            function["dependencies"],
            ["memory_v1_projection_outbox_release_authority_v1"],
        )
        self.assertTrue(function["policy"]["new_functions_only"])
        self.assertTrue(function["policy"]["prior_absence_required"])
        self.assertEqual(len(function["functions"]), 2)
        by_id = {item["id"]: item for item in function["functions"]}
        self.assertEqual(
            by_id["memory.release_owner_projection_outbox_v1"]["owner"],
            "memory_v5_writer",
        )
        self.assertEqual(
            by_id["memory.release_owner_projection_outbox_write_v1"][
                "forward_execute_roles"
            ],
            ["memory_v5_writer", "sage"],
        )
        helper = (
            FUNCTION / "release_owner_projection_outbox_write_v1-forward.pgsql"
        ).read_text(encoding="utf-8")
        self.assertIn("actor<>p_actor", helper)
        self.assertIn("value.owner_user_id=actor", helper)
        self.assertNotIn("TO brains_app", helper)


if __name__ == "__main__":
    unittest.main()
