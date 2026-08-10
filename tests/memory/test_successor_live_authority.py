from __future__ import annotations

import unittest

from rag_engine.governed_memory.http_service import SUPABASE_ISSUER_ENV
from rag_engine.governed_memory.runtime.application import SUPABASE_API_KEY_ENV
from rag_engine.governed_memory.runtime.live_supabase import (
    LiveSupabaseAuthorityVerifier,
)
from rag_engine.governed_memory.successor_live_authority import (
    SuccessorLiveAuthorityConfigurationError,
    successor_live_authority_from_environment,
)


class SuccessorLiveAuthorityFactoryTests(unittest.TestCase):
    def test_construction_is_inert_and_contains_both_live_checks(self) -> None:
        verifier = successor_live_authority_from_environment(
            {
                SUPABASE_ISSUER_ENV: "https://synthetic.supabase.co/auth/v1",
                SUPABASE_API_KEY_ENV: "synthetic-publishable-key",
            }
        )
        self.assertIsInstance(verifier, LiveSupabaseAuthorityVerifier)
        self.assertEqual(
            verifier.user_verifier.config,
            verifier.session_verifier.config,
        )

    def test_missing_or_nonexact_configuration_fails_closed(self) -> None:
        for environment in (
            {},
            {
                SUPABASE_ISSUER_ENV: "http://synthetic.supabase.co/auth/v1",
                SUPABASE_API_KEY_ENV: "synthetic-publishable-key",
            },
            {
                SUPABASE_ISSUER_ENV: "https://synthetic.supabase.co/auth/v1",
                SUPABASE_API_KEY_ENV: " synthetic-publishable-key",
            },
        ):
            with self.subTest(environment=environment):
                with self.assertRaisesRegex(
                    SuccessorLiveAuthorityConfigurationError,
                    "successor_live_authority_unconfigured",
                ):
                    successor_live_authority_from_environment(environment)


if __name__ == "__main__":
    unittest.main()
