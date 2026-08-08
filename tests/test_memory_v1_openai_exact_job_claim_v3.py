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
    / "memory_openai_extraction_authority_v3"
)
V2_PACKAGE = (
    ROOT
    / "governed-function-migrations"
    / "memory_openai_extraction_authority_v2"
)
LEDGER = {
    "catalog_evidence_sha256": "e18acde5ba79608fa56e63e968e2e0abe02e19e72d0628aa42a55b7f4cfe0c72",
    "ledger_id": "governed-memory-v1-initial-production-baseline",
    "ledger_sha256": "c2195f4d5ea132f6eb82781ca34f7ae12516d81c6eaca3b9488d1f51037da995",
}


class OpenAIExactJobClaimV3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.package = load_function_package(PACKAGE, LEDGER)

    def test_successor_identity_hashes_and_acl_are_exact(self) -> None:
        function = self.package.functions[0]
        self.assertEqual(
            self.package.manifest["dependencies"],
            ["memory_openai_extraction_authority_v2"],
        )
        self.assertEqual(
            function.prior_definition_sha256,
            "b5c7d93553e2d1418b3d812b0cefe749f15260b7966a84f2229800cabb5684cf",
        )
        self.assertEqual(
            function.expected_definition_sha256,
            "94f0f62d7ac1899b3746d5fa4dfcf258b4445a3a443fcfd4c61885db98ec0cc5",
        )
        self.assertEqual(function.owner, "memory_v5_extraction_scheduler_maintainer")
        self.assertEqual(function.forward_execute_roles, ("brains_app",))
        self.assertEqual(function.rollback_execute_roles, ("brains_app",))

    def test_request_receipt_collision_is_qualified_at_replay_and_insert(self) -> None:
        sql = (
            PACKAGE / "claim_owner_v5_bounded_extraction_job_v1-forward.pgsql"
        ).read_text(encoding="utf-8")
        self.assertEqual(
            sql.count("<<memory_openai_extraction_authority_v3>>"),
            1,
        )
        insert_qualified = (
            "request_receipt_sha,"
            "memory_openai_extraction_authority_v3.request_receipt,"
            "\n      p_provider_id,p_provider_version"
        )
        replay_qualified = (
            "receipt.request_receipt=\n"
            "               memory_openai_extraction_authority_v3.request_receipt"
        )
        replay_sha_qualified = (
            "receipt.request_sha256=\n"
            "               memory_openai_extraction_authority_v3.request_receipt->>'request_sha256'"
        )
        self.assertEqual(sql.count(insert_qualified), 1)
        self.assertEqual(sql.count(replay_qualified), 1)
        self.assertEqual(sql.count(replay_sha_qualified), 1)
        self.assertNotIn(
            "request_receipt_sha,request_receipt,p_provider_id",
            sql,
        )
        self.assertNotIn("receipt.request_receipt=request_receipt", sql)
        self.assertNotIn(
            "receipt.request_sha256=request_receipt->>'request_sha256'",
            sql,
        )
        self.assertIn(
            "request_receipt_sha256,request_receipt,",
            sql,
        )

    def test_rollback_restores_exact_v2_function_bytes(self) -> None:
        rollback = (
            PACKAGE / "claim_owner_v5_bounded_extraction_job_v1-rollback.pgsql"
        ).read_bytes()
        prior = (
            V2_PACKAGE / "claim_owner_v5_bounded_extraction_job_v1-forward.pgsql"
        ).read_bytes()
        self.assertEqual(rollback, prior)

    def test_relation_artifacts_are_security_preserving_noops(self) -> None:
        expected = (
            b"ALTER TABLE memory.evidence_extraction_job "
            b"FORCE ROW LEVEL SECURITY;\n"
        )
        self.assertEqual(self.package.relation_forward_bytes, expected)
        self.assertEqual(self.package.relation_rollback_bytes, expected)


if __name__ == "__main__":
    unittest.main()
