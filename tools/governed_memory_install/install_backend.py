from __future__ import annotations

"""Claim-bound adapter for the exact dormant-store controller plan.

The backend exposes no subprocess argv, shell, endpoint, or generic action.
All effects cross a closed typed host-operation boundary.  Each mutation must
consume the exact attempt-bound observation immediately produced for that
step, allowing an implementation to refuse probe/apply ownership drift.
"""

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Mapping, Protocol

from .controller import (
    CLAIM_BOUND_EXECUTION_MODE,
    JournalRecord,
    PlanStep,
    STORES_ONLY_PLAN,
    StepState,
    validate_plan,
)
from .execution_capability import (
    ClaimedExecutionBindingError,
    _claimed_execution_binding_evidence,
)
from .host_boundary import (
    HostApplyResult,
    HostBoundaryError,
    HostObservation,
    HostOperationProfile,
    HostOperationRequest,
    HostResourceIdentityReceipt,
    HostResourceTarget,
    TypedHostOperations,
)
from .image_preflight import (
    ImageExpectation,
    ImagePreflightError,
    LocalImageSetReadiness,
    validate_image_set_against_expectations,
)
from .linux_plan import (
    ExactStoreEnvironmentSet,
    LinuxPlanError,
    canonical_labels_sha256,
    validate_store_spec,
)
from .store_readiness import (
    EmptyStoreReadiness,
    StoreReadinessProbe,
    TerminalCanonicalStoreReadiness,
)
from .resource_identity import (
    ResourceIdentityError,
    ResourceIdentityLedger,
    ResourceIdentityRecord,
    latest_exact_resources,
    resource_ledger_binding_sha256,
)
from .rollback import expected_rollback_resource_names


class InstallBackendError(RuntimeError):
    """Content-free refusal from the claim-bound install composition."""


class JournalAdapter(Protocol):
    def journal_records(self) -> tuple[JournalRecord, ...]: ...
    def append_journal(self, record: JournalRecord) -> None: ...


@dataclass(frozen=True, slots=True)
class InstallPrerequisites:
    store_spec_sha256: str
    controller_runtime_receipt_sha256: str
    controller_runtime_root: str
    controller_runtime_tree_sha256: str
    controller_release_root: str
    controller_release_tree_sha256: str
    controller_release_package_manifest_path: str
    controller_runtime_interpreter_path: str
    controller_runtime_interpreter_sha256: str
    controller_runtime_inventory_path: str
    controller_runtime_inventory_sha256: str
    controller_requirements_lock_sha256: str
    supervisor_launcher_path: str
    supervisor_launcher_sha256: str
    local_images: LocalImageSetReadiness
    expected_images: tuple[ImageExpectation, ImageExpectation]
    environments: ExactStoreEnvironmentSet
    readiness_probe: StoreReadinessProbe

    def __post_init__(self) -> None:
        if (
            re.fullmatch(r"[0-9a-f]{64}", self.store_spec_sha256) is None
            or any(
                re.fullmatch(r"[0-9a-f]{64}", value) is None
                for value in (
                    self.controller_runtime_receipt_sha256,
                    self.controller_runtime_tree_sha256,
                    self.controller_release_tree_sha256,
                    self.controller_runtime_interpreter_sha256,
                    self.controller_runtime_inventory_sha256,
                    self.controller_requirements_lock_sha256,
                    self.supervisor_launcher_sha256,
                )
            )
            or self.controller_runtime_root
            != (
                "/opt/governed-memory-controller/runtimes/"
                + self.controller_runtime_receipt_sha256
            )
            or re.fullmatch(
                r"/opt/governed-memory-controller/releases/[0-9a-f]{64}",
                self.controller_release_root,
            )
            is None
            or self.controller_release_package_manifest_path
            != self.controller_release_root
            + "/ops/governed_memory/installation/current/package_manifest.json"
            or self.controller_runtime_interpreter_path
            != self.controller_runtime_root + "/bin/python"
            or self.controller_runtime_inventory_path
            != self.controller_runtime_root + "/controller-distributions.json"
            or self.supervisor_launcher_path
            != self.controller_release_root
            + "/tools/governed_memory_install/store_supervisor_launcher.py"
            or type(self.local_images) is not LocalImageSetReadiness
            or not isinstance(self.expected_images, tuple)
            or len(self.expected_images) != 2
            or type(self.environments) is not ExactStoreEnvironmentSet
            or not callable(
                getattr(self.readiness_probe, "verify_fresh_empty_stores", None)
            )
            or not callable(
                getattr(
                    self.readiness_probe,
                    "verify_terminal_canonical_stores",
                    None,
                )
            )
        ):
            raise InstallBackendError("install_prerequisites_invalid")
        try:
            validate_image_set_against_expectations(
                self.local_images, self.expected_images
            )
        except ImagePreflightError as error:
            raise InstallBackendError(
                "install_image_binding_invalid"
            ) from error


