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
    / "memory_openai_eligibility_disposition_v2"
)
LEDGER = {
    "catalog_evidence_sha256": (
        "e18acde5ba79608fa56e63e968e2e0abe02e19e72d0628aa42a55b7f4cfe0c72"
    ),
    "ledger_id": "governed-memory-v1-initial-production-baseline",
    "ledger_sha256": (
        "c2195f4d5ea132f6eb82781ca34f7ae12516d81c6eaca3b9488d1f51037da995"
    ),
}


class OpenAIEligibilityDispositionV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.package = load_function_package(PACKAGE, LEDGER)
        cls.forward = (
            PACKAGE / "claim_owner_v5_bounded_extraction_job_v1-forward.pgsql"
        ).read_text(encoding="utf-8")
        cls.rollback = (
            PACKAGE / "claim_owner_v5_bounded_extraction_job_v1-rollback.pgsql"
        ).read_text(encoding="utf-8")

    def test_replaces_only_the_existing_owner_scoped_function(self) -> None:
        function = self.package.functions[0]
        self.assertEqual(
            function.identity,
            "memory.claim_owner_v5_bounded_extraction_job_v1",
        )
        self.assertEqual(
            function.prior_definition_sha256,
            "54d51518feadbba87d531cf9daa44d903761084f0a61041f096232549215b2f5",
        )
        self.assertEqual(
            function.owner,
            "memory_v5_extraction_scheduler_maintainer",
        )
        self.assertEqual(function.forward_execute_roles, ("brains_app",))
        self.assertEqual(function.rollback_execute_roles, ("brains_app",))

    def test_all_non_send_decisions_are_durable_and_zero_call(self) -> None:
        for decision in (
            "skip_zero_call",
            "review_context",
            "route_internal",
            "block_local",
        ):
            self.assertIn(f"'{decision}'", self.forward)
        self.assertIn(
            "memory_v1_openai_eligibility_disposition_v2",
            self.forward,
        )
        self.assertIn("'external_model_calls',0", self.forward)
        self.assertIn("'reason_codes',prefilter_receipt->'reason_codes'", self.forward)
        self.assertIn("disposition_target_status", self.forward)
        self.assertIn("'review_required'", self.forward)
        self.assertIn("'eligibility_disposition_only',true", self.forward)
        self.assertIn("'parent_operation_id',p_operation_id", self.forward)
        self.assertIn("'external_model_calls',0", self.forward)
        self.assertNotIn("attempts=extraction_job.attempts+1", self.forward.split(
            "IF prefilter_mode='memory_v1_openai_eligibility_disposition_v2'", 1
        )[1].split("IF finalize_mode=", 1)[0])

    def test_receipts_bind_complete_identity_and_lineage(self) -> None:
        for field in (
            "owner_user_id",
            "evidence_id",
            "job_id",
            "exchange_id",
            "evidence_content_sha256",
            "gate_result_sha256",
            "policy_sha256",
            "lineage_sha256",
            "context_envelope_sha256",
            "source_gate_sha256",
        ):
            self.assertIn(field, self.forward)
        self.assertIn("eligibility disposition replay conflicts", self.forward)
        function_body = self.forward.split("AS $function$\n", 1)[1].split(
            "\n$function$;", 1
        )[0]
        self.assertNotIn("EXECUTE ", function_body)

    def test_rollback_is_the_exact_current_v1_function(self) -> None:
        current = (
            ROOT
            / "governed-function-migrations"
            / "memory_openai_extraction_authority_v1"
            / "claim_owner_v5_bounded_extraction_job_v1-forward.pgsql"
        ).read_text(encoding="utf-8")
        self.assertEqual(self.rollback, current)

    def test_relation_surface_is_unchanged(self) -> None:
        expected = (
            "ALTER TABLE memory.evidence_extraction_job "
            "FORCE ROW LEVEL SECURITY;\n"
        )
        self.assertEqual(self.package.relation_forward_bytes.decode(), expected)
        self.assertEqual(self.package.relation_rollback_bytes.decode(), expected)


if __name__ == "__main__":
    unittest.main()
