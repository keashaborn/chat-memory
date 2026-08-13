from __future__ import annotations

"""Non-CLI, authorization-gated dormant-store install composition.

Importing this module has no side effects.  The sole callable install surface
requires both opaque verifier capabilities, the held global lock, durable
authority state, a durable journal factory, and closed typed host operations.
"""

import hashlib
from typing import Final, Mapping, Protocol
import json

from .authority_state import AuthorityState
from .controller import DormantStoreInstallController, STORES_ONLY_PLAN, ControllerReceipt
from .execution_authority import TrustedUtcClock
from .execution_capability import (
    ClaimedExecutionBindingError,
    _claimed_execution_binding_evidence,
    claim_dormant_store_install_execution_binding,
)
from .execution_lock import HeldExecutionLockCapability
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
    ReceiptArtifact,
)
from .resource_identity import ResourceIdentityLedger
from .rollback import (
    EmptyRollbackError,
    derive_exact_rollback_resources_from_ledger,
)


INACTIVE_REFUSAL_CODE: Final = "inactive_installation_package_not_authorized"


class InstallEntrypointError(RuntimeError):
    """Content-free refusal at the only install composition surface."""


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


def _run_authorized_dormant_store_install(
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
        not callable(journal_factory)
        or type(prerequisites) is not InstallPrerequisites
        or not callable(dependencies_factory)
        or not callable(resource_identity_ledger_factory)
        or type(receipt_store) is not DurableReceiptStore
        or type(require_production_receipt_store) is not bool
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
        package_evidence, unused_scope, artifacts = (
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
                "ops/governed_memory/installation/store_spec.json"
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
        claimed = claim_dormant_store_install_execution_binding(
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
    journal = journal_factory(claimed)
    try:
        if type(journal) is not DurableJournal:
            raise InstallEntrypointError(
                "dormant_install_durable_journal_required"
            )
        resource_identity_ledger = resource_identity_ledger_factory(claimed)
        if (
            type(resource_identity_ledger) is not ResourceIdentityLedger
            or not resource_identity_ledger.secure_execution_mode
        ):
            raise InstallEntrypointError(
                "dormant_install_secure_resource_ledger_required"
            )
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
    journal_factory: JournalFactory,
    dependencies_factory: ClaimBoundInstallDependenciesFactory,
    prerequisites: InstallPrerequisites,
    resource_identity_ledger_factory: ResourceIdentityLedgerFactory,
    receipt_store: DurableReceiptStore,
) -> Mapping[str, object]:
    """Claim and execute only with the canonical root-owned receipt store."""

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
        require_production_receipt_store=True,
    )


__all__ = [
    "INACTIVE_REFUSAL_CODE",
    "ClaimBoundInstallDependenciesFactory",
    "InstallEntrypointError",
    "ResourceIdentityLedgerFactory",
    "run_authorized_dormant_store_install",
]
