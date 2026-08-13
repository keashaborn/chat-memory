from __future__ import annotations

"""Exact physical empty-store rollback adapter.

The adapter deliberately has no subprocess, argv, URL, SQL, Qdrant request,
environment, or secret-value surface.  Separately reviewed Linux transports
implement the narrow typed operations below.  This layer binds those effects
to the verified install ledger, acquires a durable controller-authority marker,
rechecks semantic emptiness while both stores remain queryable, and holds that
marker through stop, physical removal, and receipt persistence.  The marker is not a database or Qdrant locking primitive: exclusion depends on the independently
verified install state (no application services, zero active clients, and
fresh root-controlled credentials).  Privileged direct host writers remain
outside this controller trust boundary.

Until the controller plan classifies the logical alias/collection/migration/
database steps as verification-only after physical erasure, this adapter fails
closed rather than journal a logical mutation that did not occur.
"""

from dataclasses import dataclass
import hashlib
import re
from typing import Final, Mapping, Protocol

from .durable_receipts import DurableReceiptStore, ReceiptArtifact
from .linux_plan import canonical_labels_sha256
from .receipts import canonical_json_bytes
from .resource_identity import (
    ResourceIdentityError,
    ResourceIdentityRecord,
    parse_ledger_bytes,
)
from .rollback import (
    RETAINED_AUDIT_KEYS,
    ROLLBACK_RESOURCE_KEYS,
    ExactRollbackResource,
    verified_rollback_resource_parts,
    verify_empty_rollback_eligibility_receipt,
)
from .rollback_entrypoint import (
    EmptyRollbackExecutionError,
    EmptyRollbackControllerAuthorityMarkerAcquisition,
    RollbackObservation,
    RollbackOperationRequest,
    canonical_live_rollback_controller_authority_marker_sha256,
    validate_empty_rollback_controller_authority_marker_capability,
    verify_retained_install_receipt_and_ledger,
)


class PhysicalRollbackAdapterError(RuntimeError):
    """Content-free refusal from exact physical rollback composition."""


_HASH_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_CONTAINER_ID_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}\Z", re.ASCII)
_SAFE_ID_RE = re.compile(
    r"/?[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,511}\Z", re.ASCII
)
_OBSERVATION_DOMAIN: Final = b"governed-memory-physical-observation-v1\x00"
_SEMANTIC_EMPTY_STATE_DOMAIN: Final = (
    b"governed-memory-semantic-empty-state-v1\x00"
)

PHYSICAL_RESOURCE_KEYS: Final = (
    "stores_supervisor",
    "qdrant_container",
    "postgres_container",
    "qdrant_volume",
    "postgres_volume",
    "network",
    "qdrant_store_secret",
    "postgres_store_secret",
    "resolved_store_spec",
)
LOGICAL_CHILD_KEYS: Final = (
    "qdrant_alias",
    "qdrant_collection",
    "migration_0004",
    "migration_0003",
    "migration_0001",
    "canonical_database_and_roles",
)
_REMOVE_OPERATIONS: Final = {
    "remove_exact_qdrant_container": "qdrant_container",
    "remove_exact_postgres_container": "postgres_container",
    "remove_exact_empty_qdrant_volume": "qdrant_volume",
    "remove_exact_empty_postgres_volume": "postgres_volume",
    "remove_exact_unused_network": "network",
    "remove_fresh_qdrant_store_secret": "qdrant_store_secret",
    "remove_fresh_postgres_store_secret": "postgres_store_secret",
    "remove_resolved_store_spec": "resolved_store_spec",
}


def _sha(value: object, *, domain: bytes = b"") -> str:
    return hashlib.sha256(domain + canonical_json_bytes(value)).hexdigest()


def _marker_bound_container_identity_sha256(
    target: LedgerBoundPhysicalTarget,
    observation: PhysicalResourceObservation,
) -> str:
    if (
        target.resource_kind != "container"
        or observation.resource_key != target.resource_key
        or observation.resource_kind != target.resource_kind
        or observation.resource_name != target.resource_name
        or observation.state not in {"running", "stopped", "absent"}
    ):
        raise PhysicalRollbackAdapterError(
            "physical_rollback_marker_bound_identity_target_invalid"
        )
    return _sha(
        {
            "schema_version": (
                "governed-memory-marker-bound-container-identity-v1"
            ),
            "resource_key": target.resource_key,
            "resource_kind": target.resource_kind,
            "resource_name": target.resource_name,
            "resource_id": target.resource_id,
            "ownership_sha256": target.ownership_sha256,
            "resource_identity_sha256": target.resource_identity_sha256,
            "resource_labels_sha256": target.resource_labels_sha256,
            "image_id": target.image_id,
            "image_repo_digest": target.image_repo_digest,
        },
        domain=_OBSERVATION_DOMAIN,
    )


@dataclass(frozen=True, slots=True)
class LedgerBoundPhysicalTarget:
    resource_key: str
    resource_kind: str
    resource_name: str
    resource_id: str
    ownership_sha256: str
    resource_identity_sha256: str
    resource_labels_sha256: str | None
    image_id: str | None
    image_repo_digest: str | None

    def __post_init__(self) -> None:
        if (
            self.resource_key not in PHYSICAL_RESOURCE_KEYS
            or _SAFE_ID_RE.fullmatch(self.resource_name) is None
            or _SAFE_ID_RE.fullmatch(self.resource_id) is None
            or any(
                _HASH_RE.fullmatch(item) is None
                for item in (
                    self.ownership_sha256,
                    self.resource_identity_sha256,
                )
            )
            or (
                self.resource_kind in {"container", "network", "volume"}
                and (
                    self.resource_labels_sha256 is None
                    or _HASH_RE.fullmatch(self.resource_labels_sha256) is None
                )
            )
            or (
                self.resource_kind not in {"container", "network", "volume"}
                and self.resource_labels_sha256 is not None
            )
        ):
            raise PhysicalRollbackAdapterError(
                "physical_rollback_target_invalid"
            )
        if self.resource_kind == "container":
            if (
                _CONTAINER_ID_RE.fullmatch(self.resource_id) is None
                or self.image_id is None
                or _IMAGE_ID_RE.fullmatch(self.image_id) is None
                or self.image_repo_digest is None
                or re.fullmatch(
                    r"[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}",
                    self.image_repo_digest,
                )
                is None
            ):
                raise PhysicalRollbackAdapterError(
                    "physical_rollback_container_target_invalid"
                )
        elif self.image_id is not None or self.image_repo_digest is not None:
            raise PhysicalRollbackAdapterError(
                "physical_rollback_target_image_invalid"
            )


