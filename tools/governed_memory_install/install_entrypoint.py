from __future__ import annotations

"""Non-CLI, authorization-gated dormant-store install composition.

Importing this module has no side effects.  The sole callable install surface
requires opaque verifier capabilities, the held global lock, durable authority
state, verified prerequisites, and the canonical production receipt store.
It selects the root-owned journal, resource ledger, PostgreSQL receipt sink,
Linux transports, and closed host operations internally after authority claim.
"""

import hashlib
import os
from enum import Enum
from pathlib import Path
import stat
from typing import Final, Mapping, Protocol
import json

from .authority_state import AuthorityState
from .controller import DormantStoreInstallController, STORES_ONLY_PLAN, ControllerReceipt
from .execution_authority import TrustedUtcClock
from .execution_capability import (
    ClaimedExecutionBindingError,
    _claimed_execution_binding_evidence,
    claim_dormant_store_install_execution_binding,
    resume_dormant_store_install_execution_binding,
)
from .execution_lock import (
    HeldExecutionLockCapability,
    validate_held_execution_lock,
)
from .controller_runtime import (
    ControllerRuntimeCapabilityError,
    _controller_runtime_capability_evidence,
)
from .install_backend import (
    ClaimBoundInstallDependencies,
    ClaimBoundInstallBackend,
    InstallBackendError,
    InstallPrerequisites,
    JournalAdapter,
)
from .image_preflight import ImagePreflightError, expectations_from_store_spec
from .linux_plan import ExecutionBinding, LinuxPlanError, bind_store_spec
from .journal import DurableJournal
from .package_capability import (
    PackageCapabilityError,
    _package_capability_parts,
)
from .receipts import build_install_receipt, canonical_json_bytes
from .durable_receipts import (
    DurableReceiptError,
    DurableReceiptStore,
    PRODUCTION_EXECUTIONS_ROOT,
    ReceiptArtifact,
    _open_directory_nofollow,
)
from .psycopg_postgres_adapter import PostgreSQLStageReceiptSink
from .resource_identity import ResourceIdentityLedger
from .rollback import (
    EmptyRollbackError,
    derive_exact_rollback_resources_from_ledger,
)


INACTIVE_REFUSAL_CODE: Final = "inactive_installation_package_not_authorized"
_PRODUCTION_JOURNAL_NAME: Final = "journal.jsonl"
_PRODUCTION_RESOURCE_LEDGER_NAME: Final = "resources.jsonl"


class InstallEntrypointError(RuntimeError):
    """Content-free refusal at the only install composition surface."""


class InstallAuthorityMode(str, Enum):
    START_INSTALL = "start-install"
    RESUME_INSTALL = "resume-install"


class JournalFactory(Protocol):
    def __call__(self, claimed_execution_binding: object) -> JournalAdapter: ...


class ResourceIdentityLedgerFactory(Protocol):
    def __call__(
        self, claimed_execution_binding: object
    ) -> ResourceIdentityLedger: ...


class ClaimBoundInstallDependenciesFactory(Protocol):
    def __call__(
        self,
        *,
        claimed_execution_binding: object,
        resolved_store_spec: Mapping[str, object],
        verified_artifacts: Mapping[str, bytes],
        prerequisites: InstallPrerequisites,
        journal: JournalAdapter,
        resource_identity_ledger: ResourceIdentityLedger,
    ) -> ClaimBoundInstallDependencies: ...


