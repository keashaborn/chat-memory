from __future__ import annotations

import base64
import copy
import hashlib
import inspect
import json
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tools.governed_memory_install.rollback_authority import (
    AUTHORIZATION_PAYLOAD_SCHEMA_VERSION,
    AUTHORIZATION_SCHEMA_VERSION,
    EmptyRollbackExpectedBindings,
    RECOVERY_DELEGATION_PAYLOAD_SCHEMA_VERSION,
    RECOVERY_DELEGATION_SCHEMA_VERSION,
    RECOVERY_DERIVATION_POLICY,
    RECOVERY_RESERVATION_OPERATION,
    ROLLBACK_OPERATION,
    RollbackAuthorityError,
    SCOPE_SCHEMA_VERSION,
    TRUST_BUNDLE_SCHEMA_VERSION,
    canonical_json_bytes,
    derive_empty_rollback_execution_capability_from_recovery_delegation,
    recovery_delegation_evidence,
    recovery_reservation_binding,
    rollback_capability_evidence,
    verify_empty_rollback_execution_capability,
    verify_empty_rollback_recovery_delegation,
)
from tools.governed_memory_install import authority as install_authority
from tools.governed_memory_install import rollback_authority as rollback_module
from tools.governed_memory_install.authority_state import nonce_sha256
from tools.governed_memory_install.execution_capability import (
    verified_dormant_install_authority_identity,
)
from tools.governed_memory_install.package_capability import (
    _package_capability_parts,
    verified_package_evidence,
    verify_install_package_capability,
)
from tools.governed_memory_install.controller_runtime import (
    EXPECTED_POSTGRESQL_DRIVER_IDENTITY_SHA256,
    VerifiedControllerRuntimeEvidence,
    _RUNTIME_TOKEN,
    _VerifiedControllerRuntimeCapability,
    verified_controller_runtime_evidence,
)


INSTALL_PACKAGE_TEST_NONCE = "install_package_test_nonce_000000000001"
INSTALL_PACKAGE_TEST_NONCE_SHA256 = nonce_sha256(INSTALL_PACKAGE_TEST_NONCE)


