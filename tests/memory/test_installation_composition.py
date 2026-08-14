from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import inspect
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.governed_memory_install import authority, install_entrypoint
from tools.governed_memory_install import linux_live_adapters, linux_store_effects
from tools.governed_memory_install.controller import (
    CompensationFailedError,
    CompletedStateError,
    InstallationCompensatedError,
    JournalEvent,
    JournalRecord,
    DormantStoreInstallController,
    STORES_ONLY_PLAN,
    validate_plan,
)
from tools.governed_memory_install.execution_capability import (
    ClaimedExecutionBindingEvidence,
    _ClaimedExecutionBinding,
    _CLAIMED_EXECUTION_TOKEN,
    verified_dormant_install_authority_identity,
)
from tools.governed_memory_install.execution_lock import GlobalExecutionLock
from tools.governed_memory_install.authority_state import AuthorityState
from tools.governed_memory_install.execution_capability import (
    _claimed_execution_binding_evidence,
)
from tools.governed_memory_install.host_boundary import (
    HostApplyResult,
    HostObservation,
    HostOperationRequest,
    HostResourceIdentityReceipt,
)
from tools.governed_memory_install.image_preflight import (
    LocalImageIdentity,
    LocalImageSetReadiness,
)
from tools.governed_memory_install.install_backend import (
    ClaimBoundInstallDependencies,
    ClaimBoundInstallBackend,
    InstallBackendError,
    InstallPrerequisites,
)
from tools.governed_memory_install.install_entrypoint import (
    INACTIVE_REFUSAL_CODE,
    InstallEntrypointError,
    _run_authorized_dormant_store_install_synthetic,
    _resume_authorized_dormant_store_install_synthetic,
    production_install_postgres_stage_receipt_sink,
    run_authorized_dormant_store_install,
    resume_authorized_dormant_store_install,
)
from tools.governed_memory_install.durable_receipts import DurableReceiptStore
from tools.governed_memory_install.journal import DurableJournal
from tools.governed_memory_install.linux_plan import (
    ExecutionBinding,
    LinuxPlanError,
    bind_store_spec,
    validate_store_spec,
)
from tools.governed_memory_install.package_capability import (
    PackageCapabilityError,
    _package_capability_parts,
    verify_install_package_capability,
)
from tools.governed_memory_install.store_readiness import (
    EXPECTED_QDRANT_COLLECTION_CONFIG_SHA256,
    EmptyStoreReadiness,
    PrebootstrapPostgreSQLReadiness,
    QdrantReadiness,
    REQUIRED_ROLE_NAMES,
    StoreReadinessError,
    TERMINAL_MIGRATION_IDS,
    POSTGRES_SERVER_VERSION,
    TerminalCanonicalStoreReadiness,
    TerminalPostgreSQLReadiness,
    TerminalQdrantReadiness,
)
from tools.governed_memory_install.resource_identity import (
    ResourceIdentityError,
    ResourceIdentityLedger,
)
from tools.governed_memory_install.postgres_native_stages import (
    APPROVED_TERMINAL_CATALOG_SHA256,
)
from tools.governed_memory_install.rollback import ROLLBACK_RESOURCE_KEYS
from tools.governed_memory_install.receipts import verify_install_receipt
from tools.governed_memory_install.controller_runtime import (
    EXPECTED_POSTGRESQL_DRIVER_IDENTITY_SHA256,
    VerifiedControllerRuntimeEvidence,
    _RUNTIME_TOKEN,
    _VerifiedControllerRuntimeCapability,
)
from tools.governed_memory_install.image_preflight import (
    expectations_from_store_spec,
)
from tests.memory.test_installation_durable_journal import _Fixture


HASH_A = "a" * 64
HASH_B = "b" * 64
PACKAGE_SHA256 = "d" * 64
RUNTIME_RECEIPT_SHA256 = "a" * 64
RUNTIME_ROOT = (
    "/opt/governed-memory-controller/runtimes/" + RUNTIME_RECEIPT_SHA256
)
AUTHORIZATION_ID = "test-auth"
AUTHORIZATION_NONCE_SHA256 = "0" * 64
JOURNAL_BINDING_SHA256 = "8" * 64
REPO_ROOT = Path(__file__).resolve().parents[2]


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _static_store_spec() -> dict[str, object]:
    return json.loads(
        (
            REPO_ROOT
            / "ops/governed_memory/installation/store_spec-v5.json"
        ).read_text(encoding="ascii")
    )


def _resolved_store_spec(
    journal_binding_sha256: str = JOURNAL_BINDING_SHA256,
) -> dict[str, object]:
    return bind_store_spec(
        _static_store_spec(),
        ExecutionBinding(
            binding_sha256=journal_binding_sha256,
            authorization_id=AUTHORIZATION_ID,
            authorization_nonce_sha256=AUTHORIZATION_NONCE_SHA256,
            execution_id="7" * 64,
            package_manifest_sha256=PACKAGE_SHA256,
        ),
    )


def _synthetic_package_artifacts(
    contract: dict[str, object], plan: dict[str, object]
) -> dict[str, bytes]:
    return {
        "ops/governed_memory/installation/current/contract.json": _canonical(
            contract
        ),
        "ops/governed_memory/installation/current/controller_plan.json": (
            _canonical(plan)
        ),
        "ops/governed_memory/installation/store_spec-v5.json": _canonical(
            _static_store_spec()
        ),
        "ops/governed_memory/installation/current/"
        "controller_runtime_contract.json": _canonical({}),
        "ops/governed_memory/controller-requirements.lock": b"runtime-lock\n",
        "tools/governed_memory_install/resource_identity.py": (
            b"identity source\n"
        ),
        "tools/governed_memory_install/store_supervisor_launcher.py": (
            b"# synthetic launcher\n"
        ),
    }


def _synthetic_runtime_capability(
    package_manifest_sha256: str, artifacts: dict[str, bytes]
) -> object:
    release_root = (
        "/opt/governed-memory-controller/releases/" + package_manifest_sha256
    )
    evidence = VerifiedControllerRuntimeEvidence(
        result_type="verified_controller_runtime_v1",
        controller_runtime_receipt_sha256=RUNTIME_RECEIPT_SHA256,
        build_plan_sha256="2" * 64,
        package_manifest_sha256=package_manifest_sha256,
        controller_runtime_contract_sha256=hashlib.sha256(
            artifacts[
                "ops/governed_memory/installation/current/"
                "controller_runtime_contract.json"
            ]
        ).hexdigest(),
        controller_requirements_lock_sha256=hashlib.sha256(
            artifacts["ops/governed_memory/controller-requirements.lock"]
        ).hexdigest(),
        standalone_cpython_specification_sha256="3" * 64,
        standalone_cpython_archive_sha256="4" * 64,
        standalone_cpython_payload_tree_sha256="5" * 64,
        wheelhouse_tree_sha256="6" * 64,
        runtime_root=RUNTIME_ROOT,
        runtime_tree_sha256="b" * 64,
        release_root=release_root,
        release_tree_sha256="8" * 64,
        package_manifest_path=(
            release_root
            + "/ops/governed_memory/installation/current/package_manifest.json"
        ),
        interpreter_path=RUNTIME_ROOT + "/bin/python",
        interpreter_sha256="c" * 64,
        inventory_path=RUNTIME_ROOT + "/controller-distributions.json",
        installed_distribution_inventory_sha256="d" * 64,
        interpreter_path_facts_sha256="9" * 64,
        postgresql_driver_identity_sha256=(
            EXPECTED_POSTGRESQL_DRIVER_IDENTITY_SHA256
        ),
        supervisor_launcher_path=(
            "/opt/governed-memory-controller/releases/"
            + package_manifest_sha256
            + "/tools/governed_memory_install/store_supervisor_launcher.py"
        ),
        supervisor_launcher_sha256=hashlib.sha256(
            artifacts[
                "tools/governed_memory_install/store_supervisor_launcher.py"
            ]
        ).hexdigest(),
        launcher_help_probe_sha256="1" * 64,
        python_implementation="CPython",
        python_version="3.12.11",
        platform_os="linux",
        platform_architecture="x86_64",
        persistent_controller_substrate_created=True,
        persistent_store_resources_created=False,
    )
    return _VerifiedControllerRuntimeCapability(evidence, _RUNTIME_TOKEN)