def production_install_postgres_stage_receipt_sink(
    *,
    receipt_store: DurableReceiptStore,
    claimed_execution_binding: object,
    held_lock: HeldExecutionLockCapability,
) -> PostgreSQLStageReceiptSink:
    """Return the exact install-mode PostgreSQL native-stage receipt sink.

    The process-local rollback-marker state remains private.  Install receipts
    cannot claim a rollback marker and the caller cannot select the sink mode,
    implementation, filename, receipt artifact, or execution identifier.
    """

    try:
        if type(receipt_store) is not DurableReceiptStore:
            raise InstallEntrypointError(
                "dormant_install_postgres_receipt_sink_invalid"
            )
        receipt_store.require_production_binding()
        validate_held_execution_lock(held_lock)
        evidence = _claimed_execution_binding_evidence(
            claimed_execution_binding
        )
        execution_root = PRODUCTION_EXECUTIONS_ROOT / evidence.execution_id
        if hashlib.sha256(
            str(held_lock._owner.path).encode("utf-8")
        ).hexdigest() != evidence.global_lock_path_sha256 or (
            Path(evidence.execution_journal_path)
            != execution_root / _PRODUCTION_JOURNAL_NAME
        ) or (
            Path(evidence.resource_identity_ledger_path)
            != execution_root / _PRODUCTION_RESOURCE_LEDGER_NAME
        ):
            raise InstallEntrypointError(
                "dormant_install_postgres_receipt_sink_invalid"
            )
        from .rollback_live_adapter import (
            StoreBackedPostgreSQLStageReceiptSink,
            _ProductionControllerAuthorityMarkerState,
        )

        return StoreBackedPostgreSQLStageReceiptSink(
            receipt_store=receipt_store,
            execution_id=evidence.execution_id,
            marker_state=_ProductionControllerAuthorityMarkerState(
                evidence.execution_id
            ),
            allowed_mode="install",
        )
    except Exception as error:
        raise InstallEntrypointError(
            "dormant_install_postgres_receipt_sink_invalid"
        ) from error


