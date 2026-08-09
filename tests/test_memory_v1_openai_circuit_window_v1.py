from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "governed-migration-ci/tools"))

from governed_function_migration import load_function_package  # noqa: E402


PACKAGE = ROOT / "governed-function-migrations" / "memory_openai_circuit_window_v1"
PREVIOUS = (
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


class OpenAICircuitWindowV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.package = load_function_package(PACKAGE, LEDGER)
        cls.forward = (
            PACKAGE / "claim_owner_v5_bounded_extraction_job_v1-forward.pgsql"
        ).read_text(encoding="utf-8")
        cls.rollback = (
            PACKAGE / "claim_owner_v5_bounded_extraction_job_v1-rollback.pgsql"
        ).read_text(encoding="utf-8")
        cls.previous = (
            PREVIOUS / "claim_owner_v5_bounded_extraction_job_v1-forward.pgsql"
        ).read_text(encoding="utf-8")

    def test_replaces_only_the_current_owner_scoped_function(self) -> None:
        function = self.package.functions[0]
        self.assertEqual(
            function.identity,
            "memory.claim_owner_v5_bounded_extraction_job_v1",
        )
        self.assertEqual(
            function.prior_definition_sha256,
            "8d7e736f01863966e3b2a715eb6a928989cf77f17186e0a913494d8dafbdfe76",
        )
        self.assertEqual(
            function.expected_definition_sha256,
            "f71bccae8e2496969daf06d4657bae71dae70630bcf9a302b033ddd91af2137e",
        )
        self.assertEqual(
            function.owner,
            "memory_v5_extraction_scheduler_maintainer",
        )
        self.assertEqual(function.forward_execute_roles, ("brains_app",))
        self.assertEqual(function.rollback_execute_roles, ("brains_app",))

    def test_completion_failures_are_bounded_by_the_requested_window(self) -> None:
        completion_query = self.forward.split(
            "FROM memory.v5_extraction_call_event AS event", 2
        )[2].split(") AS recent;", 1)[0]
        self.assertIn("event.action='completed'", completion_query)
        self.assertIn("event.created_at>=", completion_query)
        self.assertIn(
            "clock_timestamp()-make_interval(secs=>p_rolling_window_seconds)",
            completion_query,
        )
        self.assertIn("LIMIT p_failure_threshold", completion_query)

    def test_rollback_is_the_exact_current_function(self) -> None:
        self.assertEqual(self.rollback, self.previous)

    def test_only_the_completion_window_changes(self) -> None:
        predicate = (
            "      AND event.created_at>=\n"
            "        clock_timestamp()-make_interval(secs=>p_rolling_window_seconds)\n"
        )
        self.assertEqual(self.forward.replace(predicate, "", 1), self.previous)

    def test_relation_surface_is_unchanged(self) -> None:
        expected = (
            "ALTER TABLE memory.evidence_extraction_job "
            "FORCE ROW LEVEL SECURITY;\n"
        )
        self.assertEqual(self.package.relation_forward_bytes.decode(), expected)
        self.assertEqual(self.package.relation_rollback_bytes.decode(), expected)


if __name__ == "__main__":
    unittest.main()
