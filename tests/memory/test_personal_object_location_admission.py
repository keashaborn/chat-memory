"""Static synthetic contract tests for inactive migration 0007."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (
    ROOT / "governed-memory-migrations"
    / "0007_personal_object_location_admission"
)


class PersonalObjectLocationAdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.forward = (MIGRATION / "forward.pgsql").read_text(encoding="utf-8")
        cls.rollback = (MIGRATION / "rollback.pgsql").read_text(encoding="utf-8")
        cls.package = json.loads(
            (MIGRATION / "package.json").read_text(encoding="utf-8")
        )

    def test_candidate_is_inactive_and_adds_no_provider_calls(self) -> None:
        self.assertEqual(
            self.package["status"],
            "inactive_candidate_pending_live_authorization",
        )
        self.assertFalse(self.package["activation"]["production_authorized"])
        self.assertFalse(
            self.package["activation"]["production_database_applied"]
        )
        self.assertEqual(self.package["policy"]["provider_calls_added"], 0)

    def test_existing_self_path_and_new_reason_are_separate(self) -> None:
        self.assertIn("automatic_low_risk_owner_assertion", self.forward)
        self.assertIn(
            "automatic_low_risk_personal_object_location", self.forward
        )
        self.assertIn("p_subject_entity_type = 'self'", self.forward)
        self.assertIn("p_subject_entity_key = 'self'", self.forward)
        self.assertIn("p_subject_entity_type = 'other'", self.forward)
        self.assertIn("p_predicate = 'location.association'", self.forward)
        self.assertIn("p_object_kind IN ('entity', 'literal')", self.forward)

    def test_new_path_requires_owner_local_entity_and_explicit_first_person_text(self) -> None:
        self.assertIn("^local:[0-9a-f]{64}$", self.forward)
        self.assertRegex(
            self.forward,
            r"i\[\[:space:\]\]\+\(keep\|store\|leave\|put\).*?my",
        )
        self.assertRegex(
            self.forward,
            r"my\[\[:space:\]\]\+.*?\(is\|stays\|remains\)",
        )
        self.assertIn("pg_catalog.octet_length(p_review_excerpt) <= 600", self.forward)
        self.assertIn("pg_catalog.chr(10)", self.forward)
        self.assertIn("pg_catalog.chr(13)", self.forward)

    def test_selector_and_locked_review_recompute_the_same_reason(self) -> None:
        self.assertGreaterEqual(
            self.forward.count(
                "memory_private.automatic_admission_reason("
            ),
            4,
        )
        self.assertIn("automatic_reason :=", self.forward)
        self.assertIn(
            "p_reason_codes IS DISTINCT FROM ARRAY[automatic_reason]::text[]",
            self.forward,
        )
        self.assertIn("AS automatic_reason", self.forward)
        self.assertIn("ARRAY[candidate.automatic_reason]::text[]", self.forward)

    def test_all_existing_risk_gates_remain_in_the_selector(self) -> None:
        for required in (
            "proposal.epistemic_state = 'supported'",
            "proposal.sensitivity = 'ordinary'",
            "AND proposal.projectable",
            "proposal.surface = 'normal'",
            "AND NOT proposal.requires_explicit",
            "evidence.source_kind = 'conversation_message'",
            "evidence.eligibility_decision = 'send_external'",
            "provider_call.state = 'completed'",
            "extraction_job.state = 'completed'",
            "NOT memory_private.owner_source_erasure_active",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.forward)

    def test_rollback_refuses_new_rows_and_restores_old_policy(self) -> None:
        self.assertIn("FROM memory.proposal", self.rollback)
        self.assertIn("FROM memory.claim_evidence", self.rollback)
        self.assertIn(
            "personal object location admission rows prevent schema rollback",
            self.rollback,
        )
        self.assertIn(
            "DROP FUNCTION memory_private.automatic_admission_reason(",
            self.rollback,
        )
        new_reason_occurrences = len(
            re.findall(
                "automatic_low_risk_personal_object_location",
                self.rollback,
            )
        )
        self.assertEqual(new_reason_occurrences, 2)


if __name__ == "__main__":
    unittest.main()