def _prepare_production_execution_directory(
    claimed_execution_binding: object,
    *,
    held_lock: HeldExecutionLockCapability,
) -> tuple[Path, bool, Path, bool]:
    """Create/reverify only the claim-bound root-owned execution directory."""

    try:
        validate_held_execution_lock(held_lock)
        evidence = _claimed_execution_binding_evidence(
            claimed_execution_binding
        )
    except Exception as error:
        raise InstallEntrypointError(
            "dormant_install_production_execution_directory_invalid"
        ) from error
    execution_root = PRODUCTION_EXECUTIONS_ROOT / evidence.execution_id
    journal_path = execution_root / _PRODUCTION_JOURNAL_NAME
    ledger_path = execution_root / _PRODUCTION_RESOURCE_LEDGER_NAME
    if (
        Path(evidence.execution_journal_path) != journal_path
        or Path(evidence.resource_identity_ledger_path) != ledger_path
    ):
        raise InstallEntrypointError(
            "dormant_install_production_artifact_path_mismatch"
        )
    root_fd = execution_fd = -1
    created = False
    try:
        root_fd = _open_directory_nofollow(
            PRODUCTION_EXECUTIONS_ROOT,
            expected_uid=0,
        )
        root = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root.st_mode)
            or root.st_uid != 0
            or root.st_gid != 0
            or stat.S_IMODE(root.st_mode) != 0o700
        ):
            raise InstallEntrypointError(
                "dormant_install_production_executions_root_invalid"
            )
        try:
            os.mkdir(evidence.execution_id, 0o700, dir_fd=root_fd)
            created = True
        except FileExistsError:
            pass
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if nofollow == 0:
            raise InstallEntrypointError(
                "dormant_install_production_nofollow_unavailable"
            )
        execution_fd = os.open(
            evidence.execution_id,
            os.O_RDONLY
            | os.O_CLOEXEC
            | nofollow
            | getattr(os, "O_DIRECTORY", 0),
            dir_fd=root_fd,
        )
        opened = os.fstat(execution_fd)
        named = os.stat(
            evidence.execution_id,
            dir_fd=root_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o700
            or opened.st_uid != 0
            or opened.st_gid != 0
            or named.st_uid != 0
            or named.st_gid != 0
            or (opened.st_dev, opened.st_ino)
            != (named.st_dev, named.st_ino)
        ):
            raise InstallEntrypointError(
                "dormant_install_production_execution_directory_invalid"
            )
        if created:
            os.fsync(execution_fd)
            os.fsync(root_fd)

        existence: dict[str, bool] = {}
        for name in (
            _PRODUCTION_JOURNAL_NAME,
            _PRODUCTION_RESOURCE_LEDGER_NAME,
        ):
            try:
                observed = os.stat(
                    name,
                    dir_fd=execution_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                existence[name] = False
                continue
            if (
                not stat.S_ISREG(observed.st_mode)
                or stat.S_IMODE(observed.st_mode) != 0o600
                or observed.st_uid != 0
                or observed.st_gid != 0
                or observed.st_nlink != 1
            ):
                raise InstallEntrypointError(
                    "dormant_install_production_artifact_file_invalid"
                )
            existence[name] = True
        if (
            existence[_PRODUCTION_RESOURCE_LEDGER_NAME]
            and not existence[_PRODUCTION_JOURNAL_NAME]
        ):
            raise InstallEntrypointError(
                "dormant_install_production_artifact_prefix_invalid"
            )
        validate_held_execution_lock(held_lock)
        return (
            journal_path,
            existence[_PRODUCTION_JOURNAL_NAME],
            ledger_path,
            existence[_PRODUCTION_RESOURCE_LEDGER_NAME],
        )
    except InstallEntrypointError:
        raise
    except Exception as error:
        raise InstallEntrypointError(
            "dormant_install_production_execution_directory_invalid"
        ) from error
    finally:
        if execution_fd >= 0:
            os.close(execution_fd)
        if root_fd >= 0:
            os.close(root_fd)


def _production_install_journal_and_ledger(
    claimed_execution_binding: object,
    *,
    authority_state: AuthorityState,
    held_lock: HeldExecutionLockCapability,
) -> tuple[DurableJournal, ResourceIdentityLedger]:
    journal_path, journal_exists, ledger_path, ledger_exists = (
        _prepare_production_execution_directory(
            claimed_execution_binding,
            held_lock=held_lock,
        )
    )
    journal: DurableJournal | None = None
    try:
        journal = DurableJournal(
            journal_path,
            claimed_execution_binding=claimed_execution_binding,
            authority_state=authority_state,
            held_lock=held_lock,
            expected_uid=0,
            create=not journal_exists,
        )
        ledger = ResourceIdentityLedger(
            ledger_path,
            claimed_execution_binding=claimed_execution_binding,
            authority_state=authority_state,
            held_lock=held_lock,
            expected_uid=0,
            create=not ledger_exists,
        )
        for path in (journal_path, ledger_path):
            observed = path.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(observed.st_mode)
                or stat.S_IMODE(observed.st_mode) != 0o600
                or observed.st_uid != 0
                or observed.st_gid != 0
                or observed.st_nlink != 1
            ):
                raise InstallEntrypointError(
                    "dormant_install_production_audit_artifact_invalid"
                )
        return journal, ledger
    except Exception as error:
        if journal is not None:
            journal.close()
        raise InstallEntrypointError(
            "dormant_install_production_audit_artifact_invalid"
        ) from error


def _production_install_dependencies(
    *,
    claimed_execution_binding: object,
    resolved_store_spec: Mapping[str, object],
    verified_artifacts: Mapping[str, bytes],
    verified_controller_runtime_capability: object,
    prerequisites: InstallPrerequisites,
    journal: DurableJournal,
    resource_identity_ledger: ResourceIdentityLedger,
    held_lock: HeldExecutionLockCapability,
    receipt_store: DurableReceiptStore,
) -> ClaimBoundInstallDependencies:
    try:
        from .linux_live_adapters import ProductionLinuxStoreTransportFactory
        from .linux_store_effects import LinuxInstallDependenciesFactory
        from .postgres_native_stages import APPROVED_TERMINAL_CATALOG_SHA256

        receipt_sink = production_install_postgres_stage_receipt_sink(
            receipt_store=receipt_store,
            claimed_execution_binding=claimed_execution_binding,
            held_lock=held_lock,
        )
        transport_factory = ProductionLinuxStoreTransportFactory(
            held_lock=held_lock,
            verified_controller_runtime_capability=(
                verified_controller_runtime_capability
            ),
            postgres_receipt_sink=receipt_sink,
        )
        return LinuxInstallDependenciesFactory(
            transport_factory=transport_factory,
            expected_postgres_catalog_sha256=(
                APPROVED_TERMINAL_CATALOG_SHA256
            ),
        )(
            claimed_execution_binding=claimed_execution_binding,
            resolved_store_spec=resolved_store_spec,
            verified_artifacts=verified_artifacts,
            prerequisites=prerequisites,
            journal=journal,
            resource_identity_ledger=resource_identity_ledger,
        )
    except InstallEntrypointError:
        raise
    except Exception as error:
        raise InstallEntrypointError(
            "dormant_install_production_dependencies_invalid"
        ) from error