def build_verified_install_package(
    *,
    candidate_git_commit: str = "a" * 40,
    candidate_git_tree: str = "b" * 40,
    return_scope_capability: bool = False,
) -> object:
    """Build a real signed install scope and verify every synthetic artifact."""

    canonical = install_authority.canonical_json_bytes
    contract = {"exact_targets": {"stores": "fresh-empty"}}
    plan = {"install_steps": [{"step_id": f"I{index:02d}"} for index in range(1, 20)]}
    artifacts: dict[str, bytes] = {
        "ops/governed_memory/installation/current/contract.json": canonical(contract),
        "ops/governed_memory/installation/current/controller_plan.json": canonical(plan),
        "ops/governed_memory/installation/store_spec.json": canonical(
            {"stores": "fresh-empty"}
        ),
        "tools/governed_memory_install/resource_identity.py": b"synthetic identity implementation\n",
        "ops/governed_memory/installation/current/controller_runtime_contract.json": canonical(
            {"schema_version": "synthetic-controller-runtime-contract-v1"}
        ),
        "ops/governed_memory/controller-requirements.lock": b"synthetic==1.0 --hash=sha256:"
        + b"1" * 64
        + b"\n",
        "tools/governed_memory_install/store_supervisor_launcher.py": (
            b"# synthetic supervisor launcher\n"
        ),
    }
    manifest = {
        "schema_version": "governed-memory-test-package-v1",
        "state": "inactive",
        "artifacts": {
            path: hashlib.sha256(raw).hexdigest()
            for path, raw in artifacts.items()
        },
    }
    manifest_raw = canonical(manifest)
    namespace = "governed-memory.installation.test.v1"
    thread_id = "019fe927-8367-7f52-86f2-e2b5b43a2390"
    scope_id = "dormant-install-package-test-000001"
    scope = {
        "schema_version": install_authority.SCOPE_SCHEMA_VERSION,
        "operation": install_authority.AUTHORIZATION_OPERATION,
        "authorization_namespace": namespace,
        "thread_id": thread_id,
        "scope_id": scope_id,
        "candidate_git_commit": candidate_git_commit,
        "candidate_git_tree": candidate_git_tree,
        "package_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "controller_contract_sha256": hashlib.sha256(
            artifacts[
                "ops/governed_memory/installation/current/contract.json"
            ]
        ).hexdigest(),
        "execution_plan_sha256": hashlib.sha256(
            artifacts[
                "ops/governed_memory/installation/current/controller_plan.json"
            ]
        ).hexdigest(),
        "exact_targets_sha256": hashlib.sha256(
            canonical(contract["exact_targets"])
        ).hexdigest(),
        "controller_runtime_receipt_sha256": "c" * 64,
        "source_boundary": {
            "source_postgres_connection_count": 0,
            "source_postgres_read_count": 0,
            "source_postgres_write_count": 0,
            "source_preparation_phase": "separate_source_preparation_authorization_required",
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
            "allowed_secret_names": list(install_authority.ALLOWED_SECRET_NAMES),
            "forbidden_secret_classes": list(
                install_authority.FORBIDDEN_SECRET_CLASSES
            ),
            "runtime_secret_count": 0,
            "provider_secret_count": 0,
            "supabase_secret_count": 0,
            "service_secret_count": 0,
            "pilot_secret_count": 0,
            "secret_values_present": False,
        },
    }
    scope_raw = canonical(scope)
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    key_id = hashlib.sha256(public).hexdigest()
    trust = {
        "schema_version": install_authority.TRUST_BUNDLE_SCHEMA_VERSION,
        "authorization_namespace": namespace,
        "keys": [
            {
                "key_id": key_id,
                "algorithm": "Ed25519",
                "public_key_base64": base64.b64encode(public).decode("ascii"),
            }
        ],
    }
    scope_sha = hashlib.sha256(scope_raw).hexdigest()
    payload = {
        "schema_version": install_authority.AUTHORIZATION_PAYLOAD_SCHEMA_VERSION,
        "authorization_id": "dormant-install-package-test-auth-000001",
        "authorization_namespace": namespace,
        "thread_id": thread_id,
        "scope_id": scope_id,
        "scope_sha256": scope_sha,
        "key_id": key_id,
        "approval_phrase": (
            "APPROVE GOVERNED MEMORY DORMANT STORE INSTALL " + scope_sha
        ),
        "nonce": INSTALL_PACKAGE_TEST_NONCE,
        "issued_at": "2026-08-12T19:00:00Z",
        "not_before": "2026-08-12T19:00:00Z",
        "expires_at": "2026-08-12T19:10:00Z",
        "single_use": True,
    }
    envelope = {
        "schema_version": install_authority.AUTHORIZATION_SCHEMA_VERSION,
        "payload": payload,
        "signature": {
            "algorithm": "Ed25519",
            "key_id": key_id,
            "value_base64": base64.b64encode(
                private.sign(canonical(payload))
            ).decode("ascii"),
        },
    }
    trust_raw = canonical(trust)
    scope_capability = install_authority.verify_dormant_install_execution_capability(
        scope_raw,
        canonical(envelope),
        trust_raw,
        expected_namespace=namespace,
        expected_thread_id=thread_id,
        expected_scope_id=scope_id,
        expected_key_id=key_id,
        expected_trust_bundle_sha256=hashlib.sha256(trust_raw).hexdigest(),
        expected_bindings=install_authority.DormantInstallExpectedBindings(
            candidate_git_commit=candidate_git_commit,
            candidate_git_tree=candidate_git_tree,
            package_manifest_sha256=str(scope["package_manifest_sha256"]),
            controller_contract_sha256=str(scope["controller_contract_sha256"]),
                execution_plan_sha256=str(scope["execution_plan_sha256"]),
                exact_targets_sha256=str(scope["exact_targets_sha256"]),
                controller_runtime_receipt_sha256=str(
                    scope["controller_runtime_receipt_sha256"]
                ),
            ),
    )
    package_capability = verify_install_package_capability(
        scope_capability,
        signed_scope_json=scope_raw,
        package_manifest_json=manifest_raw,
        artifact_bytes=artifacts,
    )
    if return_scope_capability:
        return scope_capability, package_capability
    return package_capability