class ClaimBoundInstallBackend:
    """Concrete controller composition over injected typed operations."""

    execution_mode = CLAIM_BOUND_EXECUTION_MODE

    def __init__(
        self,
        *,
        claimed_execution_binding: object,
        journal: JournalAdapter,
        host_operations: TypedHostOperations,
        prerequisites: InstallPrerequisites,
        resource_identity_ledger: ResourceIdentityLedger,
        resolved_store_spec: Mapping[str, object],
    ) -> None:
        try:
            evidence = _claimed_execution_binding_evidence(
                claimed_execution_binding
            )
        except ClaimedExecutionBindingError as error:
            raise InstallBackendError("install_execution_binding_invalid") from error
        if evidence.controller_model_sha256 != validate_plan(STORES_ONLY_PLAN):
            raise InstallBackendError("install_controller_plan_mismatch")
        if (
            not callable(getattr(journal, "journal_records", None))
            or not callable(getattr(journal, "append_journal", None))
            or not callable(getattr(host_operations, "observe", None))
            or not callable(getattr(host_operations, "apply", None))
            or not callable(getattr(host_operations, "compensate", None))
            or type(prerequisites) is not InstallPrerequisites
            or type(resource_identity_ledger) is not ResourceIdentityLedger
        ):
            raise InstallBackendError("install_backend_dependency_invalid")
        if prerequisites.store_spec_sha256 != evidence.store_spec_sha256:
            raise InstallBackendError("install_store_spec_binding_mismatch")
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
        evidence_runtime = (
            evidence.controller_runtime_receipt_sha256,
            evidence.controller_runtime_root,
            evidence.controller_runtime_tree_sha256,
            evidence.controller_release_root,
            evidence.controller_release_tree_sha256,
            evidence.controller_release_package_manifest_path,
            evidence.controller_runtime_interpreter_path,
            evidence.controller_runtime_interpreter_sha256,
            evidence.controller_runtime_inventory_path,
            evidence.controller_runtime_inventory_sha256,
            evidence.controller_requirements_lock_sha256,
            evidence.supervisor_launcher_path,
            evidence.supervisor_launcher_sha256,
        )
        if prerequisite_runtime != evidence_runtime:
            raise InstallBackendError("install_controller_runtime_binding_mismatch")
        try:
            bound_spec = validate_store_spec(
                resolved_store_spec, allow_placeholders=False
            )
            resolved_store_spec_sha256 = hashlib.sha256(
                json.dumps(
                    bound_spec,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                ).encode("ascii")
            ).hexdigest()
        except (LinuxPlanError, TypeError, ValueError, UnicodeError) as error:
            raise InstallBackendError("install_resolved_store_spec_invalid") from error
        if resolved_store_spec_sha256 != evidence.resolved_store_spec_sha256:
            raise InstallBackendError("install_resolved_store_spec_binding_mismatch")
        execution_binding = bound_spec["execution_binding"]
        if execution_binding != {
            "authorization_id": evidence.authorization_id,
            "authorization_nonce_sha256": evidence.authorization_nonce_sha256,
            "binding_sha256": evidence.journal_binding_sha256,
            "execution_id": evidence.execution_id,
            "package_manifest_sha256": evidence.package_manifest_sha256,
        }:
            raise InstallBackendError("install_resolved_store_spec_authority_mismatch")
        resources = bound_spec["resources"]
        network = resources["network"]
        volumes = resources["volumes"]
        containers = resources["containers"]
        labeled_resources = (
            ("network", network),
            ("volume", volumes["postgres"]),
            ("volume", volumes["qdrant"]),
            ("container", containers["postgres"]),
            ("container", containers["qdrant"]),
        )
        self._expected_resource_labels_sha256 = {
            (kind, resource["name"]): canonical_labels_sha256(
                resource["labels"]
            )
            for kind, resource in labeled_resources
        }
        if any(
            (
                containers[logical_name]["image"]["reference"],
                containers[logical_name]["image"]["repo_digest"],
            )
            != (expectation.reference, expectation.repo_digest)
            for logical_name, expectation in zip(
                ("postgres", "qdrant"),
                prerequisites.expected_images,
                strict=True,
            )
        ):
            raise InstallBackendError("install_resolved_store_spec_image_mismatch")
        self._resolved_store_spec_sha256 = resolved_store_spec_sha256
        self._binding = evidence
        self._journal = journal
        self._host = host_operations
        self._prerequisites = prerequisites
        self._identity_ledger = resource_identity_ledger
        self._fresh_empty_readiness: EmptyStoreReadiness | None = None
        self._terminal_readiness: TerminalCanonicalStoreReadiness | None = None
        self._postflight_receipt_sha256: str | None = None
        self._observations: dict[tuple[str, bool], HostObservation] = {}
        try:
            self._identity_records = self._identity_ledger.records()
        except ResourceIdentityError as error:
            raise InstallBackendError(
                "install_resource_identity_ledger_invalid"
            ) from error
        if self._identity_ledger.binding_sha256 != resource_ledger_binding_sha256(
            evidence.journal_binding_sha256
        ):
            raise InstallBackendError(
                "install_resource_identity_ledger_binding_mismatch"
            )

    _STEP_RESOURCE_KEYS = {
        "I04_WRITE_RESOLVED_STORE_SPEC_AND_GENERATE_FRESH_STORE_SECRETS": (
            "resolved_store_spec",
            "qdrant_store_secret",
            "postgres_store_secret",
        ),
        "I05_CREATE_EXACT_NETWORK": ("network",),
        "I06_CREATE_EXACT_POSTGRES_VOLUME": ("postgres_volume",),
        "I07_CREATE_EXACT_QDRANT_VOLUME": ("qdrant_volume",),
        "I08_CREATE_EXACT_POSTGRES_CONTAINER": ("postgres_container",),
        "I09_CREATE_EXACT_QDRANT_CONTAINER": ("qdrant_container",),
        "I11_BOOTSTRAP_CANONICAL_DATABASE": ("canonical_database_and_roles",),
        "I12_APPLY_FOUNDATION_0001": ("migration_0001",),
        "I13_APPLY_OWNER_CLAIM_DETAIL_0003": ("migration_0003",),
        "I14_APPLY_PILOT_MARKER_0004": ("migration_0004",),
        "I15_CREATE_EMPTY_QDRANT_COLLECTION": ("qdrant_collection",),
        "I16_CREATE_QDRANT_ALIAS": ("qdrant_alias",),
        "I18_INSTALL_AND_ENABLE_STORES_SUPERVISOR": ("stores_supervisor",),
    }

    @property
    def attempt_id(self) -> str:
        return self._binding.attempt_id

    @property
    def fresh_empty_store_readiness(self) -> EmptyStoreReadiness | None:
        return self._fresh_empty_readiness

    @property
    def terminal_canonical_store_readiness(
        self,
    ) -> TerminalCanonicalStoreReadiness | None:
        return self._terminal_readiness

    @property
    def postflight_receipt_sha256(self) -> str | None:
        return self._postflight_receipt_sha256

    def resource_identity_records(self) -> tuple[ResourceIdentityRecord, ...]:
        try:
            records = self._identity_ledger.records()
        except ResourceIdentityError as error:
            raise InstallBackendError(
                "install_resource_identity_ledger_invalid"
            ) from error
        self._identity_records = records
        return records

    def journal_records(self) -> tuple[JournalRecord, ...]:
        return self._journal.journal_records()

    def append_journal(self, record: JournalRecord) -> None:
        self._journal.append_journal(record)

    def _request(
        self, step: PlanStep, *, compensation: bool
    ) -> HostOperationRequest:
        action = step.rollback if compensation else step.effect
        try:
            profile = HostOperationProfile(action)
        except ValueError as error:
            raise InstallBackendError("install_operation_profile_invalid") from error
        expected = expected_rollback_resource_names(
            self._binding.package_manifest_sha256
        )
        targets = tuple(
            HostResourceTarget(
                *expected[key],
                resource_labels_sha256=(
                    self._expected_resource_labels_sha256.get(expected[key])
                ),
            )
            for key in self._STEP_RESOURCE_KEYS.get(step.step_id, ())
        )
        return HostOperationRequest(
            profile=profile,
            step_id=step.step_id,
            execution_id=self._binding.execution_id,
            attempt_id=self._binding.attempt_id,
            execution_binding_sha256=self._binding.journal_binding_sha256,
            package_manifest_sha256=self._binding.package_manifest_sha256,
            resolved_store_spec_sha256=self._binding.resolved_store_spec_sha256,
            controller_runtime_receipt_sha256=(
                self._binding.controller_runtime_receipt_sha256
            ),
            controller_runtime_root=self._binding.controller_runtime_root,
            controller_runtime_tree_sha256=(
                self._binding.controller_runtime_tree_sha256
            ),
            controller_release_root=self._binding.controller_release_root,
            controller_release_tree_sha256=(
                self._binding.controller_release_tree_sha256
            ),
            controller_release_package_manifest_path=(
                self._binding.controller_release_package_manifest_path
            ),
            controller_runtime_interpreter_path=(
                self._binding.controller_runtime_interpreter_path
            ),
            controller_runtime_interpreter_sha256=(
                self._binding.controller_runtime_interpreter_sha256
            ),
            controller_runtime_inventory_path=(
                self._binding.controller_runtime_inventory_path
            ),
            controller_runtime_inventory_sha256=(
                self._binding.controller_runtime_inventory_sha256
            ),
            controller_requirements_lock_sha256=(
                self._binding.controller_requirements_lock_sha256
            ),
            supervisor_launcher_path=self._binding.supervisor_launcher_path,
            supervisor_launcher_sha256=(
                self._binding.supervisor_launcher_sha256
            ),
            resource_targets=targets,
        )

    @staticmethod
    def _expected_ownership(
        request: HostOperationRequest, revision_sha256: str
    ) -> str:
        document = {
            "schema_version": "governed-memory-host-observation-ownership-v1",
            "profile": request.profile.value,
            "step_id": request.step_id,
            "execution_id": request.execution_id,
            "attempt_id": request.attempt_id,
            "execution_binding_sha256": request.execution_binding_sha256,
            "resolved_store_spec_sha256": request.resolved_store_spec_sha256,
            "controller_runtime_receipt_sha256": (
                request.controller_runtime_receipt_sha256
            ),
            "controller_runtime_tree_sha256": (
                request.controller_runtime_tree_sha256
            ),
            "controller_release_tree_sha256": (
                request.controller_release_tree_sha256
            ),
            "supervisor_launcher_sha256": request.supervisor_launcher_sha256,
            "revision_sha256": revision_sha256,
        }
        return hashlib.sha256(
            json.dumps(
                document,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")
        ).hexdigest()

    @staticmethod
    def expected_resource_ownership(
        request: HostOperationRequest,
        *,
        resource_kind: str,
        resource_name: str,
        resource_id: str,
        resource_labels_sha256: str | None,
        image_id: str | None,
        image_repo_digest: str | None,
    ) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "schema_version": "governed-memory-install-resource-ownership-v1",
                    "profile": request.profile.value,
                    "step_id": request.step_id,
                    "execution_id": request.execution_id,
                    "attempt_id": request.attempt_id,
                    "execution_binding_sha256": request.execution_binding_sha256,
                    "package_manifest_sha256": request.package_manifest_sha256,
                    "resolved_store_spec_sha256": (
                        request.resolved_store_spec_sha256
                    ),
                    "controller_runtime_receipt_sha256": (
                        request.controller_runtime_receipt_sha256
                    ),
                    "controller_runtime_tree_sha256": (
                        request.controller_runtime_tree_sha256
                    ),
                    "controller_release_tree_sha256": (
                        request.controller_release_tree_sha256
                    ),
                    "supervisor_launcher_sha256": (
                        request.supervisor_launcher_sha256
                    ),
                    "resource_kind": resource_kind,
                    "resource_name": resource_name,
                    "resource_id": resource_id,
                    "resource_labels_sha256": resource_labels_sha256,
                    "image_id": image_id,
                    "image_repo_digest": image_repo_digest,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")
        ).hexdigest()

    def _validate_result(
        self,
        step: PlanStep,
        request: HostOperationRequest,
        result: object,
    ) -> HostApplyResult:
        if type(result) is not HostApplyResult:
            raise InstallBackendError("install_host_identity_receipt_required")
        expected_targets = tuple(
            (
                target.resource_kind,
                target.resource_name,
                target.resource_labels_sha256,
            )
            for target in request.resource_targets
        )
        observed_targets = tuple(
            (
                identity.resource_kind,
                identity.resource_name,
                identity.resource_labels_sha256,
            )
            for identity in result.identities
        )
        if observed_targets != expected_targets:
            raise InstallBackendError("install_host_identity_target_mismatch")
        if (
            step.step_id == "I19_COLD_RESTART_AND_VERIFY_TERMINAL_POSTFLIGHT"
        ) != (
            result.postflight_receipt_sha256 is not None
        ):
            raise InstallBackendError("install_postflight_receipt_invalid")
        for identity in result.identities:
            expected_labels_sha256 = self._expected_resource_labels_sha256.get(
                (identity.resource_kind, identity.resource_name)
            )
            if identity.resource_labels_sha256 != expected_labels_sha256:
                raise InstallBackendError("install_resource_labels_invalid")
            if identity.ownership_sha256 != self.expected_resource_ownership(
                request,
                resource_kind=identity.resource_kind,
                resource_name=identity.resource_name,
                resource_id=identity.resource_id,
                resource_labels_sha256=identity.resource_labels_sha256,
                image_id=identity.image_id,
                image_repo_digest=identity.image_repo_digest,
            ):
                raise InstallBackendError("install_resource_ownership_invalid")
            if identity.resource_kind == "container":
                expected_image = (
                    self._prerequisites.local_images.postgres
                    if "postgres" in identity.resource_name
                    else self._prerequisites.local_images.qdrant
                )
                if (
                    identity.image_id != expected_image.image_id
                    or identity.image_repo_digest != expected_image.repo_digest
                ):
                    raise InstallBackendError("install_container_image_identity_invalid")
        return result

    def _record_result(
        self,
        result: HostApplyResult,
        *,
        event: str,
    ) -> None:
        latest = latest_exact_resources(self.resource_identity_records())
        for identity in result.identities:
            key = (identity.resource_kind, identity.resource_name)
            current = latest.get(key)
            if current is not None:
                exact = (
                    current.resource_id == identity.resource_id
                    and current.image_id == identity.image_id
                    and current.image_repo_digest == identity.image_repo_digest
                    and current.ownership_sha256 == identity.ownership_sha256
                    and current.resource_labels_sha256
                    == identity.resource_labels_sha256
                )
                if not exact:
                    raise InstallBackendError("install_resource_identity_drift")
                if current.event == event:
                    continue
                if current.event == "removed":
                    raise InstallBackendError("install_resource_identity_removed")
            try:
                self._identity_ledger.append(
                    event=event,
                    resource_kind=identity.resource_kind,
                    resource_name=identity.resource_name,
                    resource_id=identity.resource_id,
                    image_id=identity.image_id,
                    image_repo_digest=identity.image_repo_digest,
                    ownership_sha256=identity.ownership_sha256,
                    resource_labels_sha256=identity.resource_labels_sha256,
                )
            except ResourceIdentityError as error:
                raise InstallBackendError(
                    "install_resource_identity_append_failed"
                ) from error
            latest[key] = self.resource_identity_records()[-1]

    def _result_from_observation(
        self, step: PlanStep, observed: HostObservation
    ) -> HostApplyResult:
        return HostApplyResult(
            observed.identities,
            observed.postflight_receipt_sha256,
        )

    def _ensure_fresh_empty_readiness(self) -> None:
        if self._fresh_empty_readiness is not None:
            return
        try:
            readiness = (
                self._prerequisites.readiness_probe.verify_fresh_empty_stores()
            )
        except Exception as error:
            raise InstallBackendError("install_store_readiness_failed") from error
        if type(readiness) is not EmptyStoreReadiness:
            raise InstallBackendError("install_store_readiness_invalid")
        self._fresh_empty_readiness = readiness

    def verify_terminal_canonical_store_readiness(
        self,
    ) -> TerminalCanonicalStoreReadiness:
        """Run a fresh terminal probe; this method deliberately never caches."""

        try:
            readiness = (
                self._prerequisites.readiness_probe
                .verify_terminal_canonical_stores()
            )
        except Exception as error:
            raise InstallBackendError(
                "install_terminal_store_readiness_failed"
            ) from error
        if type(readiness) is not TerminalCanonicalStoreReadiness:
            raise InstallBackendError(
                "install_terminal_store_readiness_invalid"
            )
        self._terminal_readiness = readiness
        return readiness

    def _fresh_readiness_is_pending_for_step(self, step: PlanStep) -> bool:
        if step.step_id != "I10_START_AND_VERIFY_EMPTY_STORES":
            return False
        records = self.journal_records()
        has_intent = any(
            record.step_id == step.step_id and record.event == "intent"
            for record in records
        )
        has_applied = any(
            record.step_id == step.step_id and record.event == "applied"
            for record in records
        )
        return has_intent and not has_applied

    def _recover_compensation_identity_if_exact(
        self,
        step: PlanStep,
        request: HostOperationRequest,
    ) -> None:
        journal_records = self.journal_records()
        if (
            not journal_records
            or journal_records[-1].step_id != step.step_id
            or journal_records[-1].event != "compensation_intent"
        ):
            return
        expected_targets = tuple(
            (target.resource_kind, target.resource_name)
            for target in request.resource_targets
        )
        if not expected_targets:
            return
        latest = latest_exact_resources(self.resource_identity_records())
        records = tuple(latest.get(target) for target in expected_targets)
        if any(record is None for record in records):
            raise InstallBackendError(
                "install_compensation_recovery_identity_incomplete"
            )
        active = tuple(
            record for record in records if record is not None and record.event != "removed"
        )
        if not active:
            return
        self._record_result(
            HostApplyResult(
                tuple(
                    HostResourceIdentityReceipt(
                        resource_kind=record.resource_kind,
                        resource_name=record.resource_name,
                        resource_id=record.resource_id,
                        ownership_sha256=record.ownership_sha256,
                        resource_labels_sha256=record.resource_labels_sha256,
                        image_id=record.image_id,
                        image_repo_digest=record.image_repo_digest,
                    )
                    for record in active
                )
            ),
            event="removed",
        )

    def _observe(
        self, step: PlanStep, *, compensation: bool
    ) -> tuple[HostOperationRequest, HostObservation]:
        request = self._request(step, compensation=compensation)
        try:
            observed = self._host.observe(request)
        except Exception as error:
            raise InstallBackendError("install_host_observation_failed") from error
        if type(observed) is not HostObservation or observed.ownership_sha256 != (
            self._expected_ownership(request, observed.revision_sha256)
        ):
            raise InstallBackendError("install_host_observation_unowned")
        self._observations[(step.step_id, compensation)] = observed
        return request, observed

    def probe(self, step: PlanStep) -> StepState:
        request, observed = self._observe(step, compensation=False)
        try:
            state = StepState(observed.state)
        except ValueError as error:
            raise InstallBackendError("install_host_state_invalid") from error
        if state is StepState.AFTER:
            result = self._validate_result(
                step, request, self._result_from_observation(step, observed)
            )
            self._record_result(result, event="created")
            if self._fresh_readiness_is_pending_for_step(step):
                self._ensure_fresh_empty_readiness()
            if (
                step.step_id
                == "I19_COLD_RESTART_AND_VERIFY_TERMINAL_POSTFLIGHT"
            ):
                self._postflight_receipt_sha256 = result.postflight_receipt_sha256
        elif observed.identities or observed.postflight_receipt_sha256 is not None:
            raise InstallBackendError("install_host_identity_before_effect")
        elif state is StepState.BEFORE:
            self._recover_compensation_identity_if_exact(step, request)
        return state

    def apply(self, step: PlanStep) -> None:
        key = (step.step_id, False)
        request, current = self._observe(step, compensation=False)
        if current.state not in {StepState.BEFORE.value, StepState.RECOVERABLE.value}:
            raise InstallBackendError("install_apply_state_invalid")
        expected = self._observations.pop(key)
        try:
            result = self._validate_result(
                step, request, self._host.apply(request, expected)
            )
            self._record_result(result, event="created")
        except (HostBoundaryError, InstallBackendError):
            raise
        except Exception as error:
            raise InstallBackendError("install_host_apply_failed") from error
        if step.step_id == "I10_START_AND_VERIFY_EMPTY_STORES":
            self._ensure_fresh_empty_readiness()
        if step.step_id == "I19_COLD_RESTART_AND_VERIFY_TERMINAL_POSTFLIGHT":
            self._postflight_receipt_sha256 = result.postflight_receipt_sha256

    def compensate(self, step: PlanStep) -> None:
        key = (step.step_id, True)
        request, current = self._observe(step, compensation=True)
        if current.state not in {StepState.AFTER.value, StepState.RECOVERABLE.value}:
            raise InstallBackendError("install_compensation_state_invalid")
        expected = self._observations.pop(key)
        try:
            observed = self._host.compensate(request, expected)
            if type(observed) is not HostApplyResult or observed.identities:
                raise InstallBackendError(
                    "install_compensation_result_invalid"
                )
            latest = latest_exact_resources(self.resource_identity_records())
            expected_targets = {
                (target.resource_kind, target.resource_name)
                for target in request.resource_targets
            }
            removals = HostApplyResult(
                tuple(
                    HostResourceIdentityReceipt(
                        resource_kind=record.resource_kind,
                        resource_name=record.resource_name,
                        resource_id=record.resource_id,
                        ownership_sha256=record.ownership_sha256,
                        resource_labels_sha256=record.resource_labels_sha256,
                        image_id=record.image_id,
                        image_repo_digest=record.image_repo_digest,
                    )
                    for identity, record in latest.items()
                    if identity in expected_targets and record.event != "removed"
                )
            )
            if { (i.resource_kind, i.resource_name) for i in removals.identities } != expected_targets:
                raise InstallBackendError(
                    "install_compensation_identity_incomplete"
                )
            self._record_result(removals, event="removed")
        except (HostBoundaryError, InstallBackendError):
            raise
        except Exception as error:
            raise InstallBackendError("install_host_compensation_failed") from error


__all__ = [
    "ClaimBoundInstallBackend",
    "InstallBackendError",
    "InstallPrerequisites",
]