def _run_authorized_dormant_store_install(
    *,
    verified_scope_capability: object,
    verified_package_capability: object,
    verified_controller_runtime_capability: object,
    authority_state: AuthorityState,
    clock: TrustedUtcClock,
    held_lock: HeldExecutionLockCapability,
    journal_factory: JournalFactory | None,
    dependencies_factory: ClaimBoundInstallDependenciesFactory | None,
    prerequisites: InstallPrerequisites,
    resource_identity_ledger_factory: ResourceIdentityLedgerFactory | None,
    receipt_store: DurableReceiptStore,
    authority_mode: InstallAuthorityMode,
    require_production_receipt_store: bool,
) -> Mapping[str, object]:
    """Shared production/synthetic composition over the sealed current plan."""

    if (
        verified_scope_capability is None
        or verified_package_capability is None
        or verified_controller_runtime_capability is None
    ):
        raise InstallEntrypointError(INACTIVE_REFUSAL_CODE)
    if (
        type(prerequisites) is not InstallPrerequisites
        or type(receipt_store) is not DurableReceiptStore
        or type(authority_mode) is not InstallAuthorityMode
        or type(require_production_receipt_store) is not bool
        or (
            require_production_receipt_store
            and any(
                value is not None
                for value in (
                    journal_factory,
                    dependencies_factory,
                    resource_identity_ledger_factory,
                )
            )
        )
        or (
            not require_production_receipt_store
            and not all(
                callable(value)
                for value in (
                    journal_factory,
                    dependencies_factory,
                    resource_identity_ledger_factory,
                )
            )
        )
    ):
        raise InstallEntrypointError(
            "dormant_install_composition_dependency_invalid"
        )
    try:
        if require_production_receipt_store:
            receipt_store.require_production_binding()
        else:
            receipt_store.require_synthetic_binding()
    except DurableReceiptError as error:
        raise InstallEntrypointError(
            "dormant_install_composition_dependency_invalid"
        ) from error
    try:
        package_evidence, _unused_scope, artifacts = (
            _package_capability_parts(verified_package_capability)
        )
        runtime_evidence = _controller_runtime_capability_evidence(
            verified_controller_runtime_capability
        )
        if (
            runtime_evidence.package_manifest_sha256
            != package_evidence.package_manifest_sha256
            or runtime_evidence.controller_runtime_receipt_sha256
            != package_evidence.controller_runtime_receipt_sha256
        ):
            raise InstallEntrypointError(
                "dormant_install_controller_runtime_binding_mismatch"
            )
        store_spec = json.loads(
            artifacts[
                "ops/governed_memory/installation/store_spec-v2.json"
            ].decode("ascii")
        )
        expected_images = expectations_from_store_spec(store_spec)
        if prerequisites.expected_images != expected_images:
            raise InstallEntrypointError(
                "dormant_install_image_expectation_binding_mismatch"
            )
        prerequisite_runtime = (
            prerequisites.controller_runtime_receipt_sha256,
            prerequisites.controller_runtime_root,
            prerequisites.controller_runtime_tree_sha256,
            prerequisites.controller_release_root,
            prerequisites.controller_release_tree_sha256,
            prerequisites.controller_release_package_manifest_path,
            prerequisites.controller_runtime_interpreter_path,
            prerequisites.controller_runtime_interpreter_sha256,
            prerequisites.controller_runtime_inventory_path,
            prerequisites.controller_runtime_inventory_sha256,
            prerequisites.controller_requirements_lock_sha256,
            prerequisites.supervisor_launcher_path,
            prerequisites.supervisor_launcher_sha256,
        )
        verified_runtime = (
            runtime_evidence.controller_runtime_receipt_sha256,
            runtime_evidence.runtime_root,
            runtime_evidence.runtime_tree_sha256,
            runtime_evidence.release_root,
            runtime_evidence.release_tree_sha256,
            runtime_evidence.package_manifest_path,
            runtime_evidence.interpreter_path,
            runtime_evidence.interpreter_sha256,
            runtime_evidence.inventory_path,
            runtime_evidence.installed_distribution_inventory_sha256,
            runtime_evidence.controller_requirements_lock_sha256,
            runtime_evidence.supervisor_launcher_path,
            runtime_evidence.supervisor_launcher_sha256,
        )
        if prerequisite_runtime != verified_runtime:
            raise InstallEntrypointError(
                "dormant_install_controller_runtime_prerequisite_mismatch"
            )
        authority_function = (
            resume_dormant_store_install_execution_binding
            if authority_mode is InstallAuthorityMode.RESUME_INSTALL
            else claim_dormant_store_install_execution_binding
        )
        claimed = authority_function(
            verified_scope_capability,
            verified_package_capability=verified_package_capability,
            verified_controller_runtime_capability=(
                verified_controller_runtime_capability
            ),
            state=authority_state,
            clock=clock,
            held_lock=held_lock,
        )
        evidence = _claimed_execution_binding_evidence(claimed)
        if (
            authority_mode is InstallAuthorityMode.START_INSTALL
            and evidence.claim_result != "execution_authority_claimed"
        ):
            raise InstallEntrypointError(
                "dormant_install_start_already_claimed"
            )
        if (
            authority_mode is InstallAuthorityMode.RESUME_INSTALL
            and evidence.claim_result != "execution_authority_exact_resume"
        ):
            raise InstallEntrypointError(
                "dormant_install_resume_claim_invalid"
            )
        resolved_store_spec = bind_store_spec(
            store_spec,
            ExecutionBinding(
                binding_sha256=evidence.journal_binding_sha256,
                authorization_id=evidence.authorization_id,
                authorization_nonce_sha256=(
                    evidence.authorization_nonce_sha256
                ),
                execution_id=evidence.execution_id,
                package_manifest_sha256=evidence.package_manifest_sha256,
            ),
        )
        if hashlib.sha256(
            canonical_json_bytes(resolved_store_spec)
        ).hexdigest() != evidence.resolved_store_spec_sha256:
            raise InstallEntrypointError(
                "dormant_install_resolved_store_spec_binding_mismatch"
            )
    except InstallEntrypointError:
        raise
    except (
        ClaimedExecutionBindingError,
        ControllerRuntimeCapabilityError,
        PackageCapabilityError,
        ImagePreflightError,
        UnicodeError,
        json.JSONDecodeError,
        KeyError,
        LinuxPlanError,
    ) as error:
        raise InstallEntrypointError(
            "dormant_install_execution_authority_refused"
        ) from error
    journal: DurableJournal | None = None
    try:
        if require_production_receipt_store:
            journal, resource_identity_ledger = (
                _production_install_journal_and_ledger(
                    claimed,
                    authority_state=authority_state,
                    held_lock=held_lock,
                )
            )
        else:
            assert journal_factory is not None
            assert resource_identity_ledger_factory is not None
            journal = journal_factory(claimed)
            resource_identity_ledger = (
                resource_identity_ledger_factory(claimed)
            )
        if type(journal) is not DurableJournal:
            raise InstallEntrypointError(
                "dormant_install_durable_journal_required"
            )
        if (
            type(resource_identity_ledger) is not ResourceIdentityLedger
            or not resource_identity_ledger.secure_execution_mode
        ):
            raise InstallEntrypointError(
                "dormant_install_secure_resource_ledger_required"
            )
        if require_production_receipt_store:
            dependencies = _production_install_dependencies(
                claimed_execution_binding=claimed,
                resolved_store_spec=resolved_store_spec,
                verified_artifacts=artifacts,
                verified_controller_runtime_capability=(
                    verified_controller_runtime_capability
                ),
                prerequisites=prerequisites,
                journal=journal,
                resource_identity_ledger=resource_identity_ledger,
                held_lock=held_lock,
                receipt_store=receipt_store,
            )
        else:
            assert dependencies_factory is not None
            dependencies = dependencies_factory(
                claimed_execution_binding=claimed,
                resolved_store_spec=resolved_store_spec,
                verified_artifacts=artifacts,
                prerequisites=prerequisites,
                journal=journal,
                resource_identity_ledger=resource_identity_ledger,
            )
        if type(dependencies) is not ClaimBoundInstallDependencies:
            raise InstallEntrypointError(
                "dormant_install_claim_bound_dependencies_invalid"
            )
        backend = ClaimBoundInstallBackend(
            claimed_execution_binding=claimed,
            journal=journal,
            host_operations=dependencies.host_operations,
            readiness_probe=dependencies.readiness_probe,
            prerequisites=prerequisites,
            resource_identity_ledger=resource_identity_ledger,
            resolved_store_spec=resolved_store_spec,
        )
        controller = DormantStoreInstallController(
            plan=STORES_ONLY_PLAN,
            backend=backend,
            held_lock=held_lock,
        )
        controller_receipt = controller.run(attempt_id=evidence.attempt_id)
        if (
            controller_receipt.outcome
            != "inactive_stores_installation_complete"
            or controller_receipt.applied_step_ids
            != tuple(step.step_id for step in STORES_ONLY_PLAN)
            or controller_receipt.compensated_step_ids
            or controller_receipt.plan_sha256
            != evidence.controller_model_sha256
        ):
            raise InstallEntrypointError(
                "dormant_install_terminal_controller_receipt_invalid"
            )
        journal_records = backend.journal_records()
        if (
            len(journal_records) != controller_receipt.journal_sequence
            or not journal_records
            or journal_records[-1].record_sha256
            != controller_receipt.journal_head_sha256
        ):
            raise InstallEntrypointError(
                "dormant_install_terminal_journal_unconfirmed"
            )
        resource_records = backend.resource_identity_records()
        if not resource_records:
            raise InstallEntrypointError(
                "dormant_install_resource_ledger_empty"
            )
        ledger_head = resource_records[-1].entry_sha256
        derive_exact_rollback_resources_from_ledger(
            resource_records,
            package_manifest_sha256=evidence.package_manifest_sha256,
            expected_ledger_head_sha256=ledger_head,
        )
        if (
            backend.resource_identity_records() != resource_records
            or backend.journal_records() != journal_records
            or backend.postflight_receipt_sha256 is None
        ):
            raise InstallEntrypointError(
                "dormant_install_terminal_resume_reverification_failed"
            )
        # This must occur after the terminal journal and exact resource ledger
        # have both been reverified.  It deliberately runs on first completion
        # and every receipt replay; the pre-bootstrap fresh-empty proof from I10
        # is not valid evidence for a migrated canonical store.
        terminal_readiness = (
            backend.verify_terminal_canonical_store_readiness()
        )
        install_receipt = build_install_receipt(
            execution_id=evidence.execution_id,
            attempt_id=evidence.attempt_id,
            candidate_git_commit=evidence.candidate_git_commit,
            candidate_git_tree=evidence.candidate_git_tree,
            package_manifest_sha256=evidence.package_manifest_sha256,
            controller_runtime_receipt_sha256=(
                evidence.controller_runtime_receipt_sha256
            ),
            controller_runtime_root=evidence.controller_runtime_root,
            controller_runtime_tree_sha256=(
                evidence.controller_runtime_tree_sha256
            ),
            controller_release_root=evidence.controller_release_root,
            controller_release_tree_sha256=(
                evidence.controller_release_tree_sha256
            ),
            controller_release_package_manifest_path=(
                evidence.controller_release_package_manifest_path
            ),
            controller_runtime_interpreter_path=(
                evidence.controller_runtime_interpreter_path
            ),
            controller_runtime_interpreter_sha256=(
                evidence.controller_runtime_interpreter_sha256
            ),
            controller_runtime_inventory_path=(
                evidence.controller_runtime_inventory_path
            ),
            controller_runtime_inventory_sha256=(
                evidence.controller_runtime_inventory_sha256
            ),
            controller_requirements_lock_sha256=(
                evidence.controller_requirements_lock_sha256
            ),
            supervisor_launcher_path=evidence.supervisor_launcher_path,
            supervisor_launcher_sha256=(
                evidence.supervisor_launcher_sha256
            ),
            authorization_sha256=evidence.authorization_sha256,
            plan_sha256=controller_receipt.plan_sha256,
            journal_head_sha256=controller_receipt.journal_head_sha256,
            journal_sequence=controller_receipt.journal_sequence,
            resource_ledger_head_sha256=ledger_head,
            resource_ledger_sequence=len(resource_records),
            image_identity_set_sha256=prerequisites.local_images.receipt_sha256,
            postflight_receipt_sha256=backend.postflight_receipt_sha256,
            terminal_store_readiness_sha256=(
                terminal_readiness.receipt_sha256
            ),
        )
        durable_receipt = receipt_store.write_once(
            ReceiptArtifact.INSTALL,
            evidence.execution_id,
            install_receipt,
        )
        if (
            durable_receipt.receipt_sha256
            != install_receipt["receipt_sha256"]
            or dict(durable_receipt.canonical_receipt) != install_receipt
        ):
            raise InstallEntrypointError(
                "dormant_install_durable_receipt_unconfirmed"
            )
        return dict(durable_receipt.canonical_receipt)
    except (InstallBackendError, EmptyRollbackError, DurableReceiptError) as error:
        raise InstallEntrypointError("dormant_install_backend_refused") from error
    finally:
        close = getattr(journal, "close", None)
        if callable(close):
            close()


