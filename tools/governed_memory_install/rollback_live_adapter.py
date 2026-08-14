from __future__ import annotations

"""Exact physical empty-store rollback adapter.

The adapter deliberately has no subprocess, argv, URL, SQL, Qdrant request,
environment, or secret-value surface.  Separately reviewed Linux transports
implement the narrow typed operations below.  This layer binds those effects
to the verified install ledger, acquires a durable controller-authority marker,
removes the exact store supervisor, then establishes an administrative writer
fence by reproving no application services, zero active clients, and semantic
emptiness while both stores remain queryable through fresh root-controlled
credentials.  The marker and fence are held through stop, physical removal,
and receipt persistence.  They exclude cooperating and unprivileged runtime
writers, not an equivalently privileged root administrator; host
administration remains the explicit trust boundary.

Until the controller plan classifies the logical alias/collection/migration/
database steps as verification-only after physical erasure, this adapter fails
closed rather than journal a logical mutation that did not occur.
"""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Final, Mapping, Protocol

from .authority_state import AuthorityState, nonce_sha256
from .durable_receipts import DurableReceiptStore, ReceiptArtifact
from .execution_lock import (
    HeldExecutionLockCapability,
    validate_held_execution_lock,
)
from .linux_plan import canonical_labels_sha256, validate_store_spec
from .linux_live_adapters import (
    ClosedRootFileEffects,
    ProductionLinuxStoreTransportFactory,
)
from .linux_store_readiness import (
    TerminalPostgreSQLSnapshot,
    TerminalQdrantSnapshot,
)
from .linux_store_effects import (
    BoundLinuxStoreTransports,
    BoundResourceSnapshot,
    EffectPresence,
    ExactInstallArtifacts,
    ExactSystemdSupervisor,
)
from .postgres_native_stages import NativeStageReceipt
from .journal import parse_durable_journal_bytes
from .receipts import (
    canonical_json_bytes,
    verify_install_receipt,
)
from .resource_identity import (
    ResourceIdentityError,
    ResourceIdentityRecord,
    load_ledger,
    resource_ledger_binding_sha256,
    parse_ledger_bytes,
)
from .rollback import (
    RETAINED_AUDIT_KEYS,
    ROLLBACK_RESOURCE_KEYS,
    ExactRollbackResource,
    build_empty_rollback_eligibility_receipt,
    verified_rollback_resource_parts,
    verify_empty_rollback_eligibility_receipt,
)
from .rollback_authority import rollback_capability_evidence
from .rollback_journal import DurableRollbackJournal
from .rollback_entrypoint import (
    EmptyRollbackExecutionError,
    EmptyRollbackControllerAuthorityMarkerAcquisition,
    RollbackObservation,
    RollbackOperationRequest,
    canonical_live_rollback_controller_authority_marker_sha256,
    claimed_empty_rollback_evidence,
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
_AUDIT_ANCHOR_DOMAIN: Final = b"governed-memory-retained-audit-anchor-v1\x00"
_INSTALL_JOURNAL_TEMPLATE: Final = (
    "/var/lib/governed-memory-controller/executions-v4/"
    "{execution_id}/journal.jsonl"
)
_RESOURCE_LEDGER_TEMPLATE: Final = (
    "/var/lib/governed-memory-controller/executions-v4/"
    "{execution_id}/resources.jsonl"
)
_APPLICATION_WRITER_PATHS: Final = (
    "/etc/systemd/system/governed-memory-http.service",
    "/etc/systemd/system/governed-memory-worker.service",
    "/etc/systemd/system/multi-user.target.wants/governed-memory-http.service",
    "/etc/systemd/system/multi-user.target.wants/governed-memory-worker.service",
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


class ClosedLinuxPhysicalRollbackDriver:
    """Exact physical rollback operations over a pre-bound live transport set.

    The caller cannot provide paths, names, argv, endpoints, credentials, or
    operation mappings.  The resolved store specification contributes only
    exact label evidence; all mutations dispatch through the closed typed
    adapters selected during claim-bound composition.
    """

    def __init__(
        self,
        *,
        transports: BoundLinuxStoreTransports,
        resolved_store_spec: Mapping[str, object],
        exact_supervisor: ExactSystemdSupervisor,
    ) -> None:
        spec = validate_store_spec(
            resolved_store_spec, allow_placeholders=False
        )
        if (
            type(transports) is not BoundLinuxStoreTransports
            or type(exact_supervisor) is not ExactSystemdSupervisor
        ):
            raise PhysicalRollbackAdapterError(
                "physical_rollback_linux_driver_binding_invalid"
            )
        resources = spec["resources"]
        self._labels = {
            "network": tuple(sorted(resources["network"]["labels"].items())),
            "postgres_volume": tuple(
                sorted(resources["volumes"]["postgres"]["labels"].items())
            ),
            "qdrant_volume": tuple(
                sorted(resources["volumes"]["qdrant"]["labels"].items())
            ),
            "postgres_container": tuple(
                sorted(resources["containers"]["postgres"]["labels"].items())
            ),
            "qdrant_container": tuple(
                sorted(resources["containers"]["qdrant"]["labels"].items())
            ),
        }
        self._transports = transports
        self._supervisor = exact_supervisor

    def _snapshot(self, key: str) -> BoundResourceSnapshot:
        t = self._transports
        readers = {
            "network": t.docker.observe_network,
            "postgres_volume": t.docker.observe_postgres_volume,
            "qdrant_volume": t.docker.observe_qdrant_volume,
            "postgres_container": t.docker.observe_postgres_container,
            "qdrant_container": t.docker.observe_qdrant_container,
            "postgres_store_secret": t.files.observe_postgres_secret,
            "qdrant_store_secret": t.files.observe_qdrant_secret,
            "resolved_store_spec": t.files.observe_resolved_store_spec,
        }
        if key == "stores_supervisor":
            return t.systemd.observe_supervisor().classify(self._supervisor)
        reader = readers.get(key)
        if reader is None:
            raise PhysicalRollbackAdapterError(
                "physical_rollback_linux_driver_target_invalid"
            )
        value = reader()
        if type(value) is not BoundResourceSnapshot:
            raise PhysicalRollbackAdapterError(
                "physical_rollback_linux_driver_observation_invalid"
            )
        return value

    def observe(
        self, target: LedgerBoundPhysicalTarget
    ) -> PhysicalResourceObservation:
        if type(target) is not LedgerBoundPhysicalTarget:
            raise PhysicalRollbackAdapterError(
                "physical_rollback_linux_driver_target_invalid"
            )
        snapshot = self._snapshot(target.resource_key)
        if snapshot.presence is EffectPresence.ABSENT:
            return PhysicalResourceObservation(
                target.resource_key,
                target.resource_kind,
                target.resource_name,
                "absent",
                None,
            )
        if snapshot.presence not in {
            EffectPresence.EXACT,
            EffectPresence.PARTIAL,
        }:
            raise PhysicalRollbackAdapterError(
                "physical_rollback_linux_driver_observation_drift"
            )
        state = "present"
        if target.resource_kind == "container":
            state = "running" if snapshot.running is True else "stopped"
        elif target.resource_key == "stores_supervisor" and (
            snapshot.presence is EffectPresence.PARTIAL
        ):
            state = "recoverable"
        return PhysicalResourceObservation(
            target.resource_key,
            target.resource_kind,
            target.resource_name,
            state,
            snapshot.resource_id,
            self._labels.get(target.resource_key, ()),
            snapshot.image_id,
            snapshot.image_repo_digest,
        )

    def _require_revision(
        self,
        target: LedgerBoundPhysicalTarget,
        expected_revision_sha256: str,
    ) -> PhysicalResourceObservation:
        observed = self.observe(target)
        if (
            _HASH_RE.fullmatch(expected_revision_sha256) is None
            or _sha(observed.as_dict(), domain=_OBSERVATION_DOMAIN)
            != expected_revision_sha256
        ):
            raise PhysicalRollbackAdapterError(
                "physical_rollback_linux_driver_revision_drift"
            )
        return observed

    def disable_and_remove_stores_supervisor(
        self,
        target: LedgerBoundPhysicalTarget,
        expected_revision_sha256: str,
    ) -> None:
        self._require_revision(target, expected_revision_sha256)
        self._transports.systemd.disable_and_remove_supervisor(
            self._supervisor
        )

    def stop_container(
        self,
        target: LedgerBoundPhysicalTarget,
        expected_revision_sha256: str,
    ) -> None:
        self._require_revision(target, expected_revision_sha256)
        if target.resource_key == "postgres_container":
            self._transports.docker.stop_postgres_container()
        elif target.resource_key == "qdrant_container":
            self._transports.docker.stop_qdrant_container()
        else:
            raise PhysicalRollbackAdapterError(
                "physical_rollback_linux_driver_target_invalid"
            )

    def remove_container(
        self,
        target: LedgerBoundPhysicalTarget,
        expected_revision_sha256: str,
    ) -> None:
        self._require_revision(target, expected_revision_sha256)
        if target.resource_key == "postgres_container":
            self._transports.docker.remove_postgres_container()
        elif target.resource_key == "qdrant_container":
            self._transports.docker.remove_qdrant_container()
        else:
            raise PhysicalRollbackAdapterError(
                "physical_rollback_linux_driver_target_invalid"
            )

    def remove_volume(
        self,
        target: LedgerBoundPhysicalTarget,
        expected_revision_sha256: str,
    ) -> None:
        self._require_revision(target, expected_revision_sha256)
        if target.resource_key == "postgres_volume":
            self._transports.docker.remove_postgres_volume()
        elif target.resource_key == "qdrant_volume":
            self._transports.docker.remove_qdrant_volume()
        else:
            raise PhysicalRollbackAdapterError(
                "physical_rollback_linux_driver_target_invalid"
            )

    def remove_network(
        self,
        target: LedgerBoundPhysicalTarget,
        expected_revision_sha256: str,
    ) -> None:
        self._require_revision(target, expected_revision_sha256)
        if target.resource_key != "network":
            raise PhysicalRollbackAdapterError(
                "physical_rollback_linux_driver_target_invalid"
            )
        self._transports.docker.remove_network()

    def remove_regular_file(
        self,
        target: LedgerBoundPhysicalTarget,
        expected_revision_sha256: str,
    ) -> None:
        self._require_revision(target, expected_revision_sha256)
        removers = {
            "postgres_store_secret": (
                self._transports.files.remove_postgres_secret
            ),
            "qdrant_store_secret": self._transports.files.remove_qdrant_secret,
            "resolved_store_spec": (
                self._transports.files.remove_resolved_store_spec
            ),
        }
        remover = removers.get(target.resource_key)
        if remover is None:
            raise PhysicalRollbackAdapterError(
                "physical_rollback_linux_driver_target_invalid"
            )
        remover()


class ProductionLinuxEmptyRollbackOperationsFactory:
    """Build the production rollback operations without injected effects.

    The public rollback entrypoint constructs this exact class after claiming
    authority.  The factory selects the filesystem, subprocess, fixed HTTP,
    PostgreSQL, marker, eligibility, audit, and physical deletion adapters.
    Its caller can supply only opaque capabilities and verified immutable
    evidence, never an operation implementation.
    """

    def __init__(
        self,
        *,
        held_lock: HeldExecutionLockCapability,
        verified_controller_runtime_capability: object,
        authority_state: AuthorityState,
        receipt_store: DurableReceiptStore,
    ) -> None:
        try:
            validate_held_execution_lock(held_lock)
            receipt_store.require_production_binding()
        except Exception:
            raise PhysicalRollbackAdapterError(
                "production_linux_rollback_factory_dependency_invalid"
            ) from None
        if (
            type(authority_state) is not AuthorityState
            or verified_controller_runtime_capability is None
        ):
            raise PhysicalRollbackAdapterError(
                "production_linux_rollback_factory_dependency_invalid"
            )
        self._held_lock = held_lock
        self._verified_controller_runtime_capability = (
            verified_controller_runtime_capability
        )
        self._authority_state = authority_state
        self._receipt_store = receipt_store

    def __call__(
        self,
        *,
        claimed_rollback: object,
        verified_rollback_capability: object,
        signed_eligibility: Mapping[str, object],
        resolved_store_spec: Mapping[str, object],
        verified_artifacts: Mapping[str, bytes],
        verified_resources: object,
        installation_receipt: Mapping[str, object],
        rollback_journal: DurableRollbackJournal,
    ) -> ExactPhysicalEmptyRollbackOperations:
        try:
            validate_held_execution_lock(self._held_lock)
            claim = claimed_empty_rollback_evidence(claimed_rollback)
            rollback_authority = rollback_capability_evidence(
                verified_rollback_capability
            )
            eligibility = verify_empty_rollback_eligibility_receipt(
                signed_eligibility
            )
            install = verify_install_receipt(installation_receipt)
            spec = validate_store_spec(
                resolved_store_spec,
                allow_placeholders=False,
            )
            artifacts = ExactInstallArtifacts.from_verified_mapping(
                verified_artifacts
            )
        except Exception:
            raise PhysicalRollbackAdapterError(
                "production_linux_rollback_factory_binding_invalid"
            ) from None
        execution_binding = spec["execution_binding"]
        installation_execution_id = str(install["execution_id"])
        installation_binding_sha256 = execution_binding.get(
            "binding_sha256"
        )
        if _HASH_RE.fullmatch(str(installation_binding_sha256)) is None:
            raise PhysicalRollbackAdapterError(
                "production_linux_rollback_factory_binding_invalid"
            )
        try:
            expected_ledger_binding = resource_ledger_binding_sha256(
                str(installation_binding_sha256)
            )
        except Exception:
            raise PhysicalRollbackAdapterError(
                "production_linux_rollback_factory_binding_invalid"
            ) from None
        resource_evidence, unused_resources = verified_rollback_resource_parts(
            verified_resources
        )
        if (
            type(rollback_journal) is not DurableRollbackJournal
            or rollback_journal.execution_id != claim.execution_id
            or rollback_journal.binding_sha256 != claim.journal_binding_sha256
            or rollback_authority.authorization_sha256
            != claim.authorization_sha256
            or rollback_authority.installation_execution_id
            != installation_execution_id
            or eligibility["installation_execution_id"]
            != installation_execution_id
            or eligibility["installation_receipt_sha256"]
            != install["receipt_sha256"]
            or install["package_manifest_sha256"]
            != claim.package_manifest_sha256
            or install["controller_runtime_receipt_sha256"]
            != claim.controller_runtime_receipt_sha256
            or execution_binding.get("execution_id")
            != installation_execution_id
            or execution_binding.get("package_manifest_sha256")
            != claim.package_manifest_sha256
            or expected_ledger_binding
            != resource_evidence.resource_ledger_binding_sha256
        ):
            raise PhysicalRollbackAdapterError(
                "production_linux_rollback_factory_binding_invalid"
            )
        ledger_path = Path(
            _RESOURCE_LEDGER_TEMPLATE.replace(
                "{execution_id}", installation_execution_id
            )
        )
        try:
            records = load_ledger(
                ledger_path,
                expected_binding_sha256=expected_ledger_binding,
                expected_uid=0,
            )
        except Exception:
            raise PhysicalRollbackAdapterError(
                "production_linux_rollback_resource_ledger_invalid"
            ) from None
        bindings = physical_rollback_bindings_from_verified_ledger(
            records,
            verified_resources,
        )
        if (
            bindings.target("resolved_store_spec").resource_id != _sha(spec)
            or bindings.package_manifest_sha256
            != claim.package_manifest_sha256
        ):
            raise PhysicalRollbackAdapterError(
                "production_linux_rollback_factory_binding_invalid"
            )
        prerequisites = _rollback_image_prerequisites(spec, bindings)
        marker_state = _ProductionControllerAuthorityMarkerState(
            claim.execution_id
        )
        postgres_receipt_sink = StoreBackedPostgreSQLStageReceiptSink(
            receipt_store=self._receipt_store,
            execution_id=claim.execution_id,
            marker_state=marker_state,
        )
        transport_factory = ProductionLinuxStoreTransportFactory(
            held_lock=self._held_lock,
            verified_controller_runtime_capability=(
                self._verified_controller_runtime_capability
            ),
            postgres_receipt_sink=postgres_receipt_sink,
        )
        transports = transport_factory(
            execution_id=installation_execution_id,
            attempt_id=str(install["attempt_id"]),
            execution_binding_sha256=(
                str(installation_binding_sha256)
            ),
            resolved_store_spec=spec,
            artifacts=artifacts,
            prerequisites=prerequisites,
        )
        if (
            type(transports) is not BoundLinuxStoreTransports
            or type(transports.files) is not ClosedRootFileEffects
        ):
            raise PhysicalRollbackAdapterError(
                "production_linux_rollback_transport_invalid"
            )
        rollback_files = transports.files.for_rollback_authority(
            claim.execution_id
        )
        rollback_transports = BoundLinuxStoreTransports(
            transports.invariants,
            rollback_files,
            transports.docker,
            transports.postgres,
            transports.qdrant,
            transports.systemd,
        )
        supervisor = ExactSystemdSupervisor.from_rendered_unit(
            artifacts.render_supervisor_unit(
                execution_id=installation_execution_id,
                runtime_receipt_sha256=(
                    claim.controller_runtime_receipt_sha256
                ),
                package_manifest_sha256=claim.package_manifest_sha256,
            )
        )
        driver = ClosedLinuxPhysicalRollbackDriver(
            transports=rollback_transports,
            resolved_store_spec=spec,
            exact_supervisor=supervisor,
        )
        # Local import preserves the one-way marker -> operations type edge.
        from .live_rollback_marker import (
            production_live_rollback_marker_transport,
        )

        marker = _StateTrackingLiveRollbackMarkerTransport(
            production_live_rollback_marker_transport(
                root_file_effects=rollback_files,
                execution_id=claim.execution_id,
            ),
            marker_state,
        )
        eligibility_probe = ClosedLinuxLiveEmptyEligibilityProbe(
            postgres=transports.postgres,
            qdrant=transports.qdrant,
            signed_eligibility=eligibility,
        )
        retained_audit_source = ClosedLinuxRetainedAuditSource(
            authority_state=self._authority_state,
            held_lock=self._held_lock,
            rollback_authority=rollback_authority,
            claimed_rollback=claim,
            installation_receipt=install,
            installation_execution_binding_sha256=(
                str(installation_binding_sha256)
            ),
            installation_files=transports.files,
            resource_records=records,
            rollback_journal=rollback_journal,
        )
        return ExactPhysicalEmptyRollbackOperations(
            bindings=bindings,
            verified_resources=verified_resources,
            driver=driver,
            eligibility_probe=eligibility_probe,
            controller_authority_marker_transport=marker,
            retained_audit_source=retained_audit_source,
            receipt_store=self._receipt_store,
        )

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


@dataclass(frozen=True, slots=True)
class _RollbackImageIdentity:
    image_id: str
    reference: str
    repo_digest: str


@dataclass(frozen=True, slots=True)
class _RollbackLocalImages:
    postgres: _RollbackImageIdentity
    qdrant: _RollbackImageIdentity


@dataclass(frozen=True, slots=True)
class _RollbackImagePrerequisites:
    local_images: _RollbackLocalImages


def _rollback_image_prerequisites(
    spec: Mapping[str, object],
    bindings: PhysicalRollbackBindings,
) -> _RollbackImagePrerequisites:
    resources = spec["resources"]
    containers = resources["containers"]
    values: dict[str, _RollbackImageIdentity] = {}
    for name in ("postgres", "qdrant"):
        target = bindings.target(name + "_container")
        image = containers[name]["image"]
        if (
            target.image_id is None
            or target.image_repo_digest != image["repo_digest"]
            or not str(image["reference"]).endswith(
                "@" + str(image["repo_digest"]).split("@", 1)[1]
            )
        ):
            raise PhysicalRollbackAdapterError(
                "production_linux_rollback_image_binding_invalid"
            )
        values[name] = _RollbackImageIdentity(
            target.image_id,
            str(image["reference"]),
            str(image["repo_digest"]),
        )
    return _RollbackImagePrerequisites(
        _RollbackLocalImages(values["postgres"], values["qdrant"])
    )


class _ProductionControllerAuthorityMarkerState:
    """Process-local reflection of an independently durable root marker."""

    def __init__(self, execution_id: str) -> None:
        if _HASH_RE.fullmatch(execution_id) is None:
            raise PhysicalRollbackAdapterError(
                "production_rollback_marker_state_invalid"
            )
        self._execution_id = execution_id
        self._marker_sha256: str | None = None

    def mark_held(
        self,
        request: RollbackOperationRequest,
        observation: DurableLiveRollbackMarkerObservation,
    ) -> None:
        if (
            request.execution_id != self._execution_id
            or type(observation) is not DurableLiveRollbackMarkerObservation
        ):
            raise PhysicalRollbackAdapterError(
                "production_rollback_marker_state_invalid"
            )
        self._marker_sha256 = observation.controller_authority_marker_sha256

    def require_held(
        self,
        request: RollbackOperationRequest,
        observation: DurableLiveRollbackMarkerObservation,
    ) -> None:
        if (
            request.execution_id != self._execution_id
            or self._marker_sha256 is None
            or observation.controller_authority_marker_sha256
            != self._marker_sha256
        ):
            raise PhysicalRollbackAdapterError(
                "production_rollback_marker_not_held"
            )

    def mark_released(self, request: RollbackOperationRequest) -> None:
        if request.execution_id != self._execution_id:
            raise PhysicalRollbackAdapterError(
                "production_rollback_marker_state_invalid"
            )
        self._marker_sha256 = None

    def held(self) -> bool:
        return self._marker_sha256 is not None


class _StateTrackingLiveRollbackMarkerTransport:
    """Track only marker state that the durable transport reverified."""

    def __init__(
        self,
        transport: DurableLiveRollbackMarkerTransport,
        state: _ProductionControllerAuthorityMarkerState,
    ) -> None:
        if not all(
            callable(getattr(transport, name, None))
            for name in (
                "acquire_or_recover",
                "persist_semantic_empty_observation",
                "assert_held",
                "release_after_receipt",
            )
        ) or type(state) is not _ProductionControllerAuthorityMarkerState:
            raise PhysicalRollbackAdapterError(
                "production_rollback_marker_transport_invalid"
            )
        self._transport = transport
        self._state = state

    def acquire_or_recover(
        self,
        request: RollbackOperationRequest,
        *,
        marker_bound_postgres_identity_sha256: str,
        marker_bound_qdrant_identity_sha256: str,
        expected_semantic_empty_state_sha256: str,
    ) -> DurableLiveRollbackMarkerObservation:
        observed = self._transport.acquire_or_recover(
            request,
            marker_bound_postgres_identity_sha256=(
                marker_bound_postgres_identity_sha256
            ),
            marker_bound_qdrant_identity_sha256=(
                marker_bound_qdrant_identity_sha256
            ),
            expected_semantic_empty_state_sha256=(
                expected_semantic_empty_state_sha256
            ),
        )
        self._state.mark_held(request, observed)
        return observed

    def persist_semantic_empty_observation(
        self,
        request: RollbackOperationRequest,
        held: DurableLiveRollbackMarkerObservation,
        semantic_empty_state_sha256: str,
    ) -> DurableLiveRollbackMarkerObservation:
        self._state.require_held(request, held)
        observed = self._transport.persist_semantic_empty_observation(
            request,
            held,
            semantic_empty_state_sha256,
        )
        self._state.mark_held(request, observed)
        return observed

    def assert_held(
        self,
        request: RollbackOperationRequest,
        held: DurableLiveRollbackMarkerObservation,
    ) -> DurableLiveRollbackMarkerObservation:
        self._state.require_held(request, held)
        observed = self._transport.assert_held(request, held)
        self._state.mark_held(request, observed)
        return observed

    def release_after_receipt(
        self,
        request: RollbackOperationRequest,
        held: DurableLiveRollbackMarkerObservation,
    ) -> None:
        self._state.require_held(request, held)
        self._transport.release_after_receipt(request, held)
        self._state.mark_released(request)


_POSTGRES_RECEIPT_ARTIFACTS: Final = {
    ("install", "roles_privacy_preflight_complete"): (
        ReceiptArtifact.POSTGRES_INSTALL_I11
    ),
    ("install", "foundation_0001_applied"): ReceiptArtifact.POSTGRES_INSTALL_I12,
    ("install", "owner_claim_detail_0003_applied"): (
        ReceiptArtifact.POSTGRES_INSTALL_I13
    ),
    ("install", "ready_for_terminal_catalog"): (
        ReceiptArtifact.POSTGRES_INSTALL_I14
    ),
    ("rollback", "installed_0001_0003"): ReceiptArtifact.POSTGRES_ROLLBACK_I14,
    ("rollback", "installed_0001"): ReceiptArtifact.POSTGRES_ROLLBACK_I13,
    ("rollback", "exact_empty_canonical_database_prefix"): (
        ReceiptArtifact.POSTGRES_ROLLBACK_I12
    ),
    ("rollback", "empty"): ReceiptArtifact.POSTGRES_ROLLBACK_I11,
}


class StoreBackedPostgreSQLStageReceiptSink:
    """Persist exact native-stage receipts through ``DurableReceiptStore``."""

    def __init__(
        self,
        *,
        receipt_store: DurableReceiptStore,
        execution_id: str,
        marker_state: _ProductionControllerAuthorityMarkerState,
        allowed_mode: str = "rollback",
    ) -> None:
        if (
            type(receipt_store) is not DurableReceiptStore
            or _HASH_RE.fullmatch(execution_id) is None
            or type(marker_state) is not _ProductionControllerAuthorityMarkerState
            or allowed_mode not in {"install", "rollback"}
        ):
            raise PhysicalRollbackAdapterError(
                "postgres_stage_receipt_sink_invalid"
            )
        self._store = receipt_store
        self._execution_id = execution_id
        self._marker_state = marker_state
        self._allowed_mode = allowed_mode

    def postgres_controller_authority_marker_held(self) -> bool:
        return self._marker_state.held()

    def persist_postgres_stage_receipt(
        self, receipt: NativeStageReceipt
    ) -> None:
        if (
            type(receipt) is not NativeStageReceipt
            or receipt.mode != self._allowed_mode
            or (
                receipt.mode == "rollback"
                and self._marker_state.held() is not True
            )
        ):
            raise PhysicalRollbackAdapterError(
                "postgres_stage_receipt_sink_refused"
            )
        artifact = _POSTGRES_RECEIPT_ARTIFACTS.get(
            (receipt.mode, receipt.final_state)
        )
        if artifact is None:
            raise PhysicalRollbackAdapterError(
                "postgres_stage_receipt_sink_refused"
            )
        native = {
            "schema_version": "governed-memory-postgres-native-stage-receipt-v1",
            "mode": receipt.mode,
            "final_state": receipt.final_state,
            "source_closure_sha256": receipt.source_closure_sha256,
            "runtime_receipt_sha256": receipt.runtime_receipt_sha256,
            "driver_runtime_identity_sha256": (
                receipt.driver_runtime_identity_sha256
            ),
            "terminal_catalog_sha256": receipt.terminal_catalog_sha256,
            "rollback_empty_proof_sha256": (
                receipt.rollback_empty_proof_sha256
            ),
            "operations": list(receipt.operations),
            "receipt_sha256": receipt.receipt_sha256,
        }
        if hashlib.sha256(
            canonical_json_bytes(
                {key: value for key, value in native.items() if key != "receipt_sha256"}
            )
        ).hexdigest() != receipt.receipt_sha256:
            raise PhysicalRollbackAdapterError(
                "postgres_stage_receipt_sink_refused"
            )
        wrapper = {
            "schema_version": (
                "governed-memory-postgres-native-stage-durable-receipt-v1"
            ),
            "execution_id": self._execution_id,
            "native_receipt": native,
            "receipt_sha256": receipt.receipt_sha256,
        }
        self._store.write_once(artifact, self._execution_id, wrapper)


class ClosedLinuxLiveEmptyEligibilityProbe:
    """Fresh content-free empty proof over the two fixed successor stores."""

    def __init__(
        self,
        *,
        postgres: object,
        qdrant: object,
        signed_eligibility: Mapping[str, object],
    ) -> None:
        eligibility = verify_empty_rollback_eligibility_receipt(
            signed_eligibility
        )
        if (
            getattr(postgres, "bind", None) != "127.0.0.1:55435"
            or getattr(qdrant, "bind", None) != "127.0.0.1:6346"
            or not callable(getattr(postgres, "inspect_terminal", None))
            or not callable(getattr(qdrant, "inspect_terminal", None))
        ):
            raise PhysicalRollbackAdapterError(
                "live_empty_eligibility_probe_invalid"
            )
        self._postgres = postgres
        self._qdrant = qdrant
        self._signed = eligibility

    @staticmethod
    def _application_writers_absent() -> bool:
        for raw in _APPLICATION_WRITER_PATHS:
            try:
                os.stat(raw, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError:
                raise PhysicalRollbackAdapterError(
                    "live_empty_application_writer_observation_failed"
                ) from None
            return False
        return True

    def observe(
        self, request: RollbackOperationRequest
    ) -> Mapping[str, object]:
        if (
            type(request) is not RollbackOperationRequest
            or request.installation_execution_id
            != self._signed["installation_execution_id"]
            or request.installation_receipt_sha256
            != self._signed["installation_receipt_sha256"]
            or request.package_manifest_sha256
            != self._signed["package_manifest_sha256"]
            or request.resource_ledger_head_sha256
            != self._signed["resource_ledger_head_sha256"]
            or request.exact_targets_sha256
            != self._signed["exact_targets_sha256"]
        ):
            raise PhysicalRollbackAdapterError(
                "live_empty_eligibility_request_mismatch"
            )
        try:
            postgres = self._postgres.inspect_terminal()
            qdrant = self._qdrant.inspect_terminal()
        except Exception:
            raise PhysicalRollbackAdapterError(
                "live_empty_store_observation_failed"
            ) from None
        writers_absent = self._application_writers_absent()
        if (
            type(postgres) is not TerminalPostgreSQLSnapshot
            or type(qdrant) is not TerminalQdrantSnapshot
            or writers_absent is not True
        ):
            raise PhysicalRollbackAdapterError(
                "live_empty_writer_fence_not_established"
            )
        observation_set_sha256 = _sha(
            {
                "schema_version": "governed-memory-live-empty-observation-v1",
                "application_writer_paths_absent": list(
                    _APPLICATION_WRITER_PATHS
                ),
                "postgres": {
                    "active_client_count": postgres.active_client_count,
                    "catalog_identity_set_sha256": _sha(
                        [item.as_dict() for item in postgres.catalog_identities]
                    ),
                    "governed_user_row_count": postgres.governed_user_row_count,
                    "source_connection_count": postgres.source_connection_count,
                },
                "qdrant": {
                    "collection_config_sha256": _sha(
                        qdrant.collection_config.canonical_document()
                    ),
                    "point_count": qdrant.point_count,
                    "source_endpoint_count": qdrant.source_endpoint_count,
                    "unexpected_candidate_collection_count": (
                        qdrant.unexpected_candidate_collection_count
                    ),
                },
            }
        )
        return build_empty_rollback_eligibility_receipt(
            candidate_git_commit=str(self._signed["candidate_git_commit"]),
            candidate_git_tree=str(self._signed["candidate_git_tree"]),
            package_manifest_sha256=str(
                self._signed["package_manifest_sha256"]
            ),
            installation_execution_id=str(
                self._signed["installation_execution_id"]
            ),
            installation_receipt_sha256=str(
                self._signed["installation_receipt_sha256"]
            ),
            exact_targets_sha256=str(self._signed["exact_targets_sha256"]),
            resource_ledger_head_sha256=str(
                self._signed["resource_ledger_head_sha256"]
            ),
            observation_set_sha256=observation_set_sha256,
            pilot_ever_started=False,
            postgresql_user_rows=postgres.governed_user_row_count,
            projection_queue_rows=0,
            qdrant_points=qdrant.point_count,
            active_clients=postgres.active_client_count,
            legacy_imports=0,
            application_services_installed=False,
            source_postgres_read_count=0,
            production_data_read=False,
            provider_calls=0,
        )


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise PhysicalRollbackAdapterError(
                "retained_audit_json_duplicate_key"
            )
        value[key] = item
    return value


def _read_fixed_root_regular(path: Path, *, max_bytes: int) -> bytes:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow == 0 or not path.is_absolute():
        raise PhysicalRollbackAdapterError("retained_audit_path_invalid")
    directory_fd = file_fd = -1
    try:
        directory_fd = os.open(
            path.parent,
            os.O_RDONLY
            | os.O_CLOEXEC
            | nofollow
            | getattr(os, "O_DIRECTORY", 0),
        )
        opened_directory = os.fstat(directory_fd)
        named_directory = path.parent.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened_directory.st_mode)
            or stat.S_IMODE(opened_directory.st_mode) != 0o700
            or opened_directory.st_uid != 0
            or (opened_directory.st_dev, opened_directory.st_ino)
            != (named_directory.st_dev, named_directory.st_ino)
        ):
            raise PhysicalRollbackAdapterError(
                "retained_audit_directory_invalid"
            )
        file_fd = os.open(
            path.name,
            os.O_RDONLY | os.O_CLOEXEC | nofollow,
            dir_fd=directory_fd,
        )
        opened = os.fstat(file_fd)
        named = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_uid != 0
            or opened.st_nlink != 1
            or opened.st_size > max_bytes
            or (opened.st_dev, opened.st_ino)
            != (named.st_dev, named.st_ino)
        ):
            raise PhysicalRollbackAdapterError(
                "retained_audit_file_invalid"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(file_fd, min(64 * 1024, max_bytes + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > max_bytes:
                raise PhysicalRollbackAdapterError(
                    "retained_audit_file_invalid"
                )
        after = os.fstat(file_fd)
        if (after.st_dev, after.st_ino, after.st_size) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
        ):
            raise PhysicalRollbackAdapterError(
                "retained_audit_file_changed"
            )
        return b"".join(chunks)
    except PhysicalRollbackAdapterError:
        raise
    except OSError:
        raise PhysicalRollbackAdapterError(
            "retained_audit_file_unavailable"
        ) from None
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if directory_fd >= 0:
            os.close(directory_fd)


class ClosedLinuxRetainedAuditSource:
    """Reverify retained evidence through existing durable primitives."""

    def __init__(
        self,
        *,
        authority_state: AuthorityState,
        held_lock: HeldExecutionLockCapability,
        rollback_authority: object,
        claimed_rollback: object,
        installation_receipt: Mapping[str, object],
        installation_execution_binding_sha256: str,
        installation_files: ClosedRootFileEffects,
        resource_records: tuple[ResourceIdentityRecord, ...],
        rollback_journal: DurableRollbackJournal,
    ) -> None:
        try:
            validate_held_execution_lock(held_lock)
            install = verify_install_receipt(installation_receipt)
        except Exception:
            raise PhysicalRollbackAdapterError(
                "retained_audit_source_invalid"
            ) from None
        if (
            type(authority_state) is not AuthorityState
            or _HASH_RE.fullmatch(installation_execution_binding_sha256)
            is None
            or type(installation_files) is not ClosedRootFileEffects
            or type(resource_records) is not tuple
            or not resource_records
            or type(rollback_journal) is not DurableRollbackJournal
            or getattr(claimed_rollback, "execution_id", None)
            != rollback_journal.execution_id
        ):
            raise PhysicalRollbackAdapterError(
                "retained_audit_source_invalid"
            )
        self._state = authority_state
        self._lock = held_lock
        self._authority = rollback_authority
        self._claim = claimed_rollback
        self._install = install
        self._install_binding = installation_execution_binding_sha256
        self._files = installation_files
        self._resource_records = resource_records
        self._rollback_journal = rollback_journal

    def _require_request(self, request: RollbackOperationRequest) -> None:
        try:
            validate_held_execution_lock(self._lock)
        except Exception:
            raise PhysicalRollbackAdapterError(
                "retained_audit_lock_not_held"
            ) from None
        if (
            type(request) is not RollbackOperationRequest
            or request.execution_id != self._claim.execution_id
            or request.journal_binding_sha256
            != self._claim.journal_binding_sha256
            or request.installation_execution_id
            != self._install["execution_id"]
            or request.installation_receipt_sha256
            != self._install["receipt_sha256"]
        ):
            raise PhysicalRollbackAdapterError(
                "retained_audit_request_invalid"
            )

    @staticmethod
    def _rollback_records_bytes(
        records: tuple[object, ...],
    ) -> bytes:
        return b"".join(
            canonical_json_bytes(
                {
                    "schema_version": "governed-memory-empty-store-rollback-journal-v3",
                    "plan_sha256": record.plan_sha256,
                    "attempt_id": record.attempt_id,
                    "sequence": record.sequence,
                    "step_id": record.step_id,
                    "event": record.event,
                    "prior_record_sha256": record.prior_record_sha256,
                    "record_sha256": record.record_sha256,
                }
            )
            + b"\n"
            for record in records
        )

    def authorization_nonce_sha256(
        self, request: RollbackOperationRequest
    ) -> str:
        self._require_request(request)
        try:
            observed = self._state.claim_nonce(
                self._authority.nonce,
                operation="empty_store_rollback",
                execution_sha256=self._claim.execution_id,
                authorization_sha256=self._authority.authorization_sha256,
                scope_sha256=self._authority.scope_sha256,
                trust_bundle_sha256=self._authority.trust_bundle_sha256,
                held_lock=self._lock,
                allow_new_claim=False,
            )
        except Exception:
            raise PhysicalRollbackAdapterError(
                "retained_audit_nonce_unavailable"
            ) from None
        expected = nonce_sha256(self._authority.nonce)
        if (
            observed.nonce_sha256 != expected
            or observed.claim_sha256 != self._claim.claim_sha256
        ):
            raise PhysicalRollbackAdapterError(
                "retained_audit_nonce_mismatch"
            )
        return expected

    def installation_journal_sha256(
        self, request: RollbackOperationRequest
    ) -> str:
        self._require_request(request)
        path = Path(
            _INSTALL_JOURNAL_TEMPLATE.replace(
                "{execution_id}", request.installation_execution_id
            )
        )
        raw = _read_fixed_root_regular(path, max_bytes=16 * 1024 * 1024)
        try:
            records = parse_durable_journal_bytes(
                raw,
                expected_plan_sha256=str(self._install["plan_sha256"]),
                expected_attempt_id=str(self._install["attempt_id"]),
            )
        except Exception:
            raise PhysicalRollbackAdapterError(
                "retained_audit_installation_journal_invalid"
            ) from None
        if (
            len(records) != self._install["journal_sequence"]
            or not records
            or records[-1].record_sha256
            != self._install["journal_head_sha256"]
        ):
            raise PhysicalRollbackAdapterError(
                "retained_audit_installation_journal_mismatch"
            )
        return hashlib.sha256(raw).hexdigest()

    def rollback_journal_sha256(
        self, request: RollbackOperationRequest
    ) -> str:
        self._require_request(request)
        records = self._rollback_journal.records()
        if not records:
            raise PhysicalRollbackAdapterError(
                "retained_audit_rollback_journal_empty"
            )
        return hashlib.sha256(self._rollback_records_bytes(records)).hexdigest()

    def authority_anchor_sha256(
        self, request: RollbackOperationRequest
    ) -> str:
        self._require_request(request)
        install_anchor = self._state.read_anchor(self._install_binding)
        rollback_records = self._rollback_journal.records()
        rollback_anchor = self._state.read_anchor(
            request.journal_binding_sha256
        )
        resource_anchor = self._state.read_resource_ledger_anchor(
            request.resource_ledger_binding_sha256,
            held_lock=self._lock,
        )
        if (
            install_anchor.sequence != self._install["journal_sequence"]
            or install_anchor.head_sha256
            != self._install["journal_head_sha256"]
            or rollback_anchor.sequence != len(rollback_records)
            or not rollback_records
            or rollback_anchor.head_sha256
            != rollback_records[-1].record_sha256
            or resource_anchor.sequence != len(self._resource_records)
            or resource_anchor.head_sha256
            != self._resource_records[-1].entry_sha256
        ):
            raise PhysicalRollbackAdapterError(
                "retained_audit_anchor_mismatch"
            )
        return _sha(
            {
                "installation_journal": {
                    "binding_sha256": install_anchor.binding_sha256,
                    "head_sha256": install_anchor.head_sha256,
                    "sequence": install_anchor.sequence,
                },
                "resource_ledger": {
                    "binding_sha256": resource_anchor.binding_sha256,
                    "head_sha256": resource_anchor.head_sha256,
                    "sequence": resource_anchor.sequence,
                },
                "rollback_journal": {
                    "binding_sha256": rollback_anchor.binding_sha256,
                    "head_sha256": rollback_anchor.head_sha256,
                    "sequence": rollback_anchor.sequence,
                },
            },
            domain=_AUDIT_ANCHOR_DOMAIN,
        )

    def resource_identity_ledger_sha256(
        self, request: RollbackOperationRequest
    ) -> str:
        self._require_request(request)
        if (
            request.resource_ledger_sequence != len(self._resource_records)
            or request.resource_ledger_head_sha256
            != self._resource_records[-1].entry_sha256
        ):
            raise PhysicalRollbackAdapterError(
                "retained_audit_resource_ledger_mismatch"
            )
        raw = b"".join(
            canonical_json_bytes(record.as_dict()) + b"\n"
            for record in self._resource_records
        )
        return hashlib.sha256(raw).hexdigest()

    def terminal_postflight_receipt_sha256(
        self, request: RollbackOperationRequest
    ) -> str:
        self._require_request(request)
        raw = self._files.read_terminal_postflight_receipt()
        if type(raw) is not bytes:
            raise PhysicalRollbackAdapterError(
                "retained_audit_terminal_postflight_absent"
            )
        try:
            document = json.loads(
                raw.decode("ascii"),
                object_pairs_hook=_strict_json_object,
                parse_constant=lambda unused: (_ for _ in ()).throw(
                    PhysicalRollbackAdapterError(
                        "retained_audit_terminal_postflight_invalid"
                    )
                ),
            )
        except PhysicalRollbackAdapterError:
            raise
        except Exception:
            raise PhysicalRollbackAdapterError(
                "retained_audit_terminal_postflight_invalid"
            ) from None
        if (
            type(document) is not dict
            or canonical_json_bytes(document) + b"\n" != raw
            or document.get("execution_id")
            != request.installation_execution_id
            or document.get("package_manifest_sha256")
            != request.package_manifest_sha256
            or document.get("receipt_sha256")
            != self._install["postflight_receipt_sha256"]
            or hashlib.sha256(
                canonical_json_bytes(
                    {
                        key: value
                        for key, value in document.items()
                        if key != "receipt_sha256"
                    }
                )
            ).hexdigest()
            != document.get("receipt_sha256")
        ):
            raise PhysicalRollbackAdapterError(
                "retained_audit_terminal_postflight_mismatch"
            )
        return str(document["receipt_sha256"])


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
        supervisor = self._observe_exact(
            self._bindings.target("stores_supervisor")
        )
        containers = tuple(
            self._observe_exact(self._bindings.target(key))
            for key in ("postgres_container", "qdrant_container")
        )
        if supervisor.state != "absent" or not (
            all(item.state == "running" for item in containers)
            or all(item.state == "absent" for item in containers)
        ):
            raise PhysicalRollbackAdapterError(
                "physical_rollback_writer_fence_precondition_invalid"
            )
        if all(item.state == "absent" for item in containers):
            # Completed replay cannot query physically erased stores.  Exact
            # ledger-bound absence is stronger than a fresh row/point query;
            # reuse only the already verified durable eligibility bytes so the
            # renewed marker can still bind and release its semantic record.
            verified = verify_empty_rollback_eligibility_receipt(
                self._controller_authority_marker.eligibility
            )
        else:
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
        if (
            verified["active_clients"] != 0
            or verified["application_services_installed"] is not False
        ):
            raise PhysicalRollbackAdapterError(
                "physical_rollback_writer_fence_not_established"
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
        if (
            self._controller_authority_marker.observation.semantic_empty_state_sha256
            is None
        ):
            raise PhysicalRollbackAdapterError(
                "physical_rollback_writer_fence_not_established"
            )
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
    "ClosedLinuxLiveEmptyEligibilityProbe",
    "ClosedLinuxPhysicalRollbackDriver",
    "ClosedLinuxRetainedAuditSource",
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
    "ProductionLinuxEmptyRollbackOperationsFactory",
    "RetainedAuditSource",
    "StoreBackedPostgreSQLStageReceiptSink",
    "physical_rollback_bindings_from_verified_ledger",
]
