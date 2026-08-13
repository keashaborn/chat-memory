from __future__ import annotations

import base64
import copy
import hashlib
import os
from pathlib import Path
import tempfile
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tools.governed_memory_install.rollback import (
    EMPTY_ROLLBACK_STEPS,
    RETAINED_AUDIT_KEYS,
    ROLLBACK_RESOURCE_KEYS,
    EmptyRollbackError,
    build_empty_rollback_eligibility_receipt,
    build_empty_rollback_plan,
    canonical_json_bytes,
    eligibility_receipt_sha256,
    empty_rollback_plan_sha256,
    derive_exact_rollback_resources_from_ledger,
    exact_rollback_targets_sha256,
    expected_rollback_resource_names,
    verify_empty_rollback_eligibility_receipt,
    verified_rollback_resource_parts,
)
from tools.governed_memory_install.rollback_authority import (
    AUTHORIZATION_PAYLOAD_SCHEMA_VERSION,
    AUTHORIZATION_SCHEMA_VERSION,
    EmptyRollbackExpectedBindings,
    ROLLBACK_OPERATION,
    SCOPE_SCHEMA_VERSION,
    TRUST_BUNDLE_SCHEMA_VERSION,
    verify_empty_rollback_execution_capability,
)
from tools.governed_memory_install.package_capability import (
    verified_package_evidence,
)
from tools.governed_memory_install.resource_identity import load_ledger
from tests.memory.test_empty_rollback_authority import (
    build_verified_controller_runtime,
    build_verified_install_package,
)
from tests.memory.resource_identity_test_support import append_resource_identity


class EmptyRollbackPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        os.chmod(self.root, 0o700)
        self.ledger_counter = 0
        self.package_capability = build_verified_install_package()
        self.runtime_capability = build_verified_controller_runtime(
            self.package_capability
        )
        package = verified_package_evidence(self.package_capability)
        self.commit = package.candidate_git_commit
        self.tree = package.candidate_git_tree
        self.package_sha = package.package_manifest_sha256
        self.installation_execution_id = "6" * 64
        self.install_receipt_sha = "d" * 64
        records = self._ledger_records()
        self.ledger_head = records[-1].entry_sha256
        self.resources = derive_exact_rollback_resources_from_ledger(
            records,
            package_manifest_sha256=self.package_sha,
            expected_ledger_head_sha256=self.ledger_head,
        )
        targets_sha = exact_rollback_targets_sha256(
            self.resources, package_manifest_sha256=self.package_sha
        )
        self.eligibility = build_empty_rollback_eligibility_receipt(
            candidate_git_commit=self.commit,
            candidate_git_tree=self.tree,
            package_manifest_sha256=self.package_sha,
            installation_execution_id=self.installation_execution_id,
            installation_receipt_sha256=self.install_receipt_sha,
            exact_targets_sha256=targets_sha,
            resource_ledger_head_sha256=self.ledger_head,
            observation_set_sha256="f" * 64,
            pilot_ever_started=False,
            postgresql_user_rows=0,
            projection_queue_rows=0,
            qdrant_points=0,
            active_clients=0,
            legacy_imports=0,
            application_services_installed=False,
            source_postgres_read_count=0,
            production_data_read=False,
            provider_calls=0,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _ledger_records(
        self,
        *,
        omit_key: str | None = None,
        drift_key: str | None = None,
    ) -> tuple[object, ...]:
        self.ledger_counter += 1
        ledger_path = self.root / f"ledger-{self.ledger_counter}.jsonl"
        binding = hashlib.sha256(
            f"binding:{self.ledger_counter}".encode("ascii")
        ).hexdigest()
        expected = expected_rollback_resource_names(self.package_sha)
        for key in ROLLBACK_RESOURCE_KEYS:
            if key == omit_key:
                continue
            kind, name = expected[key]
            if key == drift_key:
                name = "unexpected-" + key
            is_container = kind == "container"
            append_resource_identity(
                ledger_path,
                binding_sha256=binding,
                event="created",
                resource_kind=kind,
                resource_name=name,
                resource_id=(
                    hashlib.sha256(("container:" + key).encode("ascii")).hexdigest()
                    if is_container
                    else "resource-" + key
                ),
                image_id=("sha256:" + "1" * 64) if is_container else None,
                image_repo_digest=(
                    "governed/test@sha256:" + "2" * 64
                    if is_container
                    else None
                ),
                ownership_sha256=hashlib.sha256(
                    ("ownership:" + key).encode("ascii")
                ).hexdigest(),
                resource_labels_sha256=(
                    hashlib.sha256(("labels:" + key).encode("ascii")).hexdigest()
                    if kind in {"container", "network", "volume"}
                    else None
                ),
            )
        return load_ledger(
            ledger_path, expected_binding_sha256=binding
        )

    def capability(
        self,
        *,
        eligibility: dict[str, object] | None = None,
        resources: object | None = None,
    ) -> object:
        selected_eligibility = self.eligibility if eligibility is None else eligibility
        selected_resources = self.resources if resources is None else resources
        targets_sha = exact_rollback_targets_sha256(
            selected_resources,
            package_manifest_sha256=str(
                selected_eligibility["package_manifest_sha256"]
            ),
        )
        plan_sha = empty_rollback_plan_sha256(
            selected_eligibility, selected_resources
        )
        bindings = EmptyRollbackExpectedBindings(
            candidate_git_commit=str(selected_eligibility["candidate_git_commit"]),
            candidate_git_tree=str(selected_eligibility["candidate_git_tree"]),
            package_manifest_sha256=str(
                selected_eligibility["package_manifest_sha256"]
            ),
            controller_runtime_receipt_sha256=(
                verified_package_evidence(
                    self.package_capability
                ).controller_runtime_receipt_sha256
            ),
            installation_execution_id=str(
                selected_eligibility["installation_execution_id"]
            ),
            installation_receipt_sha256=str(
                selected_eligibility["installation_receipt_sha256"]
            ),
            rollback_plan_sha256=plan_sha,
            exact_targets_sha256=targets_sha,
            eligibility_receipt_sha256=str(
                selected_eligibility["receipt_sha256"]
            ),
            resource_ledger_head_sha256=str(
                selected_eligibility["resource_ledger_head_sha256"]
            ),
        )
        namespace = "governed-memory-owner-v1"
        thread_id = "019fe927-8367-7f52-86f2-e2b5b43a2390"
        scope_id = "empty-store-rollback-000001"
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
        scope_bytes = canonical_json_bytes(scope)
        scope_sha = hashlib.sha256(scope_bytes).hexdigest()
        private = Ed25519PrivateKey.generate()
        public = private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        key_id = hashlib.sha256(public).hexdigest()
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
            "nonce": "A" * 32,
            "issued_at": "2026-08-12T19:00:00Z",
            "not_before": "2026-08-12T19:00:00Z",
            "expires_at": "2026-08-12T19:10:00Z",
            "single_use": True,
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
        trust_bytes = canonical_json_bytes(trust)
        return verify_empty_rollback_execution_capability(
            scope_bytes,
            canonical_json_bytes(envelope),
            trust_bytes,
            expected_namespace=namespace,
            expected_thread_id=thread_id,
            expected_scope_id=scope_id,
            expected_key_id=key_id,
            expected_trust_bundle_sha256=hashlib.sha256(trust_bytes).hexdigest(),
            expected_bindings=bindings,
            verified_package_capability=self.package_capability,
            verified_controller_runtime_capability=self.runtime_capability,
        )

    def test_exact_empty_plan_is_reverse_destructive_and_retains_audit(self) -> None:
        plan = build_empty_rollback_plan(
            self.capability(), self.eligibility, self.resources
        )
        self.assertEqual(plan.steps, EMPTY_ROLLBACK_STEPS)
        self.assertEqual(plan.retained_audit_keys, RETAINED_AUDIT_KEYS)
        self.assertEqual(
            plan.steps[3].step_id,
            "R04_ACQUIRE_ROLLBACK_CONTROLLER_AUTHORITY_MARKER",
        )
        self.assertEqual(
            plan.steps[4].step_id,
            "R05_RECHECK_SEMANTIC_EMPTY_UNDER_CONTROLLER_AUTHORITY_MARKER",
        )
        self.assertEqual(plan.steps[5].resource_key, "stores_supervisor")
        self.assertEqual(plan.steps[6].step_id, "R07_STOP_EXACT_STORES")
        self.assertEqual(plan.steps[14].resource_key, "resolved_store_spec")
        self.assertTrue(all(step.invariant_only for step in plan.steps[15:21]))
        self.assertEqual(
            plan.steps[-1].step_id,
            "R22_VERIFY_EXACT_ABSENCE_AND_RETAIN_AUDIT",
        )
        self.assertNotIn("controller_release", ROLLBACK_RESOURCE_KEYS)
        self.assertNotIn(
            "controller_release",
            expected_rollback_resource_names(self.package_sha),
        )
        _evidence, exact = verified_rollback_resource_parts(self.resources)
        self.assertEqual(plan.resources, exact)
        self.assertEqual(
            plan.plan_sha256,
            empty_rollback_plan_sha256(self.eligibility, self.resources),
        )
        self.assertNotIn("secret_value", repr(plan).lower())

    def test_any_nonempty_or_application_state_is_ineligible(self) -> None:
        cases = (
            ("pilot_ever_started", True),
            ("postgresql_user_rows", 1),
            ("projection_queue_rows", 1),
            ("qdrant_points", 1),
            ("active_clients", 1),
            ("legacy_imports", 1),
            ("application_services_installed", True),
            ("source_postgres_read_count", 1),
            ("production_data_read", True),
            ("provider_calls", 1),
        )
        for key, value in cases:
            receipt = copy.deepcopy(self.eligibility)
            receipt[key] = value
            receipt["receipt_sha256"] = eligibility_receipt_sha256(receipt)
            with self.subTest(key=key), self.assertRaises(EmptyRollbackError):
                verify_empty_rollback_eligibility_receipt(receipt)

    def test_missing_reordered_unknown_or_unsealed_targets_fail_closed(self) -> None:
        missing = self._ledger_records(omit_key="resolved_store_spec")
        with self.assertRaises(EmptyRollbackError):
            derive_exact_rollback_resources_from_ledger(
                missing,
                package_manifest_sha256=self.package_sha,
                expected_ledger_head_sha256=missing[-1].entry_sha256,
            )

        drifted = self._ledger_records(drift_key="stores_supervisor")
        with self.assertRaises(EmptyRollbackError):
            derive_exact_rollback_resources_from_ledger(
                drifted,
                package_manifest_sha256=self.package_sha,
                expected_ledger_head_sha256=drifted[-1].entry_sha256,
            )

        _evidence, exact = verified_rollback_resource_parts(self.resources)
        with self.assertRaises(EmptyRollbackError):
            exact_rollback_targets_sha256(
                exact, package_manifest_sha256=self.package_sha
            )

    def test_authority_is_bound_to_eligibility_targets_and_ledger(self) -> None:
        capability = self.capability()
        for key in (
            "installation_execution_id",
            "installation_receipt_sha256",
            "resource_ledger_head_sha256",
            "observation_set_sha256",
        ):
            receipt = copy.deepcopy(self.eligibility)
            receipt[key] = "0" * 64
            receipt["receipt_sha256"] = eligibility_receipt_sha256(receipt)
            with self.subTest(key=key), self.assertRaises(EmptyRollbackError):
                build_empty_rollback_plan(capability, receipt, self.resources)

    def test_non_rollback_capability_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            build_empty_rollback_plan(object(), self.eligibility, self.resources)


if __name__ == "__main__":
    unittest.main()