def _run_authorized_dormant_store_install_synthetic(
    *,
    verified_scope_capability: object,
    verified_package_capability: object,
    verified_controller_runtime_capability: object,
    authority_state: AuthorityState,
    clock: TrustedUtcClock,
    held_lock: HeldExecutionLockCapability,
    journal_factory: JournalFactory,
    dependencies_factory: ClaimBoundInstallDependenciesFactory,
    prerequisites: InstallPrerequisites,
    resource_identity_ledger_factory: ResourceIdentityLedgerFactory,
    receipt_store: DurableReceiptStore,
) -> Mapping[str, object]:
    """Private in-process test path accepting only a synthetic receipt store."""

    return _run_authorized_dormant_store_install(
        verified_scope_capability=verified_scope_capability,
        verified_package_capability=verified_package_capability,
        verified_controller_runtime_capability=(
            verified_controller_runtime_capability
        ),
        authority_state=authority_state,
        clock=clock,
        held_lock=held_lock,
        journal_factory=journal_factory,
        dependencies_factory=dependencies_factory,
        prerequisites=prerequisites,
        resource_identity_ledger_factory=resource_identity_ledger_factory,
        receipt_store=receipt_store,
        authority_mode=InstallAuthorityMode.START_INSTALL,
        require_production_receipt_store=False,
    )