def _terminal_readiness(
    *, governed_user_row_count: int = 0
) -> TerminalCanonicalStoreReadiness:
    return TerminalCanonicalStoreReadiness.create(
        TerminalPostgreSQLReadiness(
            "127.0.0.1:55436",
            16,
            POSTGRES_SERVER_VERSION,
            "governed_memory",
            TERMINAL_MIGRATION_IDS,
            "7" * 64,
            "8" * 64,
            governed_user_row_count,
            0,
            0,
            "9" * 64,
        ),
        TerminalQdrantReadiness(
            "127.0.0.1:6347",
            "1.19.0",
            "governed_memory_9a54cf123493_000005",
            "governed_memory_active",
            True,
            "governed_memory_9a54cf123493_000005",
            EXPECTED_QDRANT_COLLECTION_CONFIG_SHA256,
            0,
            0,
            0,
            "a" * 64,
        ),
    )


def _binding(
    *,
    journal_binding_sha256: str = JOURNAL_BINDING_SHA256,
    resolved_store_spec: dict[str, object] | None = None,
) -> object:
    selected_store_spec = (
        _resolved_store_spec(journal_binding_sha256)
        if resolved_store_spec is None
        else resolved_store_spec
    )
    evidence = ClaimedExecutionBindingEvidence(
        result_type="dormant_store_install_claimed_execution_binding_v1",
        claim_result="claimed_new",
        operation="dormant_install",
        claim_sha256=HASH_A,
        execution_sha256=HASH_B,
        authorization_sha256=HASH_A,
        authorization_id=AUTHORIZATION_ID,
        authorization_nonce_sha256=AUTHORIZATION_NONCE_SHA256,
        scope_sha256=HASH_B,
        trust_bundle_sha256="c" * 64,
        candidate_git_commit="1" * 40,
        candidate_git_tree="2" * 40,
        package_manifest_sha256=PACKAGE_SHA256,
        controller_contract_sha256="e" * 64,
        execution_plan_sha256="f" * 64,
        controller_model_sha256=validate_plan(STORES_ONLY_PLAN),
        exact_targets_sha256="1" * 64,
        controller_runtime_receipt_sha256=RUNTIME_RECEIPT_SHA256,
        controller_runtime_root=RUNTIME_ROOT,
        controller_runtime_tree_sha256="b" * 64,
        controller_release_root=(
            "/opt/governed-memory-controller/releases/" + PACKAGE_SHA256
        ),
        controller_release_tree_sha256="8" * 64,
        controller_release_package_manifest_path=(
            "/opt/governed-memory-controller/releases/"
            + PACKAGE_SHA256
            + "/ops/governed_memory/installation/current/package_manifest.json"
        ),
        controller_runtime_interpreter_path=RUNTIME_ROOT + "/bin/python",
        controller_runtime_interpreter_sha256="c" * 64,
        controller_runtime_inventory_path=(
            RUNTIME_ROOT + "/controller-distributions.json"
        ),
        controller_runtime_inventory_sha256="d" * 64,
        controller_requirements_lock_sha256="e" * 64,
        supervisor_launcher_path=(
            "/opt/governed-memory-controller/releases/"
            + PACKAGE_SHA256
            + "/tools/governed_memory_install/store_supervisor_launcher.py"
        ),
        supervisor_launcher_sha256="f" * 64,
        store_spec_sha256="2" * 64,
        resolved_store_spec_sha256=hashlib.sha256(
            _canonical(selected_store_spec)
        ).hexdigest(),
        resource_identity_implementation_sha256="3" * 64,
        global_lock_path_sha256="4" * 64,
        authority_state_path_sha256="5" * 64,
        resource_identity_ledger_path="/tmp/execution/resources.jsonl",
        resource_identity_ledger_path_sha256="6" * 64,
        execution_journal_path="/tmp/execution/journal.jsonl",
        execution_id="7" * 64,
        attempt_id="install-" + "9" * 40,
        journal_binding_sha256=journal_binding_sha256,
    )
    return _ClaimedExecutionBinding(evidence, _CLAIMED_EXECUTION_TOKEN)


class _Readiness:
    def __init__(self) -> None:
        self.fresh_calls = 0
        self.terminal_calls = 0
        self.terminal_user_row_count = 0

    def verify_fresh_empty_stores(self) -> EmptyStoreReadiness:
        self.fresh_calls += 1
        return EmptyStoreReadiness.create(
            PrebootstrapPostgreSQLReadiness(
                "127.0.0.1:55436",
                16,
                POSTGRES_SERVER_VERSION,
                "postgres",
                "governed_memory",
                False,
                REQUIRED_ROLE_NAMES,
                (),
                0,
                "7" * 64,
            ),
            QdrantReadiness(
                "127.0.0.1:6347",
                "1.19.0",
                "governed_memory_9a54cf123493_000005",
                "governed_memory_active",
                False,
                0,
                0,
                "8" * 64,
            ),
        )

    def verify_terminal_canonical_stores(
        self,
    ) -> TerminalCanonicalStoreReadiness:
        self.terminal_calls += 1
        return _terminal_readiness(
            governed_user_row_count=self.terminal_user_row_count
        )


def _prerequisites() -> InstallPrerequisites:
    expected_images = expectations_from_store_spec(_static_store_spec())
    images = LocalImageSetReadiness.create(
        (
            LocalImageIdentity(
                "postgres",
                expected_images[0].reference,
                expected_images[0].repo_digest,
                "sha256:" + "2" * 64,
                "linux",
                "amd64",
            ),
            LocalImageIdentity(
                "qdrant",
                expected_images[1].reference,
                expected_images[1].repo_digest,
                "sha256:" + "4" * 64,
                "linux",
                "amd64",
            ),
        )
    )
    return InstallPrerequisites(
        store_spec_sha256="2" * 64,
        controller_runtime_receipt_sha256=RUNTIME_RECEIPT_SHA256,
        controller_runtime_root=RUNTIME_ROOT,
        controller_runtime_tree_sha256="b" * 64,
        controller_release_root=(
            "/opt/governed-memory-controller/releases/" + PACKAGE_SHA256
        ),
        controller_release_tree_sha256="8" * 64,
        controller_release_package_manifest_path=(
            "/opt/governed-memory-controller/releases/"
            + PACKAGE_SHA256
            + "/ops/governed_memory/installation/current/package_manifest.json"
        ),
        controller_runtime_interpreter_path=RUNTIME_ROOT + "/bin/python",
        controller_runtime_interpreter_sha256="c" * 64,
        controller_runtime_inventory_path=(
            RUNTIME_ROOT + "/controller-distributions.json"
        ),
        controller_runtime_inventory_sha256="d" * 64,
        controller_requirements_lock_sha256="e" * 64,
        supervisor_launcher_path=(
            "/opt/governed-memory-controller/releases/"
            + PACKAGE_SHA256
            + "/tools/governed_memory_install/store_supervisor_launcher.py"
        ),
        supervisor_launcher_sha256="f" * 64,
        local_images=images,
        expected_images=expected_images,
    )