@dataclass(frozen=True, slots=True)
class PhysicalRollbackBindings:
    package_manifest_sha256: str
    resource_ledger_binding_sha256: str
    resource_ledger_head_sha256: str
    resource_ledger_sequence: int
    exact_targets_sha256: str
    targets: tuple[LedgerBoundPhysicalTarget, ...]

    def __post_init__(self) -> None:
        if (
            any(
                _HASH_RE.fullmatch(item) is None
                for item in (
                    self.package_manifest_sha256,
                    self.resource_ledger_binding_sha256,
                    self.resource_ledger_head_sha256,
                    self.exact_targets_sha256,
                )
            )
            or type(self.resource_ledger_sequence) is not int
            or self.resource_ledger_sequence < len(ROLLBACK_RESOURCE_KEYS)
            or type(self.targets) is not tuple
            or any(
                type(item) is not LedgerBoundPhysicalTarget
                for item in self.targets
            )
            or len(set(self.targets)) != len(self.targets)
            or tuple(item.resource_key for item in self.targets)
            != PHYSICAL_RESOURCE_KEYS
        ):
            raise PhysicalRollbackAdapterError(
                "physical_rollback_bindings_invalid"
            )

    def target(self, resource_key: str) -> LedgerBoundPhysicalTarget:
        for target in self.targets:
            if target.resource_key == resource_key:
                return target
        raise PhysicalRollbackAdapterError("physical_rollback_target_absent")


def _ledger_resource_identity_sha256(record: ResourceIdentityRecord) -> str:
    return _sha(
        {
            "schema_version": "governed-memory-rollback-resource-identity-v2",
            "binding_sha256": record.binding_sha256,
            "resource_kind": record.resource_kind,
            "resource_name": record.resource_name,
            "resource_id": record.resource_id,
            "image_id": record.image_id,
            "image_repo_digest": record.image_repo_digest,
            "ownership_sha256": record.ownership_sha256,
            "resource_labels_sha256": record.resource_labels_sha256,
        }
    )


def physical_rollback_bindings_from_verified_ledger(
    records: tuple[ResourceIdentityRecord, ...],
    verified_resources: object,
) -> PhysicalRollbackBindings:
    """Derive effects only from the exact ledger snapshot bound to install."""

    evidence, exact_resources = verified_rollback_resource_parts(
        verified_resources
    )
    if (
        type(records) is not tuple
        or not records
        or any(type(record) is not ResourceIdentityRecord for record in records)
    ):
        raise PhysicalRollbackAdapterError(
            "physical_rollback_ledger_binding_invalid"
        )
    try:
        parsed = parse_ledger_bytes(
            b"".join(
                canonical_json_bytes(record.as_dict()) + b"\n"
                for record in records
            ),
            expected_binding_sha256=records[0].binding_sha256,
        )
    except (ResourceIdentityError, AttributeError, TypeError) as error:
        raise PhysicalRollbackAdapterError(
            "physical_rollback_ledger_binding_invalid"
        ) from error
    if (
        parsed != records
        or records[0].binding_sha256
        != evidence.resource_ledger_binding_sha256
        or len(records) != evidence.resource_ledger_sequence
        or records[-1].entry_sha256 != evidence.resource_ledger_head_sha256
    ):
        raise PhysicalRollbackAdapterError(
            "physical_rollback_ledger_binding_invalid"
        )
    exact_by_key = {item.resource_key: item for item in exact_resources}
    latest = {
        (record.resource_kind, record.resource_name): record
        for record in records
    }
    targets: list[LedgerBoundPhysicalTarget] = []
    for resource_key in PHYSICAL_RESOURCE_KEYS:
        exact = exact_by_key.get(resource_key)
        if exact is None:
            raise PhysicalRollbackAdapterError(
                "physical_rollback_ledger_target_absent"
            )
        record = latest.get((exact.resource_kind, exact.resource_name))
        if (
            record is None
            or record.event == "removed"
            or record.ownership_sha256 != exact.ownership_sha256
            or _ledger_resource_identity_sha256(record)
            != exact.resource_identity_sha256
        ):
            raise PhysicalRollbackAdapterError(
                "physical_rollback_ledger_target_drift"
            )
        targets.append(
            LedgerBoundPhysicalTarget(
                resource_key=resource_key,
                resource_kind=record.resource_kind,
                resource_name=record.resource_name,
                resource_id=record.resource_id,
                ownership_sha256=record.ownership_sha256,
                resource_identity_sha256=exact.resource_identity_sha256,
                resource_labels_sha256=record.resource_labels_sha256,
                image_id=record.image_id,
                image_repo_digest=record.image_repo_digest,
            )
        )
    return PhysicalRollbackBindings(
        package_manifest_sha256=evidence.package_manifest_sha256,
        resource_ledger_binding_sha256=(
            evidence.resource_ledger_binding_sha256
        ),
        resource_ledger_head_sha256=evidence.resource_ledger_head_sha256,
        resource_ledger_sequence=evidence.resource_ledger_sequence,
        exact_targets_sha256=evidence.exact_targets_sha256,
        targets=tuple(targets),
    )


@dataclass(frozen=True, slots=True)
class PhysicalResourceObservation:
    resource_key: str
    resource_kind: str
    resource_name: str
    state: str
    resource_id: str | None
    labels: tuple[tuple[str, str], ...] = ()
    image_id: str | None = None
    image_repo_digest: str | None = None

    def __post_init__(self) -> None:
        if (
            self.resource_key not in PHYSICAL_RESOURCE_KEYS
            or _SAFE_ID_RE.fullmatch(self.resource_name) is None
            or self.state
            not in {"present", "running", "stopped", "recoverable", "absent"}
            or type(self.labels) is not tuple
            or tuple(sorted(self.labels)) != self.labels
            or len(dict(self.labels)) != len(self.labels)
            or any(
                type(key) is not str
                or type(value) is not str
                or not key
                or any(character in key + value for character in "\x00\r\n")
                for key, value in self.labels
            )
        ):
            raise PhysicalRollbackAdapterError(
                "physical_rollback_observation_invalid"
            )
        if self.state == "absent":
            if (
                self.resource_id is not None
                or self.labels
                or self.image_id is not None
                or self.image_repo_digest is not None
            ):
                raise PhysicalRollbackAdapterError(
                    "physical_rollback_absence_observation_invalid"
                )
        elif self.resource_id is None or _SAFE_ID_RE.fullmatch(
            self.resource_id
        ) is None:
            raise PhysicalRollbackAdapterError(
                "physical_rollback_presence_observation_invalid"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "image_id": self.image_id,
            "image_repo_digest": self.image_repo_digest,
            "labels": {key: value for key, value in self.labels},
            "resource_id": self.resource_id,
            "resource_key": self.resource_key,
            "resource_kind": self.resource_kind,
            "resource_name": self.resource_name,
            "state": self.state,
        }


