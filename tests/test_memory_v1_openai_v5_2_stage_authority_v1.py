from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "governed-migration-ci/tools"))

from governed_function_migration import load_function_package  # noqa: E402


PACKAGE_ID = "memory_openai_v5_2_stage_authority_v1"
PACKAGE = ROOT / "governed-function-migrations" / PACKAGE_ID
LEDGER = {
    "ledger_id": "governed-memory-v1-initial-production-baseline",
    "ledger_sha256": "c2195f4d5ea132f6eb82781ca34f7ae12516d81c6eaca3b9488d1f51037da995",
    "catalog_evidence_sha256": "e18acde5ba79608fa56e63e968e2e0abe02e19e72d0628aa42a55b7f4cfe0c72",
}
EXPECTED_FUNCTION_HASHES = {
    "memory.guard_v5_2_terminal_evidence_from_stage_v1": "a78fcabebd44ff84eb91e5d8e5437de70508c32eba7cafab1e8c9382412821c0",
    "memory.persist_owner_evidence_extraction_packet_v5": "433200bac57acb47e0f9b289d3d7782880310fbcb7081a7e7a6056ce3b461ba4",
    "memory.plan_owner_v5_2_exact_packet_route_v1": "2e53382112bd37eeceaef8b4b1a3438f7462723f7e29e60d9c1116bd312cd1a9",
    "memory.record_owner_v5_2_review_route_v1": "5f4e594e9b12bb70b4a33ceba592c499ce8c8a714bb58453fe1b607c74a4f5b7",
}


class OpenAIV52StageAuthorityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.package = load_function_package(PACKAGE, LEDGER)

    def test_package_binds_the_calibrated_function_definitions(self) -> None:
        self.assertEqual(
            {item.identity: item.expected_definition_sha256 for item in self.package.functions},
            EXPECTED_FUNCTION_HASHES,
        )
        self.assertTrue(
            all(item.prior_definition_sha256 != item.expected_definition_sha256 for item in self.package.functions)
        )

    def test_function_execution_roles_remain_narrow(self) -> None:
        roles = {item.identity: item.forward_execute_roles for item in self.package.functions}
        self.assertEqual(roles["memory.guard_v5_2_terminal_evidence_from_stage_v1"], ())
        self.assertEqual(roles["memory.persist_owner_evidence_extraction_packet_v5"], ("brains_app",))
        self.assertEqual(roles["memory.record_owner_v5_2_review_route_v1"], ("brains_app",))
        self.assertEqual(
            roles["memory.plan_owner_v5_2_exact_packet_route_v1"],
            ("brains_app", "memory_v5_2_local_router_maintainer"),
        )

    def test_relation_delta_is_forced_rls_and_denies_application_table_access(self) -> None:
        sql = self.package.relation_forward_bytes.decode("utf-8")
        self.assertIn("ALTER TABLE memory.v5_2_openai_packet_route_event FORCE ROW LEVEL SECURITY;", sql)
        self.assertIn("CREATE POLICY owner_isolation ON memory.v5_2_openai_packet_route_event", sql)
        self.assertNotIn(" TO brains_app", sql)
        self.assertNotIn(" TO PUBLIC", sql)
        self.assertNotIn("GRANT UPDATE", sql)
        self.assertNotIn("GRANT DELETE", sql)

    def test_stage_guard_requires_exact_immutable_packet_provenance(self) -> None:
        guard = (PACKAGE / "guard_v5_2_terminal_evidence_from_stage_v1-forward.pgsql").read_text(
            encoding="utf-8"
        )
        self.assertIn("extraction IS NOT DISTINCT FROM source_packet.normalized_packet", guard)
        self.assertIn("source_packet.validator_packet_sha256", guard)
        self.assertIn("source_packet.provider_id<>'openai_responses'", guard)
        self.assertIn("source_packet.external_model_calls<>1", guard)
        self.assertIn("memory.current_actor_user_id() IS DISTINCT FROM NEW.owner_user_id", guard)


if __name__ == "__main__":
    unittest.main()