class _Clock:
    def read_utc(self) -> datetime:
        return datetime(2026, 8, 12, 12, 5, tzinfo=timezone.utc)


class _Journal:
    def __init__(self) -> None:
        self.records: list[JournalRecord] = []
        self.fail_before_terminal_applied = False

    def journal_records(self) -> tuple[JournalRecord, ...]:
        return tuple(self.records)

    def append_journal(self, record: JournalRecord) -> None:
        if (
            self.fail_before_terminal_applied
            and record.step_id == STORES_ONLY_PLAN[-1].step_id
            and record.event == JournalEvent.APPLIED.value
        ):
            self.fail_before_terminal_applied = False
            raise RuntimeError("synthetic_crash_before_terminal_applied_append")
        self.records.append(record)


class _TypedHost:
    def __init__(self) -> None:
        self.states = {
            step.step_id: ("after" if step.invariant_only else "before")
            for step in STORES_ONLY_PLAN
        }
        self.operations: list[str] = []
        self.tamper_ownership = False
        self.omit_identity_step: str | None = None
        self.fail_apply_step: str | None = None
        self.fail_after_compensate_step: str | None = None
        self.tamper_resource_labels = False

    @staticmethod
    def _ownership(request: HostOperationRequest, revision: str) -> str:
        return ClaimBoundInstallBackend._expected_ownership(request, revision)

    def observe(self, request: HostOperationRequest) -> HostObservation:
        revision = hashlib.sha256(
            (request.step_id + ":" + self.states[request.step_id]).encode()
        ).hexdigest()
        ownership = self._ownership(request, revision)
        if self.tamper_ownership:
            ownership = "0" * 64
        after = self.states[request.step_id] == "after"
        result = self._result(request) if after else HostApplyResult(())
        return HostObservation(
            self.states[request.step_id],
            revision,
            ownership,
            result.identities,
            result.postflight_receipt_sha256,
        )

    def _result(self, request: HostOperationRequest) -> HostApplyResult:
        identities: list[HostResourceIdentityReceipt] = []
        selected_targets = request.resource_targets
        if request.step_id == self.omit_identity_step:
            selected_targets = selected_targets[:-1]
        for target in selected_targets:
            is_container = target.resource_kind == "container"
            has_labels = target.resource_kind in {"container", "network", "volume"}
            if "postgres" in target.resource_name:
                image_id = "sha256:" + "2" * 64
                repo_digest = expectations_from_store_spec(
                    _static_store_spec()
                )[0].repo_digest
            else:
                image_id = "sha256:" + "4" * 64
                repo_digest = expectations_from_store_spec(
                    _static_store_spec()
                )[1].repo_digest
            resource_id = (
                hashlib.sha256(
                    (target.resource_kind + ":" + target.resource_name).encode(
                        "ascii"
                    )
                ).hexdigest()
                if is_container
                else "resource-" + hashlib.sha256(
                    target.resource_name.encode("ascii")
                ).hexdigest()[:24]
            )
            ownership = ClaimBoundInstallBackend.expected_resource_ownership(
                request,
                resource_kind=target.resource_kind,
                resource_name=target.resource_name,
                resource_id=resource_id,
                resource_labels_sha256=target.resource_labels_sha256,
                image_id=image_id if is_container else None,
                image_repo_digest=repo_digest if is_container else None,
            )
            identities.append(
                HostResourceIdentityReceipt(
                    resource_kind=target.resource_kind,
                    resource_name=target.resource_name,
                    resource_id=resource_id,
                    ownership_sha256=ownership,
                    resource_labels_sha256=(
                        (
                            "0" * 64
                            if self.tamper_resource_labels and has_labels
                            else target.resource_labels_sha256
                        )
                        if has_labels
                        else None
                    ),
                    image_id=image_id if is_container else None,
                    image_repo_digest=repo_digest if is_container else None,
                )
            )
        return HostApplyResult(
            tuple(identities),
            "f" * 64
            if request.step_id
            == "I19_COLD_RESTART_AND_VERIFY_TERMINAL_POSTFLIGHT"
            else None,
        )

    def apply(
        self, request: HostOperationRequest, expected: HostObservation
    ) -> HostApplyResult:
        if request.step_id == self.fail_apply_step:
            self.fail_apply_step = None
            raise RuntimeError("synthetic install failure")
        self.operations.append(request.profile.value)
        self.states[request.step_id] = "after"
        return self._result(request)

    def compensate(
        self, request: HostOperationRequest, expected: HostObservation
    ) -> HostApplyResult:
        self.operations.append(request.profile.value)
        self.states[request.step_id] = "before"
        if request.step_id == self.fail_after_compensate_step:
            self.fail_after_compensate_step = None
            raise RuntimeError("synthetic crash after compensation effect")
        return HostApplyResult(())


class InstallationCompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.identity_fixture = _Fixture(
            Path(self.temporary.name) / "identity-fixture"
        )
        self.identity_binding = self.identity_fixture.claimed_binding()
        self.identity_evidence = _claimed_execution_binding_evidence(
            self.identity_binding
        )
        identity_parent = Path(
            self.identity_evidence.resource_identity_ledger_path
        ).parent
        identity_parent.mkdir(parents=True, mode=0o700)
        os.chmod(identity_parent, 0o700)
        directory = Path(self.temporary.name) / "lock"
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)
        self.lock = GlobalExecutionLock(directory / "execution.lock")

    def tearDown(self) -> None:
        self.lock.close()
        self.identity_fixture.close()
        self.temporary.cleanup()

    def _identity_ledger(self) -> ResourceIdentityLedger:
        path = Path(self.identity_evidence.resource_identity_ledger_path)
        return ResourceIdentityLedger(
            path,
            claimed_execution_binding=self.identity_binding,
            authority_state=self.identity_fixture.state,
            held_lock=self.identity_fixture.execution_lock.held_capability(),
            create=not path.exists(),
        )

    def _backend(self) -> tuple[ClaimBoundInstallBackend, _Journal, _TypedHost]:
        journal = _Journal()
        host = _TypedHost()
        readiness = _Readiness()
        resolved_store_spec = _resolved_store_spec(
            self.identity_evidence.journal_binding_sha256
        )
        backend = ClaimBoundInstallBackend(
            claimed_execution_binding=_binding(
                journal_binding_sha256=(
                    self.identity_evidence.journal_binding_sha256
                ),
                resolved_store_spec=resolved_store_spec,
            ),
            journal=journal,
            host_operations=host,
            readiness_probe=readiness,
            prerequisites=_prerequisites(),
            resource_identity_ledger=self._identity_ledger(),
            resolved_store_spec=resolved_store_spec,
        )
        return backend, journal, host

    @staticmethod
    def _managed_resources(spec: dict[str, object]) -> tuple[dict[str, object], ...]:
        resources = spec["resources"]
        return (
            resources["network"],
            resources["volumes"]["postgres"],
            resources["volumes"]["qdrant"],
            resources["containers"]["postgres"],
            resources["containers"]["qdrant"],
        )

    def test_prebind_requires_exact_dynamic_placeholders_on_every_resource(
        self,
    ) -> None:
        static = _static_store_spec()
        validate_store_spec(static, allow_placeholders=True)
        dynamic_labels = {
            "lifeswitch.governed-memory.authorization-id": "${AUTHORIZATION_ID}",
            "lifeswitch.governed-memory.authorization-nonce-sha256": (
                "${AUTHORIZATION_NONCE_SHA256}"
            ),
            "lifeswitch.governed-memory.execution-binding-sha256": (
                "${EXECUTION_BINDING_SHA256}"
            ),
            "lifeswitch.governed-memory.execution-id": "${EXECUTION_ID}",
            "lifeswitch.governed-memory.package-manifest-sha256": (
                "${PACKAGE_MANIFEST_SHA256}"
            ),
        }
        for resource in self._managed_resources(static):
            self.assertEqual(
                {key: resource["labels"][key] for key in dynamic_labels},
                dynamic_labels,
            )
        for key, wrong_value in (
            ("lifeswitch.governed-memory.authorization-id", "static-auth"),
            ("lifeswitch.governed-memory.execution-id", "1" * 64),
            (
                "lifeswitch.governed-memory.package-manifest-sha256",
                "2" * 64,
            ),
        ):
            with self.subTest(label=key):
                wrong = deepcopy(static)
                self._managed_resources(wrong)[0]["labels"][key] = wrong_value
                with self.assertRaisesRegex(
                    LinuxPlanError,
                    "store_spec_label_execution_binding_invalid",
                ):
                    validate_store_spec(wrong, allow_placeholders=True)

    def test_bound_labels_equal_exact_execution_identity_on_every_resource(
        self,
    ) -> None:
        resolved = _resolved_store_spec()
        expected = {
            "lifeswitch.governed-memory.authorization-id": AUTHORIZATION_ID,
            "lifeswitch.governed-memory.authorization-nonce-sha256": (
                AUTHORIZATION_NONCE_SHA256
            ),
            "lifeswitch.governed-memory.execution-binding-sha256": (
                JOURNAL_BINDING_SHA256
            ),
            "lifeswitch.governed-memory.execution-id": "7" * 64,
            "lifeswitch.governed-memory.package-manifest-sha256": PACKAGE_SHA256,
        }
        for resource in self._managed_resources(resolved):
            self.assertEqual(
                {key: resource["labels"][key] for key in expected},
                expected,
            )

    def test_postbind_rejects_wrong_missing_extra_or_unresolved_labels(
        self,
    ) -> None:
        resolved = _resolved_store_spec()
        mutations = (
            (
                "wrong_authority",
                lambda labels: labels.__setitem__(
                    "lifeswitch.governed-memory.authorization-id",
                    "wrong-auth",
                ),
                "store_spec_label_execution_binding_invalid",
            ),
            (
                "wrong_execution",
                lambda labels: labels.__setitem__(
                    "lifeswitch.governed-memory.execution-id", "8" * 64
                ),
                "store_spec_label_execution_binding_invalid",
            ),
            (
                "wrong_package",
                lambda labels: labels.__setitem__(
                    "lifeswitch.governed-memory.package-manifest-sha256",
                    "9" * 64,
                ),
                "store_spec_label_execution_binding_invalid",
            ),
            (
                "placeholder_residue",
                lambda labels: labels.__setitem__(
                    "lifeswitch.governed-memory.execution-id", "${EXECUTION_ID}"
                ),
                "store_spec_label_execution_binding_invalid",
            ),
            (
                "missing",
                lambda labels: labels.pop(
                    "lifeswitch.governed-memory.package-manifest-sha256"
                ),
                "store_spec_labels_invalid",
            ),
            (
                "extra",
                lambda labels: labels.__setitem__(
                    "lifeswitch.governed-memory.unbound", "value"
                ),
                "store_spec_labels_invalid",
            ),
        )
        for name, mutate, error in mutations:
            with self.subTest(mutation=name):
                wrong = deepcopy(resolved)
                labels = self._managed_resources(wrong)[0]["labels"]
                mutate(labels)
                with self.assertRaisesRegex(LinuxPlanError, error):
                    validate_store_spec(wrong, allow_placeholders=False)

    def test_closed_typed_backend_runs_exact_nineteen_step_plan(self) -> None:
        backend, unused_journal, host = self._backend()
        receipt = DormantStoreInstallController(
            plan=STORES_ONLY_PLAN,
            backend=backend,
            held_lock=self.lock.held_capability(),
        ).run(attempt_id=backend.attempt_id)
        self.assertEqual(receipt.outcome, "inactive_stores_installation_complete")
        self.assertEqual(len(receipt.applied_step_ids), 19)
        self.assertIsNotNone(backend.fresh_empty_store_readiness)
        self.assertEqual(
            backend.fresh_empty_store_readiness.production_read_count, 0
        )
        self.assertIsNone(backend.terminal_canonical_store_readiness)
        self.assertEqual(
            host.operations, [step.effect for step in STORES_ONLY_PLAN]
        )
        records = backend.resource_identity_records()
        self.assertEqual(len(records), len(ROLLBACK_RESOURCE_KEYS))
        self.assertTrue(all(record.event == "created" for record in records))
        self.assertNotIn("secret_value", repr(records).lower())
        self.assertFalse(any("docker" in operation for operation in host.operations))
        resumed = DormantStoreInstallController(
            plan=STORES_ONLY_PLAN,
            backend=backend,
            held_lock=self.lock.held_capability(),
        ).run(attempt_id=backend.attempt_id)
        self.assertEqual(resumed, receipt)
        self.assertEqual(len(host.operations), 19)

    def test_effect_without_every_exact_identity_refuses_before_ledger_append(
        self,
    ) -> None:
        backend, unused_journal, host = self._backend()
        host.omit_identity_step = (
            "I04_WRITE_RESOLVED_STORE_SPEC_AND_GENERATE_FRESH_STORE_SECRETS"
        )
        step = STORES_ONLY_PLAN[3]
        self.assertEqual(backend.probe(step).value, "before")
        with self.assertRaisesRegex(
            InstallBackendError, "install_host_identity_target_mismatch"
        ):
            backend.apply(step)
        self.assertEqual(backend.resource_identity_records(), ())

    def test_claim_bound_compensation_records_exact_removed_identities(
        self,
    ) -> None:
        backend, unused_journal, host = self._backend()
        host.fail_apply_step = "I11_BOOTSTRAP_CANONICAL_DATABASE"
        with self.assertRaises(InstallationCompensatedError):
            DormantStoreInstallController(
                plan=STORES_ONLY_PLAN,
                backend=backend,
                held_lock=self.lock.held_capability(),
            ).run(attempt_id=backend.attempt_id)
        records = backend.resource_identity_records()
        created = [record for record in records if record.event == "created"]
        removed = [record for record in records if record.event == "removed"]
        self.assertEqual(len(created), 8)
        self.assertEqual(len(removed), 8)
        self.assertEqual(
            {(record.resource_kind, record.resource_name) for record in created},
            {(record.resource_kind, record.resource_name) for record in removed},
        )

    def test_compensation_effect_crash_recovers_ledger_without_reapply(
        self,
    ) -> None:
        backend, unused_journal, host = self._backend()
        host.fail_apply_step = "I11_BOOTSTRAP_CANONICAL_DATABASE"
        host.fail_after_compensate_step = "I09_CREATE_EXACT_QDRANT_CONTAINER"
        controller = DormantStoreInstallController(
            plan=STORES_ONLY_PLAN,
            backend=backend,
            held_lock=self.lock.held_capability(),
        )
        with self.assertRaises(CompensationFailedError):
            controller.run(attempt_id=backend.attempt_id)
        count = host.operations.count("remove_exact_qdrant_container")
        receipt = controller.run(attempt_id=backend.attempt_id)
        self.assertEqual(
            receipt.outcome, "same_attempt_compensation_complete"
        )
        self.assertEqual(
            host.operations.count("remove_exact_qdrant_container"), count
        )
        latest = {
            (record.resource_kind, record.resource_name): record
            for record in backend.resource_identity_records()
        }
        self.assertTrue(latest)
        self.assertTrue(all(record.event == "removed" for record in latest.values()))

    def test_multi_resource_compensation_ledger_crash_resumes_remaining_append(
        self,
    ) -> None:
        backend, unused_journal, host = self._backend()
        host.fail_apply_step = "I11_BOOTSTRAP_CANONICAL_DATABASE"
        controller = DormantStoreInstallController(
            plan=STORES_ONLY_PLAN,
            backend=backend,
            held_lock=self.lock.held_capability(),
        )
        real_append = ResourceIdentityLedger.append
        secret_removals = 0

        def fail_second_secret_removal(
            ledger: ResourceIdentityLedger, *args: object, **kwargs: object
        ):
            nonlocal secret_removals
            if (
                kwargs.get("event") == "removed"
                and kwargs.get("resource_kind") == "secret_file"
            ):
                secret_removals += 1
                if secret_removals == 2:
                    raise ResourceIdentityError("injected_second_append_failure")
            return real_append(ledger, *args, **kwargs)

        with mock.patch.object(
            ResourceIdentityLedger,
            "append",
            new=fail_second_secret_removal,
        ):
            with self.assertRaises(CompensationFailedError):
                controller.run(attempt_id=backend.attempt_id)

        self.assertEqual(secret_removals, 2)
        self.assertEqual(
            host.operations.count(
                "remove_resolved_store_spec_and_fresh_store_secrets"
            ),
            1,
        )
        receipt = controller.run(attempt_id=backend.attempt_id)
        self.assertEqual(receipt.outcome, "same_attempt_compensation_complete")
        self.assertEqual(
            host.operations.count(
                "remove_resolved_store_spec_and_fresh_store_secrets"
            ),
            1,
        )
        latest = {
            (record.resource_kind, record.resource_name): record
            for record in backend.resource_identity_records()
        }
        self.assertTrue(latest)
        self.assertTrue(all(record.event == "removed" for record in latest.values()))

    def test_probe_apply_requires_attempt_bound_ownership(self) -> None:
        backend, unused_journal, host = self._backend()
        host.tamper_ownership = True
        with self.assertRaisesRegex(
            InstallBackendError, "install_host_observation_unowned"
        ):
            backend.probe(STORES_ONLY_PLAN[0])

    def test_host_cannot_substitute_resolved_docker_labels(self) -> None:
        backend, unused_journal, host = self._backend()
        host.tamper_resource_labels = True
        step = STORES_ONLY_PLAN[5]
        self.assertEqual(backend.probe(step).value, "before")
        with self.assertRaisesRegex(
            InstallBackendError, "install_host_identity_target_mismatch"
        ):
            backend.apply(step)
        self.assertEqual(backend.resource_identity_records(), ())

    def test_backend_rejects_resolved_store_spec_drift(self) -> None:
        resolved = _resolved_store_spec(
            self.identity_evidence.journal_binding_sha256
        )
        resolved["resources"]["network"]["labels"][
            "lifeswitch.governed-memory.authorization-id"
        ] = "different-auth"
        with self.assertRaisesRegex(
            InstallBackendError, "install_resolved_store_spec_invalid"
        ):
            ClaimBoundInstallBackend(
                claimed_execution_binding=_binding(
                    journal_binding_sha256=(
                        self.identity_evidence.journal_binding_sha256
                    ),
                    resolved_store_spec=resolved,
                ),
                journal=_Journal(),
                host_operations=_TypedHost(),
                readiness_probe=_Readiness(),
                prerequisites=_prerequisites(),
                resource_identity_ledger=self._identity_ledger(),
                resolved_store_spec=resolved,
            )

    def test_readiness_rejects_boolean_numeric_fields(self) -> None:
        constructors = (
            lambda: PrebootstrapPostgreSQLReadiness(
                "127.0.0.1:55436",
                False,
                POSTGRES_SERVER_VERSION,
                "postgres",
                "governed_memory",
                False,
                REQUIRED_ROLE_NAMES,
                (),
                0,
                "1" * 64,
            ),
            lambda: PrebootstrapPostgreSQLReadiness(
                "127.0.0.1:55436",
                16,
                POSTGRES_SERVER_VERSION,
                "postgres",
                "governed_memory",
                0,
                REQUIRED_ROLE_NAMES,
                (),
                0,
                "1" * 64,
            ),
            lambda: QdrantReadiness(
                "127.0.0.1:6347",
                "1.19.0",
                "governed_memory_9a54cf123493_000005",
                "governed_memory_active",
                False,
                False,
                0,
                "2" * 64,
            ),
            lambda: EmptyStoreReadiness.create(
                PrebootstrapPostgreSQLReadiness(
                    "127.0.0.1:55436",
                    16,
                    POSTGRES_SERVER_VERSION,
                    "postgres",
                    "governed_memory",
                    False,
                    REQUIRED_ROLE_NAMES,
                    (),
                    0,
                    "1" * 64,
                ),
                QdrantReadiness(
                    "127.0.0.1:6347",
                    "1.19.0",
                    "governed_memory_9a54cf123493_000005",
                    "governed_memory_active",
                    False,
                    0,
                    0,
                    "2" * 64,
                ),
                provider_call_count=False,
            ),
            lambda: TerminalPostgreSQLReadiness(
                "127.0.0.1:55436",
                16,
                POSTGRES_SERVER_VERSION,
                "governed_memory",
                TERMINAL_MIGRATION_IDS,
                "7" * 64,
                "8" * 64,
                False,
                0,
                0,
                "9" * 64,
            ),
            lambda: TerminalQdrantReadiness(
                "127.0.0.1:6347",
                "1.19.0",
                "governed_memory_9a54cf123493_000005",
                "governed_memory_active",
                False,
                "governed_memory_9a54cf123493_000005",
                EXPECTED_QDRANT_COLLECTION_CONFIG_SHA256,
                0,
                0,
                0,
                "a" * 64,
            ),
            lambda: TerminalQdrantReadiness(
                "127.0.0.1:6347",
                "1.19.0",
                "governed_memory_9a54cf123493_000005",
                "governed_memory_active",
                True,
                "wrong_collection",
                EXPECTED_QDRANT_COLLECTION_CONFIG_SHA256,
                0,
                0,
                0,
                "a" * 64,
            ),
            lambda: TerminalCanonicalStoreReadiness.create(
                _terminal_readiness().postgres,
                _terminal_readiness().qdrant,
                provider_call_count=False,
            ),
        )
        for constructor in constructors:
            with self.subTest(constructor=constructor), self.assertRaises(
                StoreReadinessError
            ):
                constructor()

    def test_terminal_effect_with_exact_intent_recovers_without_reapply(self) -> None:
        backend, journal, host = self._backend()
        journal.fail_before_terminal_applied = True
        controller = DormantStoreInstallController(
            plan=STORES_ONLY_PLAN,
            backend=backend,
            held_lock=self.lock.held_capability(),
        )
        with self.assertRaises(CompletedStateError):
            controller.run(attempt_id=backend.attempt_id)
        terminal_effect_count = host.operations.count(
            STORES_ONLY_PLAN[-1].effect
        )
        receipt = controller.run(attempt_id=backend.attempt_id)
        self.assertEqual(receipt.outcome, "inactive_stores_installation_complete")
        self.assertEqual(
            host.operations.count(STORES_ONLY_PLAN[-1].effect),
            terminal_effect_count,
        )

    def test_inactive_entrypoint_refuses_before_dependencies(self) -> None:
        with self.assertRaisesRegex(InstallEntrypointError, INACTIVE_REFUSAL_CODE):
            run_authorized_dormant_store_install(
                verified_scope_capability=None,
                verified_package_capability=None,
                verified_controller_runtime_capability=None,
                authority_state=None,  # type: ignore[arg-type]
                clock=None,  # type: ignore[arg-type]
                held_lock=self.lock.held_capability(),
                prerequisites=None,  # type: ignore[arg-type]
                receipt_store=None,  # type: ignore[arg-type]
            )

    def test_public_install_entrypoint_has_no_factory_injection_surface(
        self,
    ) -> None:
        parameters = set(
            inspect.signature(
                run_authorized_dormant_store_install
            ).parameters
        )
        self.assertEqual(
            parameters,
            {
                "verified_scope_capability",
                "verified_package_capability",
                "verified_controller_runtime_capability",
                "authority_state",
                "clock",
                "held_lock",
                "prerequisites",
                "receipt_store",
            },
        )
        self.assertTrue(
            {
                "journal_factory",
                "resource_identity_ledger_factory",
                "dependencies_factory",
                "transport_factory",
                "secret_source",
                "postgres_receipt_sink",
            }.isdisjoint(parameters)
        )

    def test_public_install_receipt_sink_factory_is_parameter_closed(self) -> None:
        parameters = set(
            inspect.signature(
                production_install_postgres_stage_receipt_sink
            ).parameters
        )
        self.assertEqual(
            parameters,
            {
                "receipt_store",
                "claimed_execution_binding",
                "held_lock",
            },
        )
        self.assertTrue(
            {
                "allowed_mode",
                "marker_state",
                "artifact",
                "path",
                "sink_factory",
                "execution_id",
            }.isdisjoint(parameters)
        )
        with self.assertRaisesRegex(
            InstallEntrypointError,
            "postgres_receipt_sink_invalid",
        ):
            production_install_postgres_stage_receipt_sink(
                receipt_store=DurableReceiptStore.synthetic(
                    Path(self.temporary.name).resolve()
                ),
                claimed_execution_binding=self.identity_binding,
                held_lock=(
                    self.identity_fixture.execution_lock.held_capability()
                ),
            )
        with self.assertRaisesRegex(
            InstallEntrypointError,
            "postgres_receipt_sink_invalid",
        ):
            production_install_postgres_stage_receipt_sink(
                receipt_store=DurableReceiptStore.production(),
                claimed_execution_binding=self.identity_binding,
                held_lock=(
                    self.identity_fixture.execution_lock.held_capability()
                ),
            )
        production_execution_root = (
            Path("/var/lib/governed-memory-controller/executions-v5")
            / self.identity_evidence.execution_id
        )
        production_ledger_path = (
            production_execution_root / "resources.jsonl"
        )
        production_binding = _ClaimedExecutionBinding(
            replace(
                self.identity_evidence,
                execution_journal_path=str(
                    production_execution_root / "journal.jsonl"
                ),
                resource_identity_ledger_path=str(production_ledger_path),
                resource_identity_ledger_path_sha256=hashlib.sha256(
                    str(production_ledger_path).encode("utf-8")
                ).hexdigest(),
            ),
            _CLAIMED_EXECUTION_TOKEN,
        )
        production_sink = production_install_postgres_stage_receipt_sink(
            receipt_store=DurableReceiptStore.production(),
            claimed_execution_binding=production_binding,
            held_lock=self.identity_fixture.execution_lock.held_capability(),
        )
        self.assertEqual(
            type(production_sink).__name__,
            "StoreBackedPostgreSQLStageReceiptSink",
        )
        self.assertFalse(
            production_sink.postgres_controller_authority_marker_held()
        )
        with self.assertRaisesRegex(
            InstallEntrypointError,
            "postgres_receipt_sink_invalid",
        ):
            production_install_postgres_stage_receipt_sink(
                receipt_store=DurableReceiptStore.production(),
                claimed_execution_binding=production_binding,
                held_lock=self.lock.held_capability(),
            )

    def test_production_dependencies_select_exact_closed_factories(self) -> None:
        sink = mock.sentinel.postgres_receipt_sink
        transports = mock.sentinel.production_transports
        dependencies = ClaimBoundInstallDependencies(_TypedHost(), _Readiness())
        transport_constructor = mock.Mock(return_value=transports)
        dependencies_instance = mock.Mock(return_value=dependencies)
        dependencies_constructor = mock.Mock(
            return_value=dependencies_instance
        )
        held_lock = self.identity_fixture.execution_lock.held_capability()
        runtime_capability = mock.sentinel.runtime_capability
        prerequisites = _prerequisites()
        journal = mock.sentinel.journal
        ledger = mock.sentinel.resource_identity_ledger
        receipt_store = DurableReceiptStore.production()
        resolved_store_spec = _resolved_store_spec(
            self.identity_evidence.journal_binding_sha256
        )
        artifacts = {"sealed": b"artifact"}

        with (
            mock.patch.object(
                install_entrypoint,
                "production_install_postgres_stage_receipt_sink",
                return_value=sink,
            ) as sink_factory,
            mock.patch.object(
                linux_live_adapters,
                "ProductionLinuxStoreTransportFactory",
                transport_constructor,
            ),
            mock.patch.object(
                linux_store_effects,
                "LinuxInstallDependenciesFactory",
                dependencies_constructor,
            ),
        ):
            selected = install_entrypoint._production_install_dependencies(
                claimed_execution_binding=self.identity_binding,
                resolved_store_spec=resolved_store_spec,
                verified_artifacts=artifacts,
                verified_controller_runtime_capability=runtime_capability,
                prerequisites=prerequisites,
                journal=journal,  # type: ignore[arg-type]
                resource_identity_ledger=ledger,  # type: ignore[arg-type]
                held_lock=held_lock,
                receipt_store=receipt_store,
            )

        self.assertIs(selected, dependencies)
        sink_factory.assert_called_once_with(
            receipt_store=receipt_store,
            claimed_execution_binding=self.identity_binding,
            held_lock=held_lock,
        )
        transport_constructor.assert_called_once_with(
            held_lock=held_lock,
            verified_controller_runtime_capability=runtime_capability,
            postgres_receipt_sink=sink,
        )
        dependencies_constructor.assert_called_once_with(
            transport_factory=transports,
            expected_postgres_catalog_sha256=(
                APPROVED_TERMINAL_CATALOG_SHA256
            ),
        )
        self.assertNotIn(
            "secret_source",
            dependencies_constructor.call_args.kwargs,
        )
        dependencies_instance.assert_called_once_with(
            claimed_execution_binding=self.identity_binding,
            resolved_store_spec=resolved_store_spec,
            verified_artifacts=artifacts,
            prerequisites=prerequisites,
            journal=journal,
            resource_identity_ledger=ledger,
        )

    def test_entrypoint_emits_receipt_only_after_terminal_journal_and_ledger(
        self,
    ) -> None:
        root = Path(self.temporary.name)
        state_dir = root / "authority"
        execution_dir = (root / "executions").resolve()
        state_dir.mkdir(mode=0o700)
        execution_dir.mkdir(mode=0o700)
        os.chmod(state_dir, 0o700)
        os.chmod(execution_dir, 0o700)
        receipt_store = DurableReceiptStore.synthetic(execution_dir)
        state = AuthorityState(state_dir / "authority.sqlite3", create=True)
        contract = {
            "exact_targets": {
                "global_lock": str(self.lock.path),
                "nonce_state": str(state.path),
                "resource_identity_ledger": str(
                    execution_dir / "{execution_id}" / "resources.jsonl"
                ),
                "execution_journal": str(
                    execution_dir / "{execution_id}" / "journal.jsonl"
                ),
            }
        }
        plan = {
            "install_steps": [
                {
                    "id": step.step_id,
                    "effect": step.effect,
                    "rollback": step.rollback,
                    "observation_requires": list(
                        step.observation_requires
                    ),
                    "proof_lifetime": step.proof_lifetime,
                }
                for step in STORES_ONLY_PLAN
            ]
        }
        prerequisites = _prerequisites()
        artifacts = _synthetic_package_artifacts(contract, plan)
        manifest = {
            "schema_version": "test-package-v1",
            "state": "inactive",
            "artifacts": {
                path: hashlib.sha256(raw).hexdigest()
                for path, raw in artifacts.items()
            },
        }
        manifest_raw = _canonical(manifest)
        scope = {
            "candidate_git_commit": "1" * 40,
            "candidate_git_tree": "2" * 40,
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
                _canonical(contract["exact_targets"])
            ).hexdigest(),
            "controller_runtime_receipt_sha256": RUNTIME_RECEIPT_SHA256,
        }
        scope_raw = _canonical(scope)
        scope_evidence = authority.CryptographicallyValidScopeNotExecution(
            result_type="cryptographically_valid_scope_not_execution",
            operation="dormant_install",
            authorization_namespace="test",
            thread_id="test-thread",
            scope_id="test-scope",
            authorization_id="test-auth",
            key_id=HASH_A,
            nonce="N" * 48,
            scope_sha256=hashlib.sha256(scope_raw).hexdigest(),
            authorization_sha256=HASH_A,
            trust_bundle_sha256=HASH_B,
            not_before="2026-08-12T12:00:00Z",
            expires_at="2026-08-12T12:10:00Z",
        )
        scope_capability = authority._VerifiedDormantInstallCapability(
            scope_evidence, authority._EXECUTION_CAPABILITY_TOKEN
        )
        package_capability = verify_install_package_capability(
            scope_capability,
            signed_scope_json=scope_raw,
            package_manifest_json=manifest_raw,
            artifact_bytes=artifacts,
        )
        runtime_capability = _synthetic_runtime_capability(
            scope["package_manifest_sha256"], artifacts
        )
        prerequisites = replace(
            prerequisites,
            store_spec_sha256=hashlib.sha256(
                artifacts[
                    "ops/governed_memory/installation/store_spec-v5.json"
                ]
            ).hexdigest(),
            controller_requirements_lock_sha256=hashlib.sha256(
                artifacts[
                    "ops/governed_memory/controller-requirements.lock"
                ]
            ).hexdigest(),
            supervisor_launcher_path=(
                "/opt/governed-memory-controller/releases/"
                + scope["package_manifest_sha256"]
                + "/tools/governed_memory_install/"
                "store_supervisor_launcher.py"
            ),
            controller_release_root=(
                "/opt/governed-memory-controller/releases/"
                + scope["package_manifest_sha256"]
            ),
            controller_release_package_manifest_path=(
                "/opt/governed-memory-controller/releases/"
                + scope["package_manifest_sha256"]
                + "/ops/governed_memory/installation/current/package_manifest.json"
            ),
            supervisor_launcher_sha256=hashlib.sha256(
                artifacts[
                    "tools/governed_memory_install/"
                    "store_supervisor_launcher.py"
                ]
            ).hexdigest(),
        )
        host = _TypedHost()
        readiness_probe = _Readiness()

        def dependencies_factory(**unused: object) -> ClaimBoundInstallDependencies:
            return ClaimBoundInstallDependencies(host, readiness_probe)

        def journal_factory(claimed: object) -> DurableJournal:
            evidence = _claimed_execution_binding_evidence(claimed)
            path = Path(evidence.execution_journal_path)
            path.parent.mkdir(mode=0o700, exist_ok=True)
            os.chmod(path.parent, 0o700)
            return DurableJournal(
                path,
                claimed_execution_binding=claimed,
                authority_state=state,
                held_lock=self.lock.held_capability(),
                create=not path.exists(),
            )

        def ledger_factory(claimed: object) -> ResourceIdentityLedger:
            evidence = _claimed_execution_binding_evidence(claimed)
            path = Path(evidence.resource_identity_ledger_path)
            return ResourceIdentityLedger(
                path,
                claimed_execution_binding=claimed,
                authority_state=state,
                held_lock=self.lock.held_capability(),
                create=not path.exists(),
            )

        with self.assertRaisesRegex(
            InstallEntrypointError,
            "dormant_install_composition_dependency_invalid",
        ):
            run_authorized_dormant_store_install(
                verified_scope_capability=scope_capability,
                verified_package_capability=package_capability,
                verified_controller_runtime_capability=runtime_capability,
                authority_state=state,
                clock=_Clock(),
                held_lock=self.lock.held_capability(),
                prerequisites=prerequisites,
                receipt_store=receipt_store,
            )
        self.assertEqual(host.operations, [])

        receipt = _run_authorized_dormant_store_install_synthetic(
            verified_scope_capability=scope_capability,
            verified_package_capability=package_capability,
            verified_controller_runtime_capability=runtime_capability,
            authority_state=state,
            clock=_Clock(),
            held_lock=self.lock.held_capability(),
            journal_factory=journal_factory,
            resource_identity_ledger_factory=ledger_factory,
            dependencies_factory=dependencies_factory,
            prerequisites=prerequisites,
            receipt_store=receipt_store,
        )
        verified = verify_install_receipt(receipt)
        install_identity = verified_dormant_install_authority_identity(
            scope_capability
        )
        self.assertEqual(verified["execution_id"], install_identity.execution_id)
        canonical_claim = state.claim_nonce(
            "N" * 48,
            operation=install_identity.operation,
            execution_sha256=install_identity.execution_sha256,
            authorization_sha256=install_identity.authorization_sha256,
            scope_sha256=install_identity.scope_sha256,
            trust_bundle_sha256=install_identity.trust_bundle_sha256,
            held_lock=self.lock.held_capability(),
            allow_new_claim=False,
        )
        self.assertEqual(canonical_claim.result, "exact_execution_resumed")
        self.assertEqual(
            (
                canonical_claim.nonce_sha256,
                canonical_claim.operation_sha256,
                canonical_claim.execution_sha256,
                canonical_claim.authorization_sha256,
                canonical_claim.scope_sha256,
                canonical_claim.trust_bundle_sha256,
                canonical_claim.claim_sha256,
            ),
            (
                install_identity.authorization_nonce_sha256,
                install_identity.operation_sha256,
                install_identity.execution_sha256,
                install_identity.authorization_sha256,
                install_identity.scope_sha256,
                install_identity.trust_bundle_sha256,
                install_identity.claim_sha256,
            ),
        )
        self.assertEqual(verified["resource_ledger_sequence"], 15)
        self.assertEqual(verified["journal_sequence"], 38)
        self.assertEqual(
            verified["terminal_store_readiness_sha256"],
            backend_readiness_sha := _terminal_readiness().receipt_sha256,
        )
        self.assertRegex(backend_readiness_sha, r"^[0-9a-f]{64}$")
        self.assertEqual(readiness_probe.fresh_calls, 1)
        self.assertEqual(readiness_probe.terminal_calls, 1)
        resumed = _resume_authorized_dormant_store_install_synthetic(
            verified_scope_capability=scope_capability,
            verified_package_capability=package_capability,
            verified_controller_runtime_capability=runtime_capability,
            authority_state=state,
            clock=_Clock(),
            held_lock=self.lock.held_capability(),
            journal_factory=journal_factory,
            resource_identity_ledger_factory=ledger_factory,
            dependencies_factory=dependencies_factory,
            prerequisites=prerequisites,
            receipt_store=receipt_store,
        )
        self.assertEqual(resumed, verified)
        self.assertEqual(len(host.operations), 19)
        self.assertEqual(readiness_probe.fresh_calls, 1)
        self.assertEqual(readiness_probe.terminal_calls, 2)

        production_journal_calls: list[str] = []
        production_dependencies_calls: list[str] = []
        production_held_lock = self.lock.held_capability()

        def production_journal_and_ledger(
            claimed: object,
            *,
            authority_state: AuthorityState,
            held_lock: object,
        ) -> tuple[DurableJournal, ResourceIdentityLedger]:
            evidence = _claimed_execution_binding_evidence(claimed)
            self.assertIs(authority_state, state)
            self.assertIs(held_lock, production_held_lock)
            production_journal_calls.append(evidence.execution_id)
            return journal_factory(claimed), ledger_factory(claimed)

        def production_dependencies(
            **kwargs: object,
        ) -> ClaimBoundInstallDependencies:
            evidence = _claimed_execution_binding_evidence(
                kwargs["claimed_execution_binding"]
            )
            self.assertIs(
                kwargs["verified_controller_runtime_capability"],
                runtime_capability,
            )
            self.assertIs(kwargs["prerequisites"], prerequisites)
            self.assertIs(kwargs["receipt_store"], receipt_store)
            production_dependencies_calls.append(evidence.execution_id)
            return dependencies_factory(**kwargs)

        with (
            mock.patch.object(
                DurableReceiptStore,
                "require_production_binding",
                autospec=True,
                return_value=None,
            ),
            mock.patch.object(
                install_entrypoint,
                "_production_install_journal_and_ledger",
                side_effect=production_journal_and_ledger,
            ),
            mock.patch.object(
                install_entrypoint,
                "_production_install_dependencies",
                side_effect=production_dependencies,
            ),
        ):
            public_receipt = resume_authorized_dormant_store_install(
                verified_scope_capability=scope_capability,
                verified_package_capability=package_capability,
                verified_controller_runtime_capability=runtime_capability,
                authority_state=state,
                clock=_Clock(),
                held_lock=production_held_lock,
                prerequisites=prerequisites,
                receipt_store=receipt_store,
            )
            public_resumed = resume_authorized_dormant_store_install(
                verified_scope_capability=scope_capability,
                verified_package_capability=package_capability,
                verified_controller_runtime_capability=runtime_capability,
                authority_state=state,
                clock=_Clock(),
                held_lock=production_held_lock,
                prerequisites=prerequisites,
                receipt_store=receipt_store,
            )
        self.assertEqual(public_receipt, verified)
        self.assertEqual(public_resumed, verified)
        self.assertEqual(len(production_journal_calls), 2)
        self.assertEqual(len(set(production_journal_calls)), 1)
        self.assertEqual(production_dependencies_calls, production_journal_calls)
        self.assertEqual(len(host.operations), 19)
        self.assertEqual(readiness_probe.fresh_calls, 1)
        self.assertEqual(readiness_probe.terminal_calls, 4)

        readiness_probe.terminal_user_row_count = 1
        with self.assertRaisesRegex(
            InstallEntrypointError, "dormant_install_backend_refused"
        ):
            _resume_authorized_dormant_store_install_synthetic(
                verified_scope_capability=scope_capability,
                verified_package_capability=package_capability,
                verified_controller_runtime_capability=runtime_capability,
                authority_state=state,
                clock=_Clock(),
                held_lock=self.lock.held_capability(),
                journal_factory=journal_factory,
                resource_identity_ledger_factory=ledger_factory,
                dependencies_factory=dependencies_factory,
                prerequisites=prerequisites,
                receipt_store=receipt_store,
            )
        self.assertEqual(readiness_probe.terminal_calls, 5)
        self.assertEqual(len(host.operations), 19)

    def test_package_capability_closes_and_rehashes_all_artifacts(self) -> None:
        contract = {
            "exact_targets": {
                "global_lock": "/tmp/execution.lock",
                "nonce_state": "/tmp/authority.sqlite3",
                "resource_identity_ledger": (
                    "/tmp/executions/{execution_id}/resources.jsonl"
                ),
                "execution_journal": "/tmp/executions/{execution_id}/journal.jsonl",
            }
        }
        plan = {
            "install_steps": [
                {"id": step.step_id, "effect": step.effect, "rollback": step.rollback}
                for step in STORES_ONLY_PLAN
            ]
        }
        artifacts = _synthetic_package_artifacts(contract, plan)
        manifest = {
            "schema_version": "test-package-v1",
            "state": "inactive",
            "artifacts": {
                path: hashlib.sha256(raw).hexdigest()
                for path, raw in artifacts.items()
            },
        }
        manifest_raw = _canonical(manifest)
        scope = {
            "candidate_git_commit": "1" * 40,
            "candidate_git_tree": "2" * 40,
            "package_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
            "controller_contract_sha256": hashlib.sha256(
                artifacts["ops/governed_memory/installation/current/contract.json"]
            ).hexdigest(),
            "execution_plan_sha256": hashlib.sha256(
                artifacts["ops/governed_memory/installation/current/controller_plan.json"]
            ).hexdigest(),
            "exact_targets_sha256": hashlib.sha256(
                _canonical(contract["exact_targets"])
            ).hexdigest(),
            "controller_runtime_receipt_sha256": RUNTIME_RECEIPT_SHA256,
        }
        scope_raw = _canonical(scope)
        scope_evidence = authority.CryptographicallyValidScopeNotExecution(
            result_type="cryptographically_valid_scope_not_execution",
            operation="dormant_install",
            authorization_namespace="test",
            thread_id="test-thread",
            scope_id="test-scope",
            authorization_id="test-auth",
            key_id=HASH_A,
            nonce="N" * 48,
            scope_sha256=hashlib.sha256(scope_raw).hexdigest(),
            authorization_sha256=HASH_A,
            trust_bundle_sha256=HASH_B,
            not_before="2026-08-12T12:00:00Z",
            expires_at="2026-08-12T12:10:00Z",
        )
        scope_cap = authority._VerifiedDormantInstallCapability(
            scope_evidence, authority._EXECUTION_CAPABILITY_TOKEN
        )
        package_cap = verify_install_package_capability(
            scope_cap,
            signed_scope_json=scope_raw,
            package_manifest_json=manifest_raw,
            artifact_bytes=artifacts,
        )
        evidence, unused_scope, closed = _package_capability_parts(package_cap)
        self.assertEqual(evidence.artifact_count, 7)
        self.assertEqual(set(closed), set(artifacts))
        tampered = dict(artifacts)
        tampered[next(iter(tampered))] += b"x"
        with self.assertRaisesRegex(
            PackageCapabilityError, "package_artifact_hash_mismatch"
        ):
            verify_install_package_capability(
                scope_cap,
                signed_scope_json=scope_raw,
                package_manifest_json=manifest_raw,
                artifact_bytes=tampered,
            )


if __name__ == "__main__":
    unittest.main()