def _resume_authorized_dormant_store_install_synthetic(
    *,
    verified_scope_capability: object,
    verified_package_capability: object,
    verified_controller_runtime_capability: object,
    authority_state: AuthorityState,
    clock: TrustedUtcClock,
    held_lock: HeldExecutionLockCapability,
    journal_factory: JournalFactory,
    dependencies_factory: ClaimBoundInstallDependenciesFactory,
    prerequisites: InstallPrerequisites,
    resource_identity_ledger_factory: ResourceIdentityLedgerFactory,
    receipt_store: DurableReceiptStore,
) -> Mapping[str, object]:
    """Private synthetic exact-resume path; it cannot create a nonce claim."""

    return _run_authorized_dormant_store_install(
        verified_scope_capability=verified_scope_capability,
        verified_package_capability=verified_package_capability,
        verified_controller_runtime_capability=(
            verified_controller_runtime_capability
        ),
        authority_state=authority_state,
        clock=clock,
        held_lock=held_lock,
        journal_factory=journal_factory,
        dependencies_factory=dependencies_factory,
        prerequisites=prerequisites,
        resource_identity_ledger_factory=resource_identity_ledger_factory,
        receipt_store=receipt_store,
        authority_mode=InstallAuthorityMode.RESUME_INSTALL,
        require_production_receipt_store=False,
    )