class PhysicalRollbackDriver(Protocol):
    """Narrow OS boundary; implementations accept no argv or endpoints."""

    def observe(
        self, target: LedgerBoundPhysicalTarget
    ) -> PhysicalResourceObservation: ...

    def disable_and_remove_stores_supervisor(
        self,
        target: LedgerBoundPhysicalTarget,
        expected_revision_sha256: str,
    ) -> None: ...

    def stop_container(
        self,
        target: LedgerBoundPhysicalTarget,
        expected_revision_sha256: str,
    ) -> None: ...

    def remove_container(
        self,
        target: LedgerBoundPhysicalTarget,
        expected_revision_sha256: str,
    ) -> None: ...

    def remove_volume(
        self,
        target: LedgerBoundPhysicalTarget,
        expected_revision_sha256: str,
    ) -> None: ...

    def remove_network(
        self,
        target: LedgerBoundPhysicalTarget,
        expected_revision_sha256: str,
    ) -> None: ...

    def remove_regular_file(
        self,
        target: LedgerBoundPhysicalTarget,
        expected_revision_sha256: str,
    ) -> None: ...

class EmptyEligibilityProbe(Protocol):
    def observe(
        self, request: RollbackOperationRequest
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class DurableLiveRollbackMarkerObservation:
    controller_authority_marker_sha256: str
    marker_bound_postgres_identity_sha256: str
    marker_bound_qdrant_identity_sha256: str
    semantic_empty_state_sha256: str | None
    durable_root_file_regular_no_follow: bool
    durable_root_file_mode: int
    durable_root_file_uid: int
    durable_root_file_gid: int
    durable_root_file_fsynced: bool
    durable_parent_fsynced: bool

    def __post_init__(self) -> None:
        if (
            _HASH_RE.fullmatch(self.controller_authority_marker_sha256) is None
            or _HASH_RE.fullmatch(
                self.marker_bound_postgres_identity_sha256
            ) is None
            or _HASH_RE.fullmatch(
                self.marker_bound_qdrant_identity_sha256
            ) is None
            or (
                self.semantic_empty_state_sha256 is not None
                and _HASH_RE.fullmatch(
                    self.semantic_empty_state_sha256
                ) is None
            )
            or self.durable_root_file_regular_no_follow is not True
            or self.durable_root_file_mode != 0o400
            or self.durable_root_file_uid != 0
            or self.durable_root_file_gid != 0
            or self.durable_root_file_fsynced is not True
            or self.durable_parent_fsynced is not True
        ):
            raise PhysicalRollbackAdapterError(
                "physical_rollback_live_rollback_controller_authority_marker_observation_invalid"
            )


class DurableLiveRollbackMarkerTransport(Protocol):
    """Durable controller-authority marker over ledger-bound live stores."""

    def acquire_or_recover(
        self,
        request: RollbackOperationRequest,
        *,
        marker_bound_postgres_identity_sha256: str,
        marker_bound_qdrant_identity_sha256: str,
        expected_semantic_empty_state_sha256: str,
    ) -> DurableLiveRollbackMarkerObservation: ...

    def persist_semantic_empty_observation(
        self,
        request: RollbackOperationRequest,
        held: DurableLiveRollbackMarkerObservation,
        semantic_empty_state_sha256: str,
    ) -> DurableLiveRollbackMarkerObservation: ...

    def assert_held(
        self,
        request: RollbackOperationRequest,
        held: DurableLiveRollbackMarkerObservation,
    ) -> DurableLiveRollbackMarkerObservation: ...

    def release_after_receipt(
        self,
        request: RollbackOperationRequest,
        held: DurableLiveRollbackMarkerObservation,
    ) -> None: ...


class RetainedAuditSource(Protocol):
    def authorization_nonce_sha256(
        self, request: RollbackOperationRequest
    ) -> str: ...

    def installation_journal_sha256(
        self, request: RollbackOperationRequest
    ) -> str: ...

    def rollback_journal_sha256(
        self, request: RollbackOperationRequest
    ) -> str: ...

    def authority_anchor_sha256(
        self, request: RollbackOperationRequest
    ) -> str: ...

    def resource_identity_ledger_sha256(
        self, request: RollbackOperationRequest
    ) -> str: ...

    def terminal_postflight_receipt_sha256(
        self, request: RollbackOperationRequest
    ) -> str: ...


@dataclass(slots=True)
class _HeldPhysicalControllerAuthorityMarker:
    observation: DurableLiveRollbackMarkerObservation
    eligibility: dict[str, object]
    marker_bound_observations: tuple[
        PhysicalResourceObservation, PhysicalResourceObservation
    ]


def _equivalent_empty_state(
    signed: Mapping[str, object], observed: Mapping[str, object]
) -> bool:
    left = verify_empty_rollback_eligibility_receipt(signed)
    right = verify_empty_rollback_eligibility_receipt(observed)
    ignored = {"observation_set_sha256", "receipt_sha256"}
    return {
        key: value for key, value in left.items() if key not in ignored
    } == {
        key: value for key, value in right.items() if key not in ignored
    }


def _semantic_empty_state_sha256(
    receipt: Mapping[str, object],
) -> str:
    verified = verify_empty_rollback_eligibility_receipt(receipt)
    return _sha(
        {
            key: verified[key]
            for key in sorted(verified)
            if key not in {"observation_set_sha256", "receipt_sha256"}
        },
        domain=_SEMANTIC_EMPTY_STATE_DOMAIN,
    )


class ExactPhysicalEmptyRollbackOperations:
    """Concrete ``EmptyRollbackOperations`` over a narrow trusted driver."""

    def __init__(
        self,
        *,
        bindings: PhysicalRollbackBindings,
        verified_resources: object,
        driver: PhysicalRollbackDriver,
        eligibility_probe: EmptyEligibilityProbe,
        controller_authority_marker_transport: DurableLiveRollbackMarkerTransport,
        retained_audit_source: RetainedAuditSource,
        receipt_store: DurableReceiptStore,
    ) -> None:
        evidence, resources = verified_rollback_resource_parts(
            verified_resources
        )
        if (
            type(bindings) is not PhysicalRollbackBindings
            or bindings.package_manifest_sha256
            != evidence.package_manifest_sha256
            or bindings.resource_ledger_binding_sha256
            != evidence.resource_ledger_binding_sha256
            or bindings.resource_ledger_head_sha256
            != evidence.resource_ledger_head_sha256
            or bindings.resource_ledger_sequence
            != evidence.resource_ledger_sequence
            or bindings.exact_targets_sha256 != evidence.exact_targets_sha256
            or tuple(item.resource_key for item in resources)
            != ROLLBACK_RESOURCE_KEYS
            or not all(
                callable(getattr(driver, name, None))
                for name in (
                    "observe",
                    "disable_and_remove_stores_supervisor",
                    "stop_container",
                    "remove_container",
                    "remove_volume",
                    "remove_network",
                    "remove_regular_file",
                )
            )
            or not callable(getattr(eligibility_probe, "observe", None))
            or not all(
                callable(getattr(controller_authority_marker_transport, name, None))
                for name in (
                    "acquire_or_recover",
                    "persist_semantic_empty_observation",
                    "assert_held",
                    "release_after_receipt",
                )
            )
            or not all(
                callable(getattr(retained_audit_source, name, None))
                for name in (
                    "authorization_nonce_sha256",
                    "installation_journal_sha256",
                    "rollback_journal_sha256",
                    "authority_anchor_sha256",
                    "resource_identity_ledger_sha256",
                    "terminal_postflight_receipt_sha256",
                )
            )
            or type(receipt_store) is not DurableReceiptStore
        ):
            raise PhysicalRollbackAdapterError(
                "physical_rollback_adapter_binding_invalid"
            )
        exact_by_key = {item.resource_key: item for item in resources}
        for target in bindings.targets:
            exact = exact_by_key[target.resource_key]
            calculated_identity = _sha(
                {
                    "schema_version": (
                        "governed-memory-rollback-resource-identity-v2"
                    ),
                    "binding_sha256": (
                        bindings.resource_ledger_binding_sha256
                    ),
                    "resource_kind": target.resource_kind,
                    "resource_name": target.resource_name,
                    "resource_id": target.resource_id,
                    "image_id": target.image_id,
                    "image_repo_digest": target.image_repo_digest,
                    "ownership_sha256": target.ownership_sha256,
                    "resource_labels_sha256": (
                        target.resource_labels_sha256
                    ),
                }
            )
            if (
                target.resource_kind != exact.resource_kind
                or target.resource_name != exact.resource_name
                or target.ownership_sha256 != exact.ownership_sha256
                or target.resource_identity_sha256
                != exact.resource_identity_sha256
                or calculated_identity != exact.resource_identity_sha256
            ):
                raise PhysicalRollbackAdapterError(
                    "physical_rollback_adapter_target_binding_invalid"
                )
        self._bindings = bindings
        self._verified_resources = verified_resources
        self._resources = resources
        self._driver = driver
        self._eligibility_probe = eligibility_probe
        self._controller_authority_marker_transport = controller_authority_marker_transport
        self._audit = retained_audit_source
        self._receipts = receipt_store
        self._controller_authority_marker: _HeldPhysicalControllerAuthorityMarker | None = None
        self._installation_execution_id: str | None = None

    @staticmethod
    def _revision(observation: PhysicalResourceObservation) -> str:
        return _sha(observation.as_dict(), domain=_OBSERVATION_DOMAIN)

    def _observe_exact(
        self,
        target: LedgerBoundPhysicalTarget,
    ) -> PhysicalResourceObservation:
        observed = self._driver.observe(target)
        if (
            type(observed) is not PhysicalResourceObservation
            or observed.resource_key != target.resource_key
            or observed.resource_kind != target.resource_kind
            or observed.resource_name != target.resource_name
        ):
            raise PhysicalRollbackAdapterError(
                "physical_rollback_observation_target_mismatch"
            )
        if observed.state == "absent":
            return observed
        labels_sha256 = (
            canonical_labels_sha256(dict(observed.labels))
            if target.resource_labels_sha256 is not None
            else None
        )
        if (
            observed.resource_id != target.resource_id
            or (
                target.resource_labels_sha256 is None
                and bool(observed.labels)
            )
            or labels_sha256 != target.resource_labels_sha256
            or observed.image_id != target.image_id
            or observed.image_repo_digest != target.image_repo_digest
            or (
                target.resource_kind == "container"
                and observed.state not in {"running", "stopped"}
            )
            or (
                target.resource_kind != "container"
                and observed.state != "present"
                and not (
                    target.resource_key == "stores_supervisor"
                    and observed.state == "recoverable"
                )
            )
        ):
            raise PhysicalRollbackAdapterError(
                "physical_rollback_resource_identity_drift"
            )
        return observed

    @staticmethod
    def _request_ownership_sha256(
        request: RollbackOperationRequest,
        revision_sha256: str,
    ) -> str:
        return _sha(
            {
                "schema_version": "governed-memory-empty-rollback-ownership-v2",
                "step_id": request.step.step_id,
                "operation": request.step.operation,
                "resource": (
                    None
                    if request.resource is None
                    else request.resource.as_dict()
                ),
                "execution_id": request.execution_id,
                "attempt_id": request.attempt_id,
                "journal_binding_sha256": request.journal_binding_sha256,
                "plan_sha256": request.plan_sha256,
                "package_manifest_sha256": request.package_manifest_sha256,
                "controller_runtime_receipt_sha256": (
                    request.controller_runtime_receipt_sha256
                ),
                "controller_runtime_root": request.controller_runtime_root,
                "controller_runtime_tree_sha256": (
                    request.controller_runtime_tree_sha256
                ),
                "controller_release_root": request.controller_release_root,
                "controller_release_tree_sha256": (
                    request.controller_release_tree_sha256
                ),
                "controller_release_package_manifest_path": (
                    request.controller_release_package_manifest_path
                ),
                "controller_runtime_interpreter_path": (
                    request.controller_runtime_interpreter_path
                ),
                "controller_runtime_interpreter_sha256": (
                    request.controller_runtime_interpreter_sha256
                ),
                "controller_runtime_inventory_path": (
                    request.controller_runtime_inventory_path
                ),
                "controller_runtime_inventory_sha256": (
                    request.controller_runtime_inventory_sha256
                ),
                "controller_requirements_lock_sha256": (
                    request.controller_requirements_lock_sha256
                ),
                "supervisor_launcher_path": request.supervisor_launcher_path,
                "supervisor_launcher_sha256": (
                    request.supervisor_launcher_sha256
                ),
                "installation_execution_id": (
                    request.installation_execution_id
                ),
                "installation_receipt_sha256": (
                    request.installation_receipt_sha256
                ),
                "eligibility_receipt_sha256": (
                    request.eligibility_receipt_sha256
                ),
                "resource_ledger_binding_sha256": (
                    request.resource_ledger_binding_sha256
                ),
                "resource_ledger_head_sha256": (
                    request.resource_ledger_head_sha256
                ),
                "resource_ledger_sequence": request.resource_ledger_sequence,
                "exact_targets_sha256": request.exact_targets_sha256,
                "revision_sha256": revision_sha256,
            }
        )

    def _rollback_observation(
        self,
        request: RollbackOperationRequest,
        observations: tuple[PhysicalResourceObservation, ...],
        state: str,
    ) -> RollbackObservation:
        revision = _sha(
            [item.as_dict() for item in observations],
            domain=_OBSERVATION_DOMAIN,
        )
        return RollbackObservation(
            state=state,
            revision_sha256=revision,
            ownership_sha256=self._request_ownership_sha256(request, revision),
            controller_runtime_tree_sha256=(
                request.controller_runtime_tree_sha256
            ),
            controller_release_tree_sha256=(
                request.controller_release_tree_sha256
            ),
        )

    def _validate_request(self, request: RollbackOperationRequest) -> None:
        if (
            type(request) is not RollbackOperationRequest
            or request.package_manifest_sha256
            != self._bindings.package_manifest_sha256
            or request.resource_ledger_head_sha256
            != self._bindings.resource_ledger_head_sha256
            or request.resource_ledger_binding_sha256
            != self._bindings.resource_ledger_binding_sha256
            or request.resource_ledger_sequence
            != self._bindings.resource_ledger_sequence
            or request.exact_targets_sha256
            != self._bindings.exact_targets_sha256
            or (
                self._installation_execution_id is not None
                and request.installation_execution_id
                != self._installation_execution_id
            )
        ):
            raise PhysicalRollbackAdapterError(
                "physical_rollback_request_binding_invalid"
            )
        if request.resource is not None:
            matching = next(
                (
                    item
                    for item in self._resources
                    if item.resource_key == request.resource.resource_key
                ),
                None,
            )
            if request.resource != matching:
                raise PhysicalRollbackAdapterError(
                    "physical_rollback_request_resource_invalid"
                )

    def verify_install_receipt_and_ledger(
        self,
        request: RollbackOperationRequest,
    ) -> object:
        self._validate_request(request)
        evidence = self._receipts.read(
            ReceiptArtifact.INSTALL,
            request.installation_execution_id,
        )
        if evidence.receipt_sha256 != request.installation_receipt_sha256:
            raise PhysicalRollbackAdapterError(
                "physical_rollback_install_receipt_mismatch"
            )
        if (
            self._installation_execution_id is not None
            and self._installation_execution_id
            != request.installation_execution_id
        ):
            raise PhysicalRollbackAdapterError(
                "physical_rollback_installation_execution_drift"
            )
        self._installation_execution_id = request.installation_execution_id
        return verify_retained_install_receipt_and_ledger(
            canonical_json_bytes(dict(evidence.canonical_receipt)),
            self._verified_resources,
        )

    def observe_empty_eligibility(
        self,
        request: RollbackOperationRequest,
        controller_authority_marker_capability: object | None,
    ) -> Mapping[str, object]:
        self._validate_request(request)
        if controller_authority_marker_capability is None:
            raise PhysicalRollbackAdapterError(
                "physical_rollback_controller_authority_marker_missing_for_semantic_probe"
            )
        self._require_controller_authority_marker(request, controller_authority_marker_capability)
        if self._controller_authority_marker is None:
            raise PhysicalRollbackAdapterError(
                "physical_rollback_controller_authority_marker_not_held"
            )
        try:
            observed = self._eligibility_probe.observe(request)
        except Exception:
            raise PhysicalRollbackAdapterError(
                "physical_rollback_live_empty_observation_failed"
            ) from None
        try:
            verified = verify_empty_rollback_eligibility_receipt(observed)
        except Exception as error:
            raise PhysicalRollbackAdapterError(
                "physical_rollback_live_empty_observation_invalid"
            ) from error
        if not _equivalent_empty_state(self._controller_authority_marker.eligibility, verified):
            raise PhysicalRollbackAdapterError(
                "physical_rollback_live_empty_state_drift"
            )
        semantic_sha256 = _semantic_empty_state_sha256(verified)
        try:
            persisted = (
                self._controller_authority_marker_transport.persist_semantic_empty_observation(
                    request,
                    self._controller_authority_marker.observation,
                    semantic_sha256,
                )
            )
        except Exception:
            raise PhysicalRollbackAdapterError(
                "physical_rollback_live_empty_controller_authority_marker_persistence_failed"
            ) from None
        if (
            type(persisted) is not DurableLiveRollbackMarkerObservation
            or persisted.controller_authority_marker_sha256
            != self._controller_authority_marker.observation.controller_authority_marker_sha256
            or persisted.marker_bound_postgres_identity_sha256
            != self._controller_authority_marker.observation.marker_bound_postgres_identity_sha256
            or persisted.marker_bound_qdrant_identity_sha256
            != self._controller_authority_marker.observation.marker_bound_qdrant_identity_sha256
            or persisted.semantic_empty_state_sha256 != semantic_sha256
        ):
            raise PhysicalRollbackAdapterError(
                "physical_rollback_live_empty_controller_authority_marker_persistence_invalid"
            )
        self._controller_authority_marker.observation = persisted
        self._controller_authority_marker.eligibility = dict(verified)
        return dict(verified)

    def acquire_empty_rollback_controller_authority_marker(
        self,
        request: RollbackOperationRequest,
    ) -> EmptyRollbackControllerAuthorityMarkerAcquisition:
        self._validate_request(request)
        if self._controller_authority_marker is not None:
            raise PhysicalRollbackAdapterError(
                "physical_rollback_controller_authority_marker_already_held"
            )
        if request.step.step_id != "R04_ACQUIRE_ROLLBACK_CONTROLLER_AUTHORITY_MARKER":
            raise PhysicalRollbackAdapterError(
                "physical_rollback_controller_authority_marker_precondition_invalid"
            )
        exact_observations = tuple(
            self._observe_exact(self._bindings.target(key))
            for key in ("postgres_container", "qdrant_container")
        )
        states = {item.state for item in exact_observations}
        if not (states <= {"running", "stopped", "absent"}):
            raise PhysicalRollbackAdapterError(
                "physical_rollback_live_controller_authority_marker_store_state_invalid"
            )
        durable = self._receipts.read(
            ReceiptArtifact.EMPTY_ROLLBACK_ELIGIBILITY,
            request.installation_execution_id,
        )
        if durable.receipt_sha256 != request.eligibility_receipt_sha256:
            raise PhysicalRollbackAdapterError(
                "physical_rollback_eligibility_binding_mismatch"
            )
        eligibility = verify_empty_rollback_eligibility_receipt(
            durable.canonical_receipt
        )
        if (
            eligibility["installation_execution_id"]
            != request.installation_execution_id
        ):
            raise PhysicalRollbackAdapterError(
                "physical_rollback_eligibility_execution_mismatch"
            )
        observed_by_key = {
            item.resource_key: item for item in exact_observations
        }
        marker_bound_postgres_identity_sha256 = (
            _marker_bound_container_identity_sha256(
                self._bindings.target("postgres_container"),
                observed_by_key["postgres_container"],
            )
        )
        marker_bound_qdrant_identity_sha256 = (
            _marker_bound_container_identity_sha256(
                self._bindings.target("qdrant_container"),
                observed_by_key["qdrant_container"],
            )
        )
        expected_controller_authority_marker_sha256 = canonical_live_rollback_controller_authority_marker_sha256(
            request,
            marker_bound_postgres_identity_sha256=(
                marker_bound_postgres_identity_sha256
            ),
            marker_bound_qdrant_identity_sha256=(
                marker_bound_qdrant_identity_sha256
            ),
        )
        try:
            marker = self._controller_authority_marker_transport.acquire_or_recover(
                request,
                marker_bound_postgres_identity_sha256=(
                    marker_bound_postgres_identity_sha256
                ),
                marker_bound_qdrant_identity_sha256=(
                    marker_bound_qdrant_identity_sha256
                ),
                expected_semantic_empty_state_sha256=(
                    _semantic_empty_state_sha256(eligibility)
                ),
            )
        except Exception:
            raise PhysicalRollbackAdapterError(
                "physical_rollback_live_rollback_controller_authority_marker_acquisition_failed"
            ) from None
        if (
            type(marker) is not DurableLiveRollbackMarkerObservation
            or marker.controller_authority_marker_sha256 != expected_controller_authority_marker_sha256
            or marker.marker_bound_postgres_identity_sha256
            != marker_bound_postgres_identity_sha256
            or marker.marker_bound_qdrant_identity_sha256
            != marker_bound_qdrant_identity_sha256
        ):
            raise PhysicalRollbackAdapterError(
                "physical_rollback_live_rollback_controller_authority_marker_binding_invalid"
            )
        self._controller_authority_marker = _HeldPhysicalControllerAuthorityMarker(
            observation=marker,
            eligibility=dict(eligibility),
            marker_bound_observations=(
                exact_observations[0],
                exact_observations[1],
            ),
        )
        return EmptyRollbackControllerAuthorityMarkerAcquisition(
            execution_id=request.execution_id,
            attempt_id=request.attempt_id,
            plan_sha256=request.plan_sha256,
            exact_targets_sha256=request.exact_targets_sha256,
            controller_runtime_tree_sha256=(
                request.controller_runtime_tree_sha256
            ),
            controller_release_tree_sha256=(
                request.controller_release_tree_sha256
            ),
            marker_bound_postgres_identity_sha256=(
                marker_bound_postgres_identity_sha256
            ),
            marker_bound_qdrant_identity_sha256=(
                marker_bound_qdrant_identity_sha256
            ),
            controller_authority_marker_sha256=expected_controller_authority_marker_sha256,
        )

    def _require_controller_authority_marker(
        self,
        request: RollbackOperationRequest,
        controller_authority_marker_capability: object,
    ) -> None:
        if self._controller_authority_marker is None:
            raise PhysicalRollbackAdapterError(
                "physical_rollback_controller_authority_marker_not_held"
            )
        validate_empty_rollback_controller_authority_marker_capability(
            controller_authority_marker_capability,
            expected_execution_id=request.execution_id,
            expected_attempt_id=request.attempt_id,
            expected_plan_sha256=request.plan_sha256,
            expected_eligibility_receipt_sha256=(
                request.eligibility_receipt_sha256
            ),
            expected_exact_targets_sha256=request.exact_targets_sha256,
            expected_controller_runtime_tree_sha256=(
                request.controller_runtime_tree_sha256
            ),
            expected_controller_release_tree_sha256=(
                request.controller_release_tree_sha256
            ),
            expected_marker_bound_postgres_identity_sha256=(
                self._controller_authority_marker.observation
                .marker_bound_postgres_identity_sha256
            ),
            expected_marker_bound_qdrant_identity_sha256=(
                self._controller_authority_marker.observation
                .marker_bound_qdrant_identity_sha256
            ),
            expected_controller_authority_marker_sha256=self._controller_authority_marker.observation.controller_authority_marker_sha256,
        )
        try:
            observed = self._controller_authority_marker_transport.assert_held(
                request,
                self._controller_authority_marker.observation,
            )
        except Exception:
            raise PhysicalRollbackAdapterError(
                "physical_rollback_live_rollback_controller_authority_marker_not_held"
            ) from None
        if observed != self._controller_authority_marker.observation:
            raise PhysicalRollbackAdapterError(
                "physical_rollback_live_rollback_controller_authority_marker_drift"
            )

    def release_empty_rollback_controller_authority_marker(
        self,
        request: RollbackOperationRequest,
        controller_authority_marker_capability: object,
    ) -> None:
        self._validate_request(request)
        self._require_controller_authority_marker(request, controller_authority_marker_capability)
        for target in self._bindings.targets:
            if self._observe_exact(target).state != "absent":
                raise PhysicalRollbackAdapterError(
                    "physical_rollback_controller_authority_marker_release_before_exact_absence"
                )
        try:
            self._controller_authority_marker_transport.release_after_receipt(
                request,
                self._controller_authority_marker.observation,
            )
        except Exception:
            raise PhysicalRollbackAdapterError(
                "physical_rollback_live_rollback_controller_authority_marker_release_failed"
            ) from None
        self._controller_authority_marker = None

    def observe(
        self,
        request: RollbackOperationRequest,
    ) -> RollbackObservation:
        self._validate_request(request)
        operation = request.step.operation
        if request.resource is not None:
            key = request.resource.resource_key
        elif operation == "stop_exact_stores":
            key = ""
        else:
            raise PhysicalRollbackAdapterError(
                "physical_rollback_operation_not_physical"
            )
        if key in LOGICAL_CHILD_KEYS:
            volumes = tuple(
                self._observe_exact(self._bindings.target(item))
                for item in ("qdrant_volume", "postgres_volume")
            )
            state = "after" if all(item.state == "absent" for item in volumes) else "drift"
            return self._rollback_observation(request, volumes, state)
        if operation == "stop_exact_stores":
            containers = tuple(
                self._observe_exact(self._bindings.target(item))
                for item in ("qdrant_container", "postgres_container")
            )
            if all(item.state == "stopped" for item in containers):
                state = "after"
            elif all(item.state in {"running", "stopped"} for item in containers):
                state = "before"
            else:
                state = "drift"
            return self._rollback_observation(request, containers, state)
        target = self._bindings.target(key)
        observed = self._observe_exact(target)
        return self._rollback_observation(
            request,
            (observed,),
            (
                "after"
                if observed.state == "absent"
                else "recoverable"
                if observed.state == "recoverable"
                else "before"
            ),
        )

    def apply_if_still_empty(
        self,
        request: RollbackOperationRequest,
        expected: RollbackObservation,
        controller_authority_marker_capability: object,
        signed_eligibility: Mapping[str, object],
    ) -> None:
        self._validate_request(request)
        self._require_controller_authority_marker(request, controller_authority_marker_capability)
        eligibility = verify_empty_rollback_eligibility_receipt(
            signed_eligibility
        )
        if (
            eligibility["receipt_sha256"]
            != request.eligibility_receipt_sha256
            or self._controller_authority_marker is None
            or self._controller_authority_marker.observation.semantic_empty_state_sha256
            is None
            or not _equivalent_empty_state(
                eligibility, self._controller_authority_marker.eligibility
            )
        ):
            raise PhysicalRollbackAdapterError(
                "physical_rollback_empty_state_drift"
            )
        operation = request.step.operation
        if operation == "disable_and_remove_stores_supervisor":
            target = self._bindings.target("stores_supervisor")
            before = self._observe_exact(target)
            canonical = self._rollback_observation(
                request,
                (before,),
                "after" if before.state == "absent" else "before",
            )
            if expected.revision_sha256 != canonical.revision_sha256:
                raise PhysicalRollbackAdapterError(
                    "physical_rollback_expected_observation_drift"
                )
            if before.state == "absent" and expected.state == "after":
                return
            if expected.state not in {"before", "recoverable"}:
                raise PhysicalRollbackAdapterError(
                    "physical_rollback_expected_observation_drift"
                )
            self._driver.disable_and_remove_stores_supervisor(
                target,
                self._revision(before),
            )
            if self._observe_exact(target).state != "absent":
                raise PhysicalRollbackAdapterError(
                    "physical_rollback_supervisor_removal_unproved"
                )
            return
        if operation == "stop_exact_stores":
            containers = tuple(
                self._observe_exact(self._bindings.target(item))
                for item in ("qdrant_container", "postgres_container")
            )
            state = (
                "after"
                if all(item.state == "stopped" for item in containers)
                else "before"
            )
            canonical = self._rollback_observation(
                request,
                containers,
                state,
            )
            if expected.revision_sha256 != canonical.revision_sha256:
                raise PhysicalRollbackAdapterError(
                    "physical_rollback_store_stop_observation_drift"
                )
            if state == "after" and expected.state == "after":
                return
            if (
                expected.state not in {"before", "recoverable"}
                or any(
                    item.state not in {"running", "stopped"}
                    for item in containers
                )
            ):
                raise PhysicalRollbackAdapterError(
                    "physical_rollback_store_stop_observation_drift"
                )
            for key, before in zip(
                ("qdrant_container", "postgres_container"),
                containers,
            ):
                if before.state == "running":
                    self._driver.stop_container(
                        self._bindings.target(key),
                        self._revision(before),
                    )
            for key in ("qdrant_container", "postgres_container"):
                if self._observe_exact(
                    self._bindings.target(key)
                ).state != "stopped":
                    raise PhysicalRollbackAdapterError(
                        "physical_rollback_container_not_stopped"
                    )
            return
        if operation in {
            "verify_qdrant_alias_physically_absent",
            "verify_qdrant_collection_physically_absent",
            "verify_pilot_marker_0004_physically_absent",
            "verify_owner_claim_detail_0003_physically_absent",
            "verify_foundation_0001_physically_absent",
            "verify_canonical_database_and_roles_physically_absent",
        }:
            if not request.step.invariant_only:
                raise PhysicalRollbackAdapterError(
                    "physical_rollback_verification_step_not_invariant"
                )
            for volume_key in ("qdrant_volume", "postgres_volume"):
                if self._observe_exact(
                    self._bindings.target(volume_key)
                ).state != "absent":
                    raise PhysicalRollbackAdapterError(
                        "physical_rollback_logical_absence_unproved"
                    )
            return
        key = _REMOVE_OPERATIONS.get(operation)
        if key is None or request.resource is None or request.resource.resource_key != key:
            raise PhysicalRollbackAdapterError(
                "physical_rollback_effect_not_allowed"
            )
        target = self._bindings.target(key)
        before = self._observe_exact(target)
        if expected.state == "after" and before.state == "absent":
            # Exact crash recovery after the effect but before APPLIED became
            # durable.  Absence is independently reobserved; there is no
            # second delete and no name-based fallback.
            if (
                expected.revision_sha256
                != self._rollback_observation(
                    request, (before,), "after"
                ).revision_sha256
            ):
                raise PhysicalRollbackAdapterError(
                    "physical_rollback_recovery_observation_drift"
                )
            return
        canonical_before = self._rollback_observation(
            request, (before,), "before"
        )
        if (
            expected.state not in {"before", "recoverable"}
            or expected.revision_sha256 != canonical_before.revision_sha256
        ):
            raise PhysicalRollbackAdapterError(
                "physical_rollback_expected_observation_drift"
            )
        revision = self._revision(before)
        if target.resource_kind == "container":
            if before.state != "stopped":
                raise PhysicalRollbackAdapterError(
                    "physical_rollback_container_not_stopped"
                )
            self._driver.remove_container(target, revision)
        elif target.resource_kind == "volume":
            for container_key in ("qdrant_container", "postgres_container"):
                if self._observe_exact(
                    self._bindings.target(container_key)
                ).state != "absent":
                    raise PhysicalRollbackAdapterError(
                        "physical_rollback_volume_has_container"
                    )
            self._driver.remove_volume(target, revision)
        elif target.resource_kind == "network":
            for dependency_key in (
                "qdrant_container",
                "postgres_container",
                "qdrant_volume",
                "postgres_volume",
            ):
                if self._observe_exact(
                    self._bindings.target(dependency_key)
                ).state != "absent":
                    raise PhysicalRollbackAdapterError(
                        "physical_rollback_network_dependency_present"
                    )
            self._driver.remove_network(target, revision)
        elif target.resource_kind in {"secret_file", "resolved_store_spec"}:
            for volume_key in ("qdrant_volume", "postgres_volume"):
                if self._observe_exact(
                    self._bindings.target(volume_key)
                ).state != "absent":
                    raise PhysicalRollbackAdapterError(
                        "physical_rollback_file_before_store_erasure"
                    )
            self._driver.remove_regular_file(target, revision)
        else:
            raise PhysicalRollbackAdapterError(
                "physical_rollback_effect_kind_invalid"
            )
        if self._observe_exact(target).state != "absent":
            raise PhysicalRollbackAdapterError(
                "physical_rollback_removal_unproved"
            )

    def exact_resources_absent(
        self,
        request: RollbackOperationRequest,
        resources: tuple[ExactRollbackResource, ...],
        controller_authority_marker_capability: object,
    ) -> bool:
        self._validate_request(request)
        self._require_controller_authority_marker(request, controller_authority_marker_capability)
        if resources != self._resources:
            raise PhysicalRollbackAdapterError(
                "physical_rollback_exact_resource_set_mismatch"
            )
        physical_absent = all(
            self._observe_exact(target).state == "absent"
            for target in self._bindings.targets
        )
        logical_keys = {
            resource.resource_key
            for resource in resources
            if resource.resource_key in LOGICAL_CHILD_KEYS
        }
        return (
            physical_absent
            and logical_keys == set(LOGICAL_CHILD_KEYS)
            and len(resources) == len(ROLLBACK_RESOURCE_KEYS)
        )

    def observe_retained_audit_set(
        self,
        request: RollbackOperationRequest,
        retained_audit_keys: tuple[str, ...],
        controller_authority_marker_capability: object,
    ) -> Mapping[str, str]:
        self._validate_request(request)
        self._require_controller_authority_marker(request, controller_authority_marker_capability)
        if retained_audit_keys != RETAINED_AUDIT_KEYS:
            raise PhysicalRollbackAdapterError(
                "physical_rollback_retained_audit_keys_invalid"
            )
        install = self._receipts.read(
            ReceiptArtifact.INSTALL, request.installation_execution_id
        )
        eligibility = self._receipts.read(
            ReceiptArtifact.EMPTY_ROLLBACK_ELIGIBILITY,
            request.installation_execution_id,
        )
        if (
            install.receipt_sha256 != request.installation_receipt_sha256
            or eligibility.receipt_sha256
            != request.eligibility_receipt_sha256
        ):
            raise PhysicalRollbackAdapterError(
                "physical_rollback_retained_receipt_mismatch"
            )
        terminal_postflight_sha256 = (
            self._audit.terminal_postflight_receipt_sha256(request)
        )
        if (
            terminal_postflight_sha256
            != install.canonical_receipt.get("postflight_receipt_sha256")
        ):
            raise PhysicalRollbackAdapterError(
                "physical_rollback_terminal_postflight_receipt_mismatch"
            )
        observed = {
            "authorization_nonce": self._audit.authorization_nonce_sha256(
                request
            ),
            "installation_journal": (
                self._audit.installation_journal_sha256(request)
            ),
            "rollback_journal": self._audit.rollback_journal_sha256(request),
            "authority_anchor": self._audit.authority_anchor_sha256(request),
            "resource_identity_ledger": (
                self._audit.resource_identity_ledger_sha256(request)
            ),
            "terminal_postflight_receipt": (
                terminal_postflight_sha256
            ),
            "installation_receipt": install.canonical_file_sha256,
            "eligibility_receipt": eligibility.canonical_file_sha256,
        }
        if any(_HASH_RE.fullmatch(value) is None for value in observed.values()):
            raise PhysicalRollbackAdapterError(
                "physical_rollback_retained_audit_hash_invalid"
            )
        return observed


__all__ = [
    "DurableLiveRollbackMarkerObservation",
    "DurableLiveRollbackMarkerTransport",
    "EmptyEligibilityProbe",
    "ExactPhysicalEmptyRollbackOperations",
    "LOGICAL_CHILD_KEYS",
    "LedgerBoundPhysicalTarget",
    "PHYSICAL_RESOURCE_KEYS",
    "PhysicalResourceObservation",
    "PhysicalRollbackAdapterError",
    "PhysicalRollbackBindings",
    "PhysicalRollbackDriver",
    "RetainedAuditSource",
    "physical_rollback_bindings_from_verified_ledger",
]
