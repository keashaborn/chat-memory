from __future__ import annotations

import json
import unittest
from uuid import UUID

from scripts.memory_v1_governed_activation_verifier_v1 import (
    ActivationVerificationError,
    parse_activation_configuration,
    verify_owner_activation,
)


OWNER = UUID("11111111-1111-4111-8111-111111111111")
OTHER = UUID("22222222-2222-4222-8222-222222222222")


def _configuration(*, active: str, all_authenticated: str, owners: str = "") -> bytes:
    return (
        "[Service]\n"
        f'Environment="MEMORY_V1_GOVERNED_ACTIVE={active}"\n'
        "Environment=\"MEMORY_V1_GOVERNED_ACTIVE_ALL_AUTHENTICATED="
        f'{all_authenticated}\"\n'
        f'Environment="MEMORY_V1_GOVERNED_ACTIVE_USER_IDS={owners}"\n'
    ).encode("utf-8")


class GovernedActivationVerifierV1Tests(unittest.TestCase):
    def test_all_authenticated_and_explicit_owner_modes_match_runtime_policy(self) -> None:
        universal = verify_owner_activation(
            _configuration(active="1", all_authenticated="1"),
            owner_user_id=OWNER,
        )
        self.assertTrue(universal["owner_allowlisted"])
        self.assertEqual(universal["activation_scope"], "all_authenticated")

        explicit = verify_owner_activation(
            _configuration(
                active="1",
                all_authenticated="0",
                owners=f"{OTHER},{OWNER}",
            ),
            owner_user_id=OWNER,
        )
        self.assertTrue(explicit["owner_allowlisted"])
        self.assertEqual(explicit["activation_scope"], "explicit_owner_list")

        absent = verify_owner_activation(
            _configuration(
                active="1",
                all_authenticated="0",
                owners=str(OTHER),
            ),
            owner_user_id=OWNER,
        )
        self.assertFalse(absent["owner_allowlisted"])

    def test_inactive_master_gate_always_denies(self) -> None:
        value = verify_owner_activation(
            _configuration(
                active="0",
                all_authenticated="1",
                owners=str(OWNER),
            ),
            owner_user_id=OWNER,
        )
        self.assertFalse(value["owner_allowlisted"])
        self.assertFalse(value["governed_active"])
        self.assertEqual(value["activation_scope"], "inactive")

    def test_report_never_emits_owner_or_configuration_values(self) -> None:
        raw = _configuration(
            active="1",
            all_authenticated="0",
            owners=str(OWNER),
        )
        report = verify_owner_activation(raw, owner_user_id=OWNER)
        encoded = json.dumps(report, sort_keys=True)
        self.assertNotIn(str(OWNER), encoded)
        self.assertNotIn("MEMORY_V1_GOVERNED_ACTIVE", encoded)
        self.assertNotIn(raw.decode("utf-8"), encoded)
        self.assertFalse(report["secret_values_emitted"])
        self.assertEqual(report["provider_calls"], 0)

    def test_duplicate_missing_binary_and_malformed_owner_fail_closed(self) -> None:
        duplicate = (
            _configuration(active="1", all_authenticated="0")
            + b'Environment="MEMORY_V1_GOVERNED_ACTIVE=0"\n'
        )
        with self.assertRaises(ActivationVerificationError):
            parse_activation_configuration(duplicate)
        with self.assertRaises(ActivationVerificationError):
            parse_activation_configuration(
                b'[Service]\nEnvironment="MEMORY_V1_GOVERNED_ACTIVE=1"\n'
            )
        with self.assertRaises(ActivationVerificationError):
            verify_owner_activation(
                _configuration(
                    active="1",
                    all_authenticated="0",
                    owners="not-a-uuid",
                ),
                owner_user_id=OWNER,
            )


if __name__ == "__main__":
    unittest.main()
