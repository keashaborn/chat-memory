from __future__ import annotations

import base64
import copy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import socket
import subprocess
import unittest
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tools.governed_memory_install import authority


NAMESPACE = "governed-memory.installation.dormant_store_install.v1"
THREAD_ID = "019fe927-8367-7f52-86f2-e2b5b43a2390"
SCOPE_ID = "dormant_store_install-fresh-stores-000001"
NOW = datetime(2026, 8, 11, 12, 5, 0, tzinfo=timezone.utc)
NONCE = "dormant_store_install_nonce_000000000000000000000001"


class InstallationAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
        self.public_key_bytes = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.key_id = hashlib.sha256(self.public_key_bytes).hexdigest()
        self.scope = self._scope()
        self.bundle = self._bundle()
        self.authorization = self._authorization(self.scope)

    @staticmethod
    def _canonical(value: object) -> bytes:
        return authority.canonical_json_bytes(value)

    def _scope(self) -> dict[str, object]:
        return {
            "schema_version": "governed-memory-dormant-install-scope-v2",
            "operation": "dormant_install",
            "authorization_namespace": NAMESPACE,
            "thread_id": THREAD_ID,
            "scope_id": SCOPE_ID,
            "candidate_git_commit": "a" * 40,
            "candidate_git_tree": "b" * 40,
            "package_manifest_sha256": "c" * 64,
            "controller_contract_sha256": "d" * 64,
            "execution_plan_sha256": "e" * 64,
            "exact_targets_sha256": "f" * 64,
            "controller_runtime_receipt_sha256": "9" * 64,
            "source_boundary": {
                "source_postgres_connection_count": 0,
                "source_postgres_read_count": 0,
                "source_postgres_write_count": 0,
                "source_preparation_phase": (
                    "separate_source_preparation_authorization_required"
                ),
            },
            "store_policy": {
                "postgresql": "fresh_isolated_empty",
                "qdrant": "fresh_isolated_empty",
                "legacy_imports_allowed": False,
                "snapshot_restore_allowed": False,
                "unprocessed_prefill_allowed": False,
                "initial_postgresql_user_row_count": 0,
                "initial_qdrant_point_count": 0,
            },
            "secret_policy": {
                "allowed_secret_names": [
                    "GOVERNED_MEMORY_BOOTSTRAP_PASSWORD",
                    "GOVERNED_MEMORY_QDRANT_API_KEY",
                ],
                "forbidden_secret_classes": [
                    "pilot",
                    "provider",
                    "runtime",
                    "service",
                    "supabase",
                ],
                "runtime_secret_count": 0,
                "provider_secret_count": 0,
                "supabase_secret_count": 0,
                "service_secret_count": 0,
                "pilot_secret_count": 0,
                "secret_values_present": False,
            },
        }

    def _bundle(self, *, key_id: str | None = None) -> dict[str, object]:
        return {
            "schema_version": (
                "governed-memory-ed25519-public-key-trust-bundle-v1"
            ),
            "authorization_namespace": NAMESPACE,
            "keys": [
                {
                    "key_id": self.key_id if key_id is None else key_id,
                    "algorithm": "Ed25519",
                    "public_key_base64": base64.b64encode(
                        self.public_key_bytes
                    ).decode("ascii"),
                }
            ],
        }

    def _authorization(
        self,
        scope: dict[str, object],
        *,
        approval_phrase: str | None = None,
        issued_at: str = "2026-08-11T12:00:00Z",
        not_before: str = "2026-08-11T12:00:00Z",
        expires_at: str = "2026-08-11T12:10:00Z",
        key_id: str | None = None,
        single_use: bool = True,
    ) -> dict[str, object]:
        scope_sha256 = authority.canonical_json_sha256(scope)
        signing_key_id = self.key_id if key_id is None else key_id
        payload = {
            "schema_version": (
                "governed-memory-dormant-install-authorization-v1"
            ),
            "authorization_id": "dormant_store_install-auth-000001",
            "authorization_namespace": NAMESPACE,
            "thread_id": THREAD_ID,
            "scope_id": SCOPE_ID,
            "scope_sha256": scope_sha256,
            "key_id": signing_key_id,
            "approval_phrase": (
                f"APPROVE GOVERNED MEMORY DORMANT STORE INSTALL {scope_sha256}"
                if approval_phrase is None
                else approval_phrase
            ),
            "nonce": NONCE,
            "issued_at": issued_at,
            "not_before": not_before,
            "expires_at": expires_at,
            "single_use": single_use,
        }
        signature = self.private_key.sign(self._canonical(payload))
        return {
            "schema_version": (
                "governed-memory-external-authorization-envelope-v1"
            ),
            "payload": payload,
            "signature": {
                "algorithm": "Ed25519",
                "key_id": signing_key_id,
                "value_base64": base64.b64encode(signature).decode("ascii"),
            },
        }

    def _verify(
        self,
        *,
        scope: dict[str, object] | None = None,
        authorization_document: dict[str, object] | None = None,
        bundle: dict[str, object] | None = None,
        now: datetime = NOW,
        nonce_used=lambda _nonce: False,
        expected_namespace: str = NAMESPACE,
        expected_thread_id: str = THREAD_ID,
        expected_scope_id: str = SCOPE_ID,
        expected_key_id: str | None = None,
    ) -> authority.CryptographicallyValidScopeNotExecution:
        actual_scope = self.scope if scope is None else scope
        actual_authorization = (
            self.authorization
            if authorization_document is None
            else authorization_document
        )
        actual_bundle = self.bundle if bundle is None else bundle
        return authority.verify_dormant_install_authority(
            self._canonical(actual_scope),
            self._canonical(actual_authorization),
            self._canonical(actual_bundle),
            expected_namespace=expected_namespace,
            expected_thread_id=expected_thread_id,
            expected_scope_id=expected_scope_id,
            expected_key_id=(
                self.key_id if expected_key_id is None else expected_key_id
            ),
            expected_trust_bundle_sha256=(
                authority.canonical_json_sha256(actual_bundle)
            ),
            now=now,
            nonce_used=nonce_used,
        )

    def _expected_bindings(self) -> authority.DormantInstallExpectedBindings:
        return authority.DormantInstallExpectedBindings(
            candidate_git_commit=str(self.scope["candidate_git_commit"]),
            candidate_git_tree=str(self.scope["candidate_git_tree"]),
            package_manifest_sha256=str(self.scope["package_manifest_sha256"]),
            controller_contract_sha256=str(
                self.scope["controller_contract_sha256"]
            ),
            execution_plan_sha256=str(self.scope["execution_plan_sha256"]),
            exact_targets_sha256=str(self.scope["exact_targets_sha256"]),
            controller_runtime_receipt_sha256=str(
                self.scope["controller_runtime_receipt_sha256"]
            ),
        )

    def _execution_capability(
        self,
        *,
        scope: dict[str, object] | None = None,
        authorization_document: dict[str, object] | None = None,
        expected_bindings: authority.DormantInstallExpectedBindings | None = None,
    ) -> object:
        actual_scope = self.scope if scope is None else scope
        actual_authorization = (
            self.authorization
            if authorization_document is None
            else authorization_document
        )
        return authority.verify_dormant_install_execution_capability(
            self._canonical(actual_scope),
            self._canonical(actual_authorization),
            self._canonical(self.bundle),
            expected_namespace=NAMESPACE,
            expected_thread_id=THREAD_ID,
            expected_scope_id=SCOPE_ID,
            expected_key_id=self.key_id,
            expected_trust_bundle_sha256=authority.canonical_json_sha256(
                self.bundle
            ),
            expected_bindings=(
                self._expected_bindings()
                if expected_bindings is None
                else expected_bindings
            ),
        )

    def test_valid_signature_is_scope_evidence_not_execution(self) -> None:
        observed_nonces: list[str] = []

        def unused(nonce: str) -> bool:
            observed_nonces.append(nonce)
            return False

        result = self._verify(nonce_used=unused)
        self.assertIsInstance(
            result, authority.CryptographicallyValidScopeNotExecution
        )
        self.assertEqual(
            result.result_type,
            "cryptographically_valid_scope_not_execution",
        )
        self.assertEqual(result.operation, "dormant_install")
        self.assertEqual(
            result.scope_sha256,
            authority.canonical_json_sha256(self.scope),
        )
        self.assertEqual(
            result.authorization_sha256,
            authority.canonical_json_sha256(self.authorization),
        )
        self.assertEqual(observed_nonces, [NONCE])
        rendered = json.dumps(asdict(result), sort_keys=True)
        for forbidden in (
            "password",
            "private_key",
            "service_token",
            "supabase_api_key",
            "openai_api_key",
        ):
            self.assertNotIn(forbidden, rendered.lower())
        self.assertNotIn("execute", result.__dataclass_fields__)
        self.assertNotIn("authorized", result.__dataclass_fields__)

    def test_execution_capability_requires_exact_local_bindings(self) -> None:
        capability = self._execution_capability()
        self.assertEqual(
            repr(capability),
            "VerifiedDormantInstallCapability(<content-redacted>)",
        )
        self.assertNotIn(NONCE, repr(capability))

        wrong = authority.DormantInstallExpectedBindings(
            candidate_git_commit="0" * 40,
            candidate_git_tree="b" * 40,
            package_manifest_sha256="c" * 64,
            controller_contract_sha256="d" * 64,
            execution_plan_sha256="e" * 64,
            exact_targets_sha256="f" * 64,
            controller_runtime_receipt_sha256="9" * 64,
        )
        with self.assertRaisesRegex(
            authority.AuthorityVerificationError,
            "authority_local_binding_mismatch",
        ):
            self._execution_capability(expected_bindings=wrong)

    def test_signature_capability_can_be_reminted_after_expiry_for_atomic_resume(
        self,
    ) -> None:
        expired = self._authorization(
            self.scope,
            issued_at="2026-08-11T11:40:00Z",
            not_before="2026-08-11T11:40:00Z",
            expires_at="2026-08-11T11:50:00Z",
        )
        with self.assertRaisesRegex(
            authority.AuthorityVerificationError,
            "authorization_not_current",
        ):
            self._verify(authorization_document=expired)
        capability = self._execution_capability(
            authorization_document=expired,
        )
        self.assertEqual(
            repr(capability),
            "VerifiedDormantInstallCapability(<content-redacted>)",
        )

    def test_verification_has_no_io_command_network_or_clock_dependency(self) -> None:
        with (
            mock.patch("builtins.open", side_effect=AssertionError("file IO")),
            mock.patch.object(
                subprocess, "run", side_effect=AssertionError("command")
            ),
            mock.patch.object(
                socket, "socket", side_effect=AssertionError("network")
            ),
        ):
            self._verify()

    def test_duplicate_and_noncanonical_json_are_rejected(self) -> None:
        scope_raw = self._canonical(self.scope)
        needle = (
            b'"authorization_namespace":"'
            + NAMESPACE.encode("ascii")
            + b'"'
        )
        duplicate = scope_raw.replace(needle, needle + b"," + needle, 1)
        with self.assertRaisesRegex(
            authority.AuthorityVerificationError,
            "authority_json_duplicate_key",
        ):
            authority.verify_dormant_install_authority(
                duplicate,
                self._canonical(self.authorization),
                self._canonical(self.bundle),
                expected_namespace=NAMESPACE,
                expected_thread_id=THREAD_ID,
                expected_scope_id=SCOPE_ID,
                expected_key_id=self.key_id,
                expected_trust_bundle_sha256=(
                    authority.canonical_json_sha256(self.bundle)
                ),
                now=NOW,
                nonce_used=lambda _nonce: False,
            )
        with self.assertRaisesRegex(
            authority.AuthorityVerificationError,
            "authority_json_not_canonical",
        ):
            authority.verify_dormant_install_authority(
                scope_raw + b"\n",
                self._canonical(self.authorization),
                self._canonical(self.bundle),
                expected_namespace=NAMESPACE,
                expected_thread_id=THREAD_ID,
                expected_scope_id=SCOPE_ID,
                expected_key_id=self.key_id,
                expected_trust_bundle_sha256=(
                    authority.canonical_json_sha256(self.bundle)
                ),
                now=NOW,
                nonce_used=lambda _nonce: False,
            )

    def test_exact_namespace_thread_scope_and_key_are_required(self) -> None:
        cases = (
            {"expected_namespace": "governed-memory.wrong"},
            {"expected_thread_id": "wrong-thread"},
            {"expected_scope_id": "wrong-scope"},
            {"expected_key_id": "0" * 64},
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                with self.assertRaises(authority.AuthorityVerificationError):
                    self._verify(**arguments)

        wrong_hash = "0" * 64
        with self.assertRaisesRegex(
            authority.AuthorityVerificationError,
            "trust_bundle_hash_mismatch",
        ):
            authority.verify_dormant_install_authority(
                self._canonical(self.scope),
                self._canonical(self.authorization),
                self._canonical(self.bundle),
                expected_namespace=NAMESPACE,
                expected_thread_id=THREAD_ID,
                expected_scope_id=SCOPE_ID,
                expected_key_id=self.key_id,
                expected_trust_bundle_sha256=wrong_hash,
                now=NOW,
                nonce_used=lambda _nonce: False,
            )

    def test_key_id_is_sha256_of_exact_raw_public_key(self) -> None:
        forged_id = "0" * 64
        bundle = self._bundle(key_id=forged_id)
        authorization_document = self._authorization(
            self.scope, key_id=forged_id
        )
        with self.assertRaisesRegex(
            authority.AuthorityVerificationError,
            "trust_bundle_key_id_invalid",
        ):
            self._verify(
                bundle=bundle,
                authorization_document=authorization_document,
                expected_key_id=forged_id,
            )

        bundle_with_secret = copy.deepcopy(self.bundle)
        bundle_with_secret["private_key_base64"] = "forbidden"
        with self.assertRaisesRegex(
            authority.AuthorityVerificationError, "trust_bundle_invalid"
        ):
            self._verify(bundle=bundle_with_secret)

    def test_phase8a_phrase_tampering_and_bad_signature_are_rejected(self) -> None:
        scope_sha256 = authority.canonical_json_sha256(self.scope)
        phase8a = self._authorization(
            self.scope,
            approval_phrase=(
                f"APPROVE PHASE 8A INACTIVE INSTALLATION {scope_sha256}"
            ),
        )
        with self.assertRaisesRegex(
            authority.AuthorityVerificationError,
            "authorization_approval_phrase_invalid",
        ):
            self._verify(authorization_document=phase8a)

        tampered_scope = copy.deepcopy(self.scope)
        tampered_scope["package_manifest_sha256"] = "1" * 64
        with self.assertRaisesRegex(
            authority.AuthorityVerificationError,
            "authorization_identity_or_binding_mismatch",
        ):
            self._verify(scope=tampered_scope)

        tampered_signature = copy.deepcopy(self.authorization)
        signature = base64.b64decode(
            tampered_signature["signature"]["value_base64"]
        )
        tampered_signature["signature"]["value_base64"] = base64.b64encode(
            bytes([signature[0] ^ 1]) + signature[1:]
        ).decode("ascii")
        with self.assertRaisesRegex(
            authority.AuthorityVerificationError,
            "authorization_signature_invalid",
        ):
            self._verify(authorization_document=tampered_signature)

    def test_validity_single_use_and_replay_are_closed(self) -> None:
        expired = self._authorization(
            self.scope,
            issued_at="2026-08-11T11:40:00Z",
            not_before="2026-08-11T11:40:00Z",
            expires_at="2026-08-11T11:50:00Z",
        )
        future = self._authorization(
            self.scope,
            issued_at="2026-08-11T12:06:00Z",
            not_before="2026-08-11T12:06:00Z",
            expires_at="2026-08-11T12:10:00Z",
        )
        too_long = self._authorization(
            self.scope,
            issued_at="2026-08-11T12:00:00Z",
            not_before="2026-08-11T12:00:00Z",
            expires_at="2026-08-11T12:15:01Z",
        )
        reusable = self._authorization(self.scope, single_use=False)
        for document in (expired, future, too_long, reusable):
            with self.subTest(document=document["payload"]):
                with self.assertRaises(authority.AuthorityVerificationError):
                    self._verify(authorization_document=document)
        with self.assertRaisesRegex(
            authority.AuthorityVerificationError,
            "authorization_nonce_replayed",
        ):
            self._verify(nonce_used=lambda _nonce: True)

    def test_dormant_store_install_scope_cannot_include_source_prep_or_runtime_secrets(self) -> None:
        source_connected = copy.deepcopy(self.scope)
        source_connected["source_boundary"][
            "source_postgres_connection_count"
        ] = 1
        runtime_secret = copy.deepcopy(self.scope)
        runtime_secret["secret_policy"]["runtime_secret_count"] = 1
        provider_name = copy.deepcopy(self.scope)
        provider_name["secret_policy"]["allowed_secret_names"].append(
            "GOVERNED_MEMORY_OPENAI_API_KEY"
        )
        restored_store = copy.deepcopy(self.scope)
        restored_store["store_policy"]["snapshot_restore_allowed"] = True

        for invalid_scope in (
            source_connected,
            runtime_secret,
            provider_name,
            restored_store,
        ):
            with self.subTest(invalid_scope=invalid_scope):
                authorization_document = self._authorization(invalid_scope)
                with self.assertRaises(authority.AuthorityVerificationError):
                    self._verify(
                        scope=invalid_scope,
                        authorization_document=authorization_document,
                    )

    def test_json_numeric_boolean_coercions_are_rejected(self) -> None:
        cases = (
            ("source_boundary", "source_postgres_connection_count", False),
            ("source_boundary", "source_postgres_read_count", 0.0),
            ("store_policy", "initial_qdrant_point_count", False),
            ("store_policy", "legacy_imports_allowed", 0),
            ("secret_policy", "runtime_secret_count", False),
            ("secret_policy", "secret_values_present", 0),
        )
        for section, key, replacement in cases:
            with self.subTest(section=section, key=key, value=replacement):
                invalid_scope = copy.deepcopy(self.scope)
                invalid_scope[section][key] = replacement
                authorization_document = self._authorization(invalid_scope)
                with self.assertRaises(authority.AuthorityVerificationError):
                    self._verify(
                        scope=invalid_scope,
                        authorization_document=authorization_document,
                    )

if __name__ == "__main__":
    unittest.main()
