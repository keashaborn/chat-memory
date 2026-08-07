from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "governed-migration-ci/tools"))

from governed_function_migration import load_function_package  # noqa: E402


PACKAGE = (
    ROOT
    / "governed-function-migrations"
    / "memory_openai_extraction_authority_v1"
)
LEDGER = {'catalog_evidence_sha256': 'e18acde5ba79608fa56e63e968e2e0abe02e19e72d0628aa42a55b7f4cfe0c72', 'ledger_id': 'governed-memory-v1-initial-production-baseline', 'ledger_sha256': 'c2195f4d5ea132f6eb82781ca34f7ae12516d81c6eaca3b9488d1f51037da995'}


class OpenAIExactJobClaimTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.package = load_function_package(PACKAGE, LEDGER)

    def test_function_identity_and_acl_are_unchanged(self) -> None:
        function = self.package.functions[0]
        self.assertEqual(function.prior_definition_sha256, "f186cbb0016aabf7c82351aedb2713d4efbe6332448e42224021f0a0720086a6")
        self.assertEqual(
            function.expected_definition_sha256,
            "54d51518feadbba87d531cf9daa44d903761084f0a61041f096232549215b2f5",
        )
        self.assertEqual(function.owner, "memory_v5_extraction_scheduler_maintainer")
        self.assertEqual(function.forward_execute_roles, ("brains_app",))
        self.assertEqual(function.rollback_execute_roles, ("brains_app",))

    def test_exact_target_is_transaction_local_and_content_bound(self) -> None:
        sql = (PACKAGE / "claim_owner_v5_bounded_extraction_job_v1-forward.pgsql").read_text(encoding="utf-8")
        self.assertIn("current_setting('memory.v5_exact_claim_mode',true)", sql)
        self.assertIn("memory_v1_openai_exact_job_claim_v1", sql)
        self.assertIn("AS exact_target_job", sql)
        self.assertIn("exact_target_job.job_id=exact_job", sql)
        self.assertIn("exact_target_job.evidence_content_sha256=exact_content_sha", sql)
        self.assertIn("target_evidence.content_sha256=exact_content_sha", sql)
        self.assertNotIn("AS target_job", sql)
        self.assertEqual(sql.count("AND (NOT exact_target OR ("), 2)
        body = sql.split("AS $function$\n", 1)[1].split("\n$function$;", 1)[0]
        self.assertNotIn("EXECUTE ", body)

    def test_relation_artifacts_add_only_owner_isolated_receipts(self) -> None:
        forward = self.package.relation_forward_bytes.decode("utf-8")
        rollback = self.package.relation_rollback_bytes.decode("utf-8")
        for relation in (
            "memory.openai_provider_request_receipt_v1",
            "memory.openai_provider_completion_receipt_v1",
        ):
            self.assertIn(f"CREATE TABLE {relation}", forward)
            self.assertIn(f"ALTER TABLE {relation} FORCE ROW LEVEL SECURITY", forward)
            self.assertIn(f"CREATE POLICY owner_isolation ON {relation}", forward)
            self.assertIn(f"GRANT SELECT,INSERT ON {relation}", forward)
            self.assertIn(f"DROP TABLE {relation}", rollback)
        self.assertNotIn("GRANT UPDATE", forward)
        self.assertNotIn("GRANT DELETE", forward)
        self.assertNotIn("TO PUBLIC", forward)


if __name__ == "__main__":
    unittest.main()