def build_verified_controller_runtime(package_capability: object) -> object:
    package, _scope, artifacts = _package_capability_parts(package_capability)
    runtime_root = (
        "/opt/governed-memory-controller/runtimes/"
        + package.controller_runtime_receipt_sha256
    )
    release_root = (
        "/opt/governed-memory-controller/releases/"
        + package.package_manifest_sha256
    )
    evidence = VerifiedControllerRuntimeEvidence(
        result_type="verified_controller_runtime_v1",
        controller_runtime_receipt_sha256=(
            package.controller_runtime_receipt_sha256
        ),
        build_plan_sha256="a" * 64,
        package_manifest_sha256=package.package_manifest_sha256,
        controller_runtime_contract_sha256=(
            package.controller_runtime_contract_sha256
        ),
        controller_requirements_lock_sha256=(
            package.controller_requirements_lock_sha256
        ),
        standalone_cpython_specification_sha256="b" * 64,
        standalone_cpython_archive_sha256="c" * 64,
        standalone_cpython_payload_tree_sha256="d" * 64,
        wheelhouse_tree_sha256="e" * 64,
        runtime_root=runtime_root,
        runtime_tree_sha256="4" * 64,
        release_root=release_root,
        release_tree_sha256="5" * 64,
        package_manifest_path=(
            release_root
            + "/ops/governed_memory/installation/current/package_manifest.json"
        ),
        interpreter_path=runtime_root + "/bin/python",
        interpreter_sha256="6" * 64,
        inventory_path=runtime_root + "/controller-distributions.json",
        installed_distribution_inventory_sha256="7" * 64,
        interpreter_path_facts_sha256="8" * 64,
        postgresql_driver_identity_sha256=(
            EXPECTED_POSTGRESQL_DRIVER_IDENTITY_SHA256
        ),
        supervisor_launcher_path=(
            release_root
            + "/tools/governed_memory_install/store_supervisor_launcher.py"
        ),
        supervisor_launcher_sha256=package.supervisor_launcher_sha256,
        launcher_help_probe_sha256="9" * 64,
        python_implementation="CPython",
        python_version="3.12.11",
        platform_os="linux",
        platform_architecture="x86_64",
        persistent_controller_substrate_created=True,
        persistent_store_resources_created=False,
    )
    return _VerifiedControllerRuntimeCapability(evidence, _RUNTIME_TOKEN)


def build_verified_rollback_capability(
    *,
    install_scope_capability: object,
    package_capability: object,
    controller_runtime_capability: object,
    bindings: EmptyRollbackExpectedBindings,
    nonce: str = "N" * 32,
) -> object:
    """Build and verify a distinct real Ed25519 rollback authorization."""

    namespace = "governed-memory-owner-v1"
    thread_id = "019fe927-8367-7f52-86f2-e2b5b43a2390"
    scope_id = "empty-store-rollback-000001"
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    key_id = hashlib.sha256(public).hexdigest()
    scope = {
        "schema_version": SCOPE_SCHEMA_VERSION,
        "operation": ROLLBACK_OPERATION,
        "authorization_namespace": namespace,
        "thread_id": thread_id,
        "scope_id": scope_id,
        "candidate_git_commit": bindings.candidate_git_commit,
        "candidate_git_tree": bindings.candidate_git_tree,
        "package_manifest_sha256": bindings.package_manifest_sha256,
        "controller_runtime_receipt_sha256": (
            bindings.controller_runtime_receipt_sha256
        ),
        "installation_execution_id": bindings.installation_execution_id,
        "installation_receipt_sha256": bindings.installation_receipt_sha256,
        "rollback_plan_sha256": bindings.rollback_plan_sha256,
        "exact_targets_sha256": bindings.exact_targets_sha256,
        "eligibility_receipt_sha256": bindings.eligibility_receipt_sha256,
        "resource_ledger_head_sha256": bindings.resource_ledger_head_sha256,
        "empty_only_policy": {
            "pilot_ever_started": False,
            "postgresql_user_rows": 0,
            "projection_queue_rows": 0,
            "qdrant_points": 0,
            "active_clients": 0,
            "legacy_imports": 0,
        },
        "retention_policy": {
            "authorization_nonce_retained": True,
            "execution_journal_retained": True,
            "authority_anchor_retained": True,
            "resource_identity_ledger_retained": True,
            "install_and_rollback_receipts_retained": True,
        },
    }
    scope_raw = canonical_json_bytes(scope)
    scope_sha = hashlib.sha256(scope_raw).hexdigest()
    payload = {
        "schema_version": AUTHORIZATION_PAYLOAD_SCHEMA_VERSION,
        "authorization_id": "rollback-authorization-000001",
        "authorization_namespace": namespace,
        "thread_id": thread_id,
        "scope_id": scope_id,
        "scope_sha256": scope_sha,
        "key_id": key_id,
        "approval_phrase": (
            "APPROVE GOVERNED MEMORY EMPTY STORE ROLLBACK " + scope_sha
        ),
        "nonce": nonce,
        "issued_at": "2026-08-12T19:00:00Z",
        "not_before": "2026-08-12T19:00:00Z",
        "expires_at": "2026-08-12T19:10:00Z",
        "single_use": True,
    }
    trust = {
        "schema_version": TRUST_BUNDLE_SCHEMA_VERSION,
        "authorization_namespace": namespace,
        "keys": [
            {
                "key_id": key_id,
                "algorithm": "Ed25519",
                "public_key_base64": base64.b64encode(public).decode("ascii"),
            }
        ],
    }
    envelope = {
        "schema_version": AUTHORIZATION_SCHEMA_VERSION,
        "payload": payload,
        "signature": {
            "algorithm": "Ed25519",
            "key_id": key_id,
            "value_base64": base64.b64encode(
                private.sign(canonical_json_bytes(payload))
            ).decode("ascii"),
        },
    }
    trust_raw = canonical_json_bytes(trust)
    return verify_empty_rollback_execution_capability(
        scope_raw,
        canonical_json_bytes(envelope),
        trust_raw,
        expected_namespace=namespace,
        expected_thread_id=thread_id,
        expected_scope_id=scope_id,
        expected_key_id=key_id,
        expected_trust_bundle_sha256=hashlib.sha256(trust_raw).hexdigest(),
        expected_bindings=bindings,
        verified_install_scope_capability=install_scope_capability,
        verified_package_capability=package_capability,
        verified_controller_runtime_capability=controller_runtime_capability,
    )


class EmptyRollbackAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        (
            self.install_scope_capability,
            self.package_capability,
        ) = build_verified_install_package(return_scope_capability=True)
        self.runtime_capability = build_verified_controller_runtime(
            self.package_capability
        )
        package = verified_package_evidence(self.package_capability)
        self.private_key = Ed25519PrivateKey.generate()
        public_key = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.key_id = hashlib.sha256(public_key).hexdigest()
        self.namespace = "governed-memory-owner-v1"
        self.thread_id = "019fe927-8367-7f52-86f2-e2b5b43a2390"
        self.scope_id = "empty-store-rollback-000001"
        self.bindings = EmptyRollbackExpectedBindings(
            candidate_git_commit="a" * 40,
            candidate_git_tree="b" * 40,
            package_manifest_sha256=package.package_manifest_sha256,
            controller_runtime_receipt_sha256=(
                package.controller_runtime_receipt_sha256
            ),
            installation_execution_id=(
                verified_dormant_install_authority_identity(
                    self.install_scope_capability
                ).execution_id
            ),
            installation_nonce_sha256=INSTALL_PACKAGE_TEST_NONCE_SHA256,
            installation_receipt_sha256="d" * 64,
            rollback_plan_sha256="e" * 64,
            exact_targets_sha256="f" * 64,
            eligibility_receipt_sha256="1" * 64,
            resource_ledger_head_sha256="2" * 64,
        )
        self.scope = {
            "schema_version": SCOPE_SCHEMA_VERSION,
            "operation": ROLLBACK_OPERATION,
            "authorization_namespace": self.namespace,
            "thread_id": self.thread_id,
            "scope_id": self.scope_id,
            "candidate_git_commit": self.bindings.candidate_git_commit,
            "candidate_git_tree": self.bindings.candidate_git_tree,
            "package_manifest_sha256": self.bindings.package_manifest_sha256,
            "controller_runtime_receipt_sha256": (
                self.bindings.controller_runtime_receipt_sha256
            ),
            "installation_execution_id": (
                self.bindings.installation_execution_id
            ),
            "installation_receipt_sha256": (
                self.bindings.installation_receipt_sha256
            ),
            "rollback_plan_sha256": self.bindings.rollback_plan_sha256,
            "exact_targets_sha256": self.bindings.exact_targets_sha256,
            "eligibility_receipt_sha256": (
                self.bindings.eligibility_receipt_sha256
            ),
            "resource_ledger_head_sha256": (
                self.bindings.resource_ledger_head_sha256
            ),
            "empty_only_policy": {
                "pilot_ever_started": False,
                "postgresql_user_rows": 0,
                "projection_queue_rows": 0,
                "qdrant_points": 0,
                "active_clients": 0,
                "legacy_imports": 0,
            },
            "retention_policy": {
                "authorization_nonce_retained": True,
                "execution_journal_retained": True,
                "authority_anchor_retained": True,
                "resource_identity_ledger_retained": True,
                "install_and_rollback_receipts_retained": True,
            },
        }
        self.trust = {
            "schema_version": TRUST_BUNDLE_SCHEMA_VERSION,
            "authorization_namespace": self.namespace,
            "keys": [
                {
                    "key_id": self.key_id,
                    "algorithm": "Ed25519",
                    "public_key_base64": base64.b64encode(public_key).decode(
                        "ascii"
                    ),
                }
            ],
        }

    def documents(
        self,
        *,
        scope: dict[str, object] | None = None,
        payload_change: tuple[str, object] | None = None,
    ) -> tuple[bytes, bytes, bytes]:
        selected_scope = self.scope if scope is None else scope
        scope_bytes = canonical_json_bytes(selected_scope)
        scope_sha256 = hashlib.sha256(scope_bytes).hexdigest()
        payload: dict[str, object] = {
            "schema_version": AUTHORIZATION_PAYLOAD_SCHEMA_VERSION,
            "authorization_id": "rollback-authorization-000001",
            "authorization_namespace": self.namespace,
            "thread_id": self.thread_id,
            "scope_id": self.scope_id,
            "scope_sha256": scope_sha256,
            "key_id": self.key_id,
            "approval_phrase": (
                "APPROVE GOVERNED MEMORY EMPTY STORE ROLLBACK " + scope_sha256
            ),
            "nonce": "A" * 32,
            "issued_at": "2026-08-12T19:00:00Z",
            "not_before": "2026-08-12T19:00:00Z",
            "expires_at": "2026-08-12T19:10:00Z",
            "single_use": True,
        }
        if payload_change is not None:
            payload[payload_change[0]] = payload_change[1]
        signature = self.private_key.sign(canonical_json_bytes(payload))
        envelope = {
            "schema_version": AUTHORIZATION_SCHEMA_VERSION,
            "payload": payload,
            "signature": {
                "algorithm": "Ed25519",
                "key_id": self.key_id,
                "value_base64": base64.b64encode(signature).decode("ascii"),
            },
        }
        return (
            scope_bytes,
            canonical_json_bytes(envelope),
            canonical_json_bytes(self.trust),
        )

    def verify(self, documents: tuple[bytes, bytes, bytes]) -> object:
        trust_sha256 = hashlib.sha256(documents[2]).hexdigest()
        return verify_empty_rollback_execution_capability(
            *documents,
            expected_namespace=self.namespace,
            expected_thread_id=self.thread_id,
            expected_scope_id=self.scope_id,
            expected_key_id=self.key_id,
            expected_trust_bundle_sha256=trust_sha256,
            expected_bindings=self.bindings,
            verified_install_scope_capability=self.install_scope_capability,
            verified_package_capability=self.package_capability,
            verified_controller_runtime_capability=self.runtime_capability,
        )

    def test_distinct_signed_empty_rollback_scope_mints_redacted_capability(
        self,
    ) -> None:
        capability = self.verify(self.documents())
        evidence = rollback_capability_evidence(capability)
        self.assertEqual(evidence.operation, ROLLBACK_OPERATION)
        self.assertEqual(evidence.scope_id, self.scope_id)
        runtime_evidence = verified_controller_runtime_evidence(
            self.runtime_capability
        )
        self.assertEqual(
            evidence.controller_runtime_receipt_sha256,
            runtime_evidence.controller_runtime_receipt_sha256,
        )
        self.assertEqual(
            evidence.controller_release_tree_sha256,
            runtime_evidence.release_tree_sha256,
        )
        self.assertNotIn("nonce", repr(capability).lower())
        self.assertNotIn("authorization", repr(capability).lower())

    def test_install_scope_and_approval_phrase_are_rejected(self) -> None:
        scope = copy.deepcopy(self.scope)
        scope["operation"] = "dormant_install"
        with self.assertRaises(RollbackAuthorityError):
            self.verify(self.documents(scope=scope))
        with self.assertRaises(RollbackAuthorityError):
            self.verify(
                self.documents(
                    payload_change=(
                        "approval_phrase",
                        "APPROVE DORMANT STORE INSTALLATION DORMANT INSTALL " + "0" * 64,
                    )
                )
            )

    def test_every_local_binding_is_exact(self) -> None:
        for key in (
            "candidate_git_commit",
            "candidate_git_tree",
            "package_manifest_sha256",
            "controller_runtime_receipt_sha256",
            "installation_execution_id",
            "installation_receipt_sha256",
            "rollback_plan_sha256",
            "exact_targets_sha256",
            "eligibility_receipt_sha256",
            "resource_ledger_head_sha256",
        ):
            scope = copy.deepcopy(self.scope)
            scope[key] = "0" * len(str(scope[key]))
            with self.subTest(key=key), self.assertRaises(RollbackAuthorityError):
                self.verify(self.documents(scope=scope))

    def test_scope_shape_type_and_canonical_bytes_fail_closed(self) -> None:
        extra = copy.deepcopy(self.scope)
        extra["unexpected"] = True
        with self.assertRaises(RollbackAuthorityError):
            self.verify(self.documents(scope=extra))

        false_as_zero = copy.deepcopy(self.scope)
        false_as_zero["empty_only_policy"]["pilot_ever_started"] = 0  # type: ignore[index]
        with self.assertRaises(RollbackAuthorityError):
            self.verify(self.documents(scope=false_as_zero))

        valid = self.documents()
        noncanonical = json.dumps(self.scope).encode("ascii")
        with self.assertRaises(RollbackAuthorityError):
            self.verify((noncanonical, valid[1], valid[2]))

    def test_trust_key_identifier_must_hash_public_key(self) -> None:
        documents = self.documents()
        trust = copy.deepcopy(self.trust)
        trust["keys"][0]["key_id"] = "0" * 64  # type: ignore[index]
        with self.assertRaises(RollbackAuthorityError):
            verify_empty_rollback_execution_capability(
                documents[0],
                documents[1],
                canonical_json_bytes(trust),
                expected_namespace=self.namespace,
                expected_thread_id=self.thread_id,
                expected_scope_id=self.scope_id,
                expected_key_id="0" * 64,
                expected_trust_bundle_sha256=hashlib.sha256(
                    canonical_json_bytes(trust)
                ).hexdigest(),
                expected_bindings=self.bindings,
                verified_install_scope_capability=self.install_scope_capability,
                verified_package_capability=self.package_capability,
                verified_controller_runtime_capability=self.runtime_capability,
            )

    def test_verified_install_package_capability_is_required(self) -> None:
        documents = self.documents()
        with self.assertRaisesRegex(
            RollbackAuthorityError, "rollback_verified_package_and_runtime_required"
        ):
            verify_empty_rollback_execution_capability(
                *documents,
                expected_namespace=self.namespace,
                expected_thread_id=self.thread_id,
                expected_scope_id=self.scope_id,
                expected_key_id=self.key_id,
                expected_trust_bundle_sha256=hashlib.sha256(
                    documents[2]
                ).hexdigest(),
                expected_bindings=self.bindings,
                verified_install_scope_capability=self.install_scope_capability,
                verified_package_capability=object(),
                verified_controller_runtime_capability=self.runtime_capability,
            )

        with self.assertRaisesRegex(
            RollbackAuthorityError, "rollback_verified_package_and_runtime_required"
        ):
            verify_empty_rollback_execution_capability(
                *documents,
                expected_namespace=self.namespace,
                expected_thread_id=self.thread_id,
                expected_scope_id=self.scope_id,
                expected_key_id=self.key_id,
                expected_trust_bundle_sha256=hashlib.sha256(
                    documents[2]
                ).hexdigest(),
                expected_bindings=self.bindings,
                verified_install_scope_capability=self.install_scope_capability,
                verified_package_capability=self.package_capability,
                verified_controller_runtime_capability=object(),
            )


