from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SearchContractV16SQLTests(unittest.TestCase):
    def test_rls_policy_targets_only_brains_app(self) -> None:
        sql = (
            ROOT
            / "ops/sql/20260731_search_contract_v1_6_rls_hardening.sql"
        ).read_text(encoding="utf-8")
        normalized = " ".join(sql.lower().split())
        self.assertIn("set local lock_timeout = '5s'", normalized)
        self.assertIn("set local statement_timeout = '30s'", normalized)
        self.assertIn(
            "set local idle_in_transaction_session_timeout = '60s'",
            normalized,
        )
        self.assertIn("rolsuper", normalized)
        self.assertIn("rolbypassrls", normalized)
        self.assertIn("relrowsecurity", normalized)
        self.assertIn("relforcerowsecurity", normalized)
        self.assertIn("unexpected response_transcript_v1 grantee", normalized)
        self.assertIn(
            "create policy response_transcript_owner_policy on "
            "trusted_web.response_transcript_v1 to brains_app",
            normalized,
        )
        self.assertIn("current_setting('app.user_id', true)", normalized)
        self.assertIn(
            "revoke all on trusted_web.response_transcript_v1 from public",
            normalized,
        )
        self.assertIn("'anon'", normalized)
        self.assertIn("'authenticated'", normalized)
        self.assertIn("'service_role'", normalized)
        self.assertNotIn("security definer", normalized)

    def test_rollback_is_bounded_and_requires_expected_policy(self) -> None:
        sql = (
            ROOT
            / "ops/sql/20260731_search_contract_v1_6_rls_hardening_rollback.sql"
        ).read_text(encoding="utf-8")
        normalized = " ".join(sql.lower().split())
        self.assertIn("set local lock_timeout = '5s'", normalized)
        self.assertIn("set local statement_timeout = '30s'", normalized)
        self.assertIn(
            "set local idle_in_transaction_session_timeout = '60s'",
            normalized,
        )
        self.assertIn(
            "expected brains_app-only policy is not installed", normalized
        )
        self.assertIn(
            "roles::text[] = array['brains_app']::text[]", normalized
        )
        self.assertIn(
            "create policy response_transcript_owner_policy on "
            "trusted_web.response_transcript_v1 using",
            normalized,
        )
        self.assertIn(
            "revoke all on trusted_web.response_transcript_v1 from public",
            normalized,
        )
        self.assertNotIn("security definer", normalized)

    def test_retention_is_design_only(self) -> None:
        design = (
            ROOT / "docs/SEARCH_CONTRACT_V1_6_CANDIDATE.md"
        ).read_text(encoding="utf-8")
        self.assertIn("No deletion job is included in v1.6.", design)
        self.assertIn("proposed retention is 90 days", design)
        self.assertIn("thread lifecycle", design)


if __name__ == "__main__":
    unittest.main()