def run_authorized_dormant_store_install(
    *,
    verified_scope_capability: object,
    verified_package_capability: object,
    verified_controller_runtime_capability: object,
    authority_state: AuthorityState,
    clock: TrustedUtcClock,
    held_lock: HeldExecutionLockCapability,
    prerequisites: InstallPrerequisites,
    receipt_store: DurableReceiptStore,
) -> Mapping[str, object]:
    """Execute through the fixed root-owned production Linux composition.

    The caller supplies verified capabilities, immutable prerequisite evidence,
    the held global lock, authority state, trusted time, and the canonical
    receipt store.  It cannot select a journal, ledger, transport, secret
    source, PostgreSQL receipt sink, readiness probe, or host operation.
    """

    return _run_authorized_dormant_store_install(
        verified_scope_capability=verified_scope_capability,
        verified_package_capability=verified_package_capability,
        verified_controller_runtime_capability=(
            verified_controller_runtime_capability
        ),
        authority_state=authority_state,
        clock=clock,
        held_lock=held_lock,
        journal_factory=None,
        dependencies_factory=None,
        prerequisites=prerequisites,
        resource_identity_ledger_factory=None,
        receipt_store=receipt_store,
        authority_mode=InstallAuthorityMode.START_INSTALL,
        require_production_receipt_store=True,
    )