class EmptyRollbackRecoveryDelegationTests(unittest.TestCase):
    def setUp(self) -> None:
        (
            self.install_scope_capability,
            self.package_capability,
        ) = build_verified_install_package(return_scope_capability=True)
        self.runtime_capability = build_verified_controller_runtime(
            self.package_capability
        )
        self.package = verified_package_evidence(self.package_capability)
        self.private_key = Ed25519PrivateKey.generate()
        public = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.key_id = hashlib.sha256(public).hexdigest()
        self.namespace = "governed-memory-owner-v1"
        self.thread_id = "019fe927-8367-7f52-86f2-e2b5b43a2390"
        self.install_scope_id = "dormant-install-package-test-000001"
        self.rollback_scope_id = "empty-store-rollback-000001"
        self.authorization_text_sha256 = "8" * 64
        self.installation_execution_id = (
            verified_dormant_install_authority_identity(
                self.install_scope_capability
            ).execution_id
        )
        self.trust = {
            "schema_version": TRUST_BUNDLE_SCHEMA_VERSION,
            "authorization_namespace": self.namespace,
            "keys": [
                {
                    "key_id": self.key_id,
                    "algorithm": "Ed25519",
                    "public_key_base64": base64.b64encode(public).decode(
                        "ascii"
                    ),
                }
            ],
        }
        self.payload = {
            "schema_version": RECOVERY_DELEGATION_PAYLOAD_SCHEMA_VERSION,
            "authorization_namespace": self.namespace,
            "thread_id": self.thread_id,
            "install_scope_id": self.install_scope_id,
            "rollback_scope_id": self.rollback_scope_id,
            "authorization_text_sha256": self.authorization_text_sha256,
            "candidate_git_commit": self.package.candidate_git_commit,
            "candidate_git_tree": self.package.candidate_git_tree,
            "package_manifest_sha256": self.package.package_manifest_sha256,
            "controller_runtime_receipt_sha256": (
                self.package.controller_runtime_receipt_sha256
            ),
            "controller_contract_sha256": (
                self.package.controller_contract_sha256
            ),
            "execution_plan_sha256": self.package.execution_plan_sha256,
            "exact_target_contract_sha256": (
                self.package.exact_targets_sha256
            ),
            "installation_execution_id": self.installation_execution_id,
            "rollback_nonce": "R" * 32,
            "recovery_reservation_nonce": "V" * 32,
            "key_id": self.key_id,
            "issued_at": "2026-08-12T19:00:00Z",
            "not_before": "2026-08-12T19:00:00Z",
            "expires_at": "2026-08-12T19:10:00Z",
            "single_use": True,
            "empty_only": True,
            "derivation_policy": dict(RECOVERY_DERIVATION_POLICY),
            "source_postgres_read_count": 0,
            "production_data_read": False,
            "provider_calls": 0,
            "activation_allowed": False,
        }

    def documents(
        self, *, payload: dict[str, object] | None = None
    ) -> tuple[bytes, bytes]:
        selected = copy.deepcopy(self.payload if payload is None else payload)
        envelope = {
            "schema_version": RECOVERY_DELEGATION_SCHEMA_VERSION,
            "payload": selected,
            "signature": {
                "algorithm": "Ed25519",
                "key_id": self.key_id,
                "value_base64": base64.b64encode(
                    self.private_key.sign(canonical_json_bytes(selected))
                ).decode("ascii"),
            },
        }
        return canonical_json_bytes(envelope), canonical_json_bytes(self.trust)

    def verify(self, documents: tuple[bytes, bytes]) -> object:
        return verify_empty_rollback_recovery_delegation(
            *documents,
            expected_namespace=self.namespace,
            expected_thread_id=self.thread_id,
            expected_install_scope_id=self.install_scope_id,
            expected_rollback_scope_id=self.rollback_scope_id,
            expected_authorization_text_sha256=(
                self.authorization_text_sha256
            ),
            expected_key_id=self.key_id,
            expected_trust_bundle_sha256=hashlib.sha256(
                documents[1]
            ).hexdigest(),
            expected_installation_execution_id=(
                self.installation_execution_id
            ),
            verified_install_scope_capability=self.install_scope_capability,
            verified_package_capability=self.package_capability,
            verified_controller_runtime_capability=self.runtime_capability,
        )

    def bindings(
        self, *, derived_resources: str = "f" * 64
    ) -> EmptyRollbackExpectedBindings:
        return EmptyRollbackExpectedBindings(
            candidate_git_commit=self.package.candidate_git_commit,
            candidate_git_tree=self.package.candidate_git_tree,
            package_manifest_sha256=self.package.package_manifest_sha256,
            controller_runtime_receipt_sha256=(
                self.package.controller_runtime_receipt_sha256
            ),
            installation_execution_id=self.installation_execution_id,
            installation_nonce_sha256=INSTALL_PACKAGE_TEST_NONCE_SHA256,
            installation_receipt_sha256="d" * 64,
            rollback_plan_sha256="e" * 64,
            exact_targets_sha256=derived_resources,
            eligibility_receipt_sha256="1" * 64,
            resource_ledger_head_sha256="2" * 64,
        )

    def scope(self, bindings: EmptyRollbackExpectedBindings) -> dict[str, object]:
        return {
            "schema_version": SCOPE_SCHEMA_VERSION,
            "operation": ROLLBACK_OPERATION,
            "authorization_namespace": self.namespace,
            "thread_id": self.thread_id,
            "scope_id": self.rollback_scope_id,
            "candidate_git_commit": bindings.candidate_git_commit,
            "candidate_git_tree": bindings.candidate_git_tree,
            "package_manifest_sha256": bindings.package_manifest_sha256,
            "controller_runtime_receipt_sha256": (
                bindings.controller_runtime_receipt_sha256
            ),
            "installation_execution_id": bindings.installation_execution_id,
            "installation_receipt_sha256": (
                bindings.installation_receipt_sha256
            ),
            "rollback_plan_sha256": bindings.rollback_plan_sha256,
            "exact_targets_sha256": bindings.exact_targets_sha256,
            "eligibility_receipt_sha256": (
                bindings.eligibility_receipt_sha256
            ),
            "resource_ledger_head_sha256": (
                bindings.resource_ledger_head_sha256
            ),
            "empty_only_policy": {
                "pilot_ever_started": False,
                "postgresql_user_rows": 0,
                "projection_queue_rows": 0,
                "qdrant_points": 0,
                "active_clients": 0,
                "legacy_imports": 0,
            },
            "retention_policy": {
                "authorization_nonce_retained": True,
                "execution_journal_retained": True,
                "authority_anchor_retained": True,
                "resource_identity_ledger_retained": True,
                "install_and_rollback_receipts_retained": True,
            },
        }

    def test_signed_public_delegation_mints_exact_reservation_binding(self) -> None:
        capability = self.verify(self.documents())
        evidence = recovery_delegation_evidence(capability)
        reservation = recovery_reservation_binding(capability)
        self.assertEqual(
            evidence.exact_target_contract_sha256,
            self.package.exact_targets_sha256,
        )
        self.assertEqual(
            reservation.operation, RECOVERY_RESERVATION_OPERATION
        )
        self.assertEqual(reservation.nonce, "V" * 32)
        self.assertEqual(
            reservation.installation_execution_id,
            self.installation_execution_id,
        )
        for value in (
            reservation.execution_sha256,
            reservation.authorization_sha256,
            reservation.scope_sha256,
            reservation.trust_bundle_sha256,
        ):
            self.assertRegex(value, r"\A[0-9a-f]{64}\Z")

    def test_rollback_uses_the_canonical_install_authority_identity(self) -> None:
        identity = verified_dormant_install_authority_identity(
            self.install_scope_capability
        )
        self.assertEqual(identity.execution_id, self.installation_execution_id)
        self.assertEqual(identity.authorization_nonce, INSTALL_PACKAGE_TEST_NONCE)
        self.assertEqual(
            identity.authorization_nonce_sha256,
            INSTALL_PACKAGE_TEST_NONCE_SHA256,
        )
        source = inspect.getsource(rollback_module)
        for copied_implementation in (
            "_execution_capability_evidence",
            "_AUTHORITY_NONCE_DOMAIN",
            "_AUTHORITY_OPERATION_DOMAIN",
            "_AUTHORITY_CLAIM_DOMAIN",
            "_INSTALL_EXECUTION_ID_DOMAIN",
            "_verified_install_scope_binding",
        ):
            with self.subTest(copied_implementation=copied_implementation):
                self.assertNotIn(copied_implementation, source)

    def test_delegation_verification_is_time_structural_not_wall_clock_current(
        self,
    ) -> None:
        # The signed window is deliberately historical. Trusted-time currency
        # belongs to the durable pre-effect reservation claim, not this parser.
        self.verify(self.documents())

    def test_changed_delegation_binding_or_policy_is_refused(self) -> None:
        for key, value in (
            ("installation_execution_id", "0" * 64),
            ("exact_target_contract_sha256", "0" * 64),
            ("activation_allowed", True),
            ("rollback_nonce", "V" * 32),
            ("rollback_nonce", INSTALL_PACKAGE_TEST_NONCE),
            ("recovery_reservation_nonce", INSTALL_PACKAGE_TEST_NONCE),
        ):
            payload = copy.deepcopy(self.payload)
            payload[key] = value
            with self.subTest(key=key), self.assertRaises(
                RollbackAuthorityError
            ):
                self.verify(self.documents(payload=payload))
        payload = copy.deepcopy(self.payload)
        payload["derivation_policy"]["rollback_scope"] = "loose"  # type: ignore[index]
        with self.assertRaises(RollbackAuthorityError):
            self.verify(self.documents(payload=payload))

    def test_delegation_signature_and_bounded_window_fail_closed(self) -> None:
        delegation, trust = self.documents()
        envelope = json.loads(delegation.decode("ascii"))
        envelope["signature"]["value_base64"] = base64.b64encode(  # type: ignore[index]
            b"0" * 64
        ).decode("ascii")
        with self.assertRaisesRegex(
            RollbackAuthorityError, "delegation_signature_invalid"
        ):
            self.verify((canonical_json_bytes(envelope), trust))
        payload = copy.deepcopy(self.payload)
        payload["expires_at"] = "2026-08-12T19:15:01Z"
        with self.assertRaisesRegex(
            RollbackAuthorityError, "delegation_time_invalid"
        ):
            self.verify(self.documents(payload=payload))

    def test_derivation_binds_independent_post_effect_resource_identity(self) -> None:
        delegation = self.verify(self.documents())
        bindings = self.bindings(derived_resources="9" * 64)
        capability = (
            derive_empty_rollback_execution_capability_from_recovery_delegation(
                delegation,
                canonical_json_bytes(self.scope(bindings)),
                expected_bindings=bindings,
            )
        )
        evidence = rollback_capability_evidence(capability)
        self.assertEqual(evidence.exact_targets_sha256, "9" * 64)
        self.assertNotEqual(
            evidence.exact_targets_sha256,
            self.package.exact_targets_sha256,
        )
        self.assertEqual(
            evidence.recovery_reservation,
            recovery_reservation_binding(delegation),
        )

    def test_derived_scope_change_is_refused(self) -> None:
        delegation = self.verify(self.documents())
        bindings = self.bindings()
        scope = self.scope(bindings)
        scope["resource_ledger_head_sha256"] = "0" * 64
        with self.assertRaisesRegex(
            RollbackAuthorityError, "recovery_local_binding_mismatch"
        ):
            derive_empty_rollback_execution_capability_from_recovery_delegation(
                delegation,
                canonical_json_bytes(scope),
                expected_bindings=bindings,
            )


if __name__ == "__main__":
    unittest.main()