def resume_authorized_dormant_store_install(
    *,
    verified_scope_capability: object,
    verified_package_capability: object,
    verified_controller_runtime_capability: object,
    authority_state: AuthorityState,
    clock: TrustedUtcClock,
    held_lock: HeldExecutionLockCapability,
    prerequisites: InstallPrerequisites,
    receipt_store: DurableReceiptStore,
) -> Mapping[str, object]:
    """Resume only the exact previously claimed install execution."""

    return _run_authorized_dormant_store_install(
        verified_scope_capability=verified_scope_capability,
        verified_package_capability=verified_package_capability,
        verified_controller_runtime_capability=(
            verified_controller_runtime_capability
        ),
        authority_state=authority_state,
        clock=clock,
        held_lock=held_lock,
        journal_factory=None,
        dependencies_factory=None,
        prerequisites=prerequisites,
        resource_identity_ledger_factory=None,
        receipt_store=receipt_store,
        authority_mode=InstallAuthorityMode.RESUME_INSTALL,
        require_production_receipt_store=True,
    )


__all__ = [
    "INACTIVE_REFUSAL_CODE",
    "InstallAuthorityMode",
    "ClaimBoundInstallDependenciesFactory",
    "InstallEntrypointError",
    "ResourceIdentityLedgerFactory",
    "production_install_postgres_stage_receipt_sink",
    "run_authorized_dormant_store_install",
    "resume_authorized_dormant_store_install",
]
