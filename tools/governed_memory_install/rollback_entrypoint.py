from __future__ import annotations

"""Callable, separately authorized empty-store rollback composition.

The module exposes no CLI, shell, subprocess, endpoint, secret, or generic
command surface.  Effects are available only through a closed injected
operation protocol after signature verification, trusted-time validation,
global-lock validation, durable nonce claim, exact empty-state verification,
and exact resource binding.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Final, Mapping, Protocol

from .authority_state import (
    AuthorityClaimNotAllowedError,
    AuthorityState,
    AuthorityStateError,
    nonce_sha256,
)
from .execution_authority import TrustedUtcClock, read_trusted_utc
from .controller_runtime import (
    ControllerRuntimeCapabilityError,
    _controller_runtime_capability_evidence,
)
from .execution_lock import (
    ExecutionLockError,
    HeldExecutionLockCapability,
    validate_held_execution_lock,
)
from .rollback import (
    EMPTY_ROLLBACK_STEPS,
    ROLLBACK_RESOURCE_KEYS,
    EmptyRollbackError,
    EmptyRollbackPlan,
    EmptyRollbackPlanStep,
    ExactRollbackResource,
    VerifiedRollbackResourceEvidence,
    build_empty_rollback_plan,
    canonical_json_bytes,
    exact_rollback_targets_sha256,
    verified_rollback_resource_parts,
    verify_built_empty_rollback_plan,
    verify_empty_rollback_eligibility_receipt,
)
from .receipts import (
    ReceiptError,
    build_empty_rollback_receipt,
    verify_install_receipt,
)
from .durable_receipts import (
    DurableReceiptError,
    DurableReceiptStore,
    PRODUCTION_EXECUTIONS_ROOT,
    ReceiptArtifact,
    _open_directory_nofollow,
)
from .rollback_authority import (
    RECOVERY_RESERVATION_OPERATION,
    ROLLBACK_OPERATION,
    RollbackAuthorityError,
    rollback_capability_evidence,
)
from .package_capability import (
    PackageCapabilityError,
    _package_capability_parts,
)


GLOBAL_LOCK_PATH: Final = Path(
    "/run/lock/governed-memory-controller/execution.lock"
)
AUTHORITY_STATE_PATH: Final = Path(
    "/var/lib/governed-memory-controller/authority-state.sqlite3"
)
ROLLBACK_JOURNAL_TEMPLATE: Final = (
    "/var/lib/governed-memory-controller/executions/"
    "{execution_id}/rollback.jsonl"
)
ROLLBACK_JOURNAL_SCHEMA: Final = "governed-memory-empty-store-rollback-journal-v3"
ROLLBACK_CLAIM_SCHEMA: Final = "governed-memory-claimed-empty-rollback-v3"
ZERO_HEAD: Final = "0" * 64
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_ATTEMPT_RE = re.compile(r"rollback-[0-9a-f]{40}\Z", re.ASCII)
_STEP_RE = re.compile(r"R(?:[0-9]{2})_[A-Z0-9_]+\Z", re.ASCII)
_TIMESTAMP_RE = re.compile(
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z\Z",
    re.ASCII,
)
_LIVE_ROLLBACK_CONTROLLER_AUTHORITY_MARKER_DOMAIN: Final = (
    b"governed-memory-live-rollback-controller-authority-marker-v1\x00"
)
_EXECUTION_DOMAIN: Final = b"governed-memory-empty-rollback-execution-v3\x00"
_ATTEMPT_DOMAIN: Final = b"governed-memory-empty-rollback-attempt-v3\x00"
_JOURNAL_DOMAIN: Final = b"governed-memory-empty-rollback-journal-v3\x00"
_RETAINED_AUDIT_SET_DOMAIN: Final = (
    b"governed-memory-empty-rollback-retained-audit-set-v1\x00"
)
_RETAINED_ROLLBACK_EVIDENCE_DOMAIN: Final = (
    b"governed-memory-empty-rollback-retained-evidence-v1\x00"
)
MAX_RETAINED_INSTALL_RECEIPT_BYTES: Final = 64 * 1024


class EmptyRollbackExecutionError(RuntimeError):
    """Content-free refusal from the rollback execution composition."""


class RollbackEvent(str, Enum):
    INTENT = "intent"
    APPLIED = "applied"
    COMPLETE = "complete"


class RollbackAuthorityMode(str, Enum):
    START_ROLLBACK = "start-rollback"
    START_RESERVED_ROLLBACK = "start-reserved-rollback"
    RESUME_ROLLBACK = "resume-rollback"


@dataclass(frozen=True, slots=True)
class RollbackJournalRecord:
    plan_sha256: str
    attempt_id: str
    sequence: int
    step_id: str
    event: str
    prior_record_sha256: str
    record_sha256: str

    @classmethod
    def create(
        cls,
        *,
        plan_sha256: str,
        attempt_id: str,
        sequence: int,
        step_id: str,
        event: RollbackEvent,
        prior_record_sha256: str,
    ) -> RollbackJournalRecord:
        document = {
            "schema_version": ROLLBACK_JOURNAL_SCHEMA,
            "plan_sha256": plan_sha256,
            "attempt_id": attempt_id,
            "sequence": sequence,
            "step_id": step_id,
            "event": event.value,
            "prior_record_sha256": prior_record_sha256,
        }
        digest = hashlib.sha256(canonical_json_bytes(document)).hexdigest()
        return cls(
            plan_sha256=plan_sha256,
            attempt_id=attempt_id,
            sequence=sequence,
            step_id=step_id,
            event=event.value,
            prior_record_sha256=prior_record_sha256,
            record_sha256=digest,
        )


def validate_rollback_records(
    records: tuple[RollbackJournalRecord, ...],
    *,
    plan_sha256: str,
    attempt_id: str,
) -> tuple[RollbackJournalRecord, ...]:
    if (
        type(records) is not tuple
        or _HASH_RE.fullmatch(plan_sha256) is None
        or _ATTEMPT_RE.fullmatch(attempt_id) is None
    ):
        raise EmptyRollbackExecutionError("empty_rollback_journal_invalid")
    prior = ZERO_HEAD
    expected_index = 0
    open_intent = False
    complete = False
    for sequence, record in enumerate(records, start=1):
        if type(record) is not RollbackJournalRecord:
            raise EmptyRollbackExecutionError("empty_rollback_journal_invalid")
        try:
            event = RollbackEvent(record.event)
        except ValueError as error:
            raise EmptyRollbackExecutionError(
                "empty_rollback_journal_event_invalid"
            ) from error
        if (
            record.plan_sha256 != plan_sha256
            or record.attempt_id != attempt_id
            or record.sequence != sequence
            or record.prior_record_sha256 != prior
            or _STEP_RE.fullmatch(record.step_id) is None
            or record
            != RollbackJournalRecord.create(
                plan_sha256=plan_sha256,
                attempt_id=attempt_id,
                sequence=sequence,
                step_id=record.step_id,
                event=event,
                prior_record_sha256=prior,
            )
        ):
            raise EmptyRollbackExecutionError("empty_rollback_journal_invalid")
        if complete:
            raise EmptyRollbackExecutionError("empty_rollback_journal_after_complete")
        if event is RollbackEvent.COMPLETE:
            if (
                open_intent
                or expected_index != len(EMPTY_ROLLBACK_STEPS)
                or record.step_id != "R99_ROLLBACK_COMPLETE"
            ):
                raise EmptyRollbackExecutionError(
                    "empty_rollback_journal_complete_invalid"
                )
            complete = True
        else:
            if expected_index >= len(EMPTY_ROLLBACK_STEPS):
                raise EmptyRollbackExecutionError(
                    "empty_rollback_journal_step_invalid"
                )
            expected_step = EMPTY_ROLLBACK_STEPS[expected_index].step_id
            if record.step_id != expected_step:
                raise EmptyRollbackExecutionError(
                    "empty_rollback_journal_step_invalid"
                )
            if event is RollbackEvent.INTENT:
                if open_intent:
                    raise EmptyRollbackExecutionError(
                        "empty_rollback_journal_intent_invalid"
                    )
                open_intent = True
            elif not open_intent:
                raise EmptyRollbackExecutionError(
                    "empty_rollback_journal_applied_without_intent"
                )
            else:
                open_intent = False
                expected_index += 1
        prior = record.record_sha256
    return records


@dataclass(frozen=True, slots=True)
class ClaimedEmptyRollbackEvidence:
    schema_version: str
    operation: str
    claim_result: str
    execution_id: str
    attempt_id: str
    claim_sha256: str
    authorization_sha256: str
    scope_sha256: str
    plan_sha256: str
    package_manifest_sha256: str
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
    installation_execution_id: str
    recovery_reservation_claim_sha256: str
    retained_evidence_sha256: str
    journal_binding_sha256: str
    journal_path: str
    authority_state_path_sha256: str
    global_lock_path_sha256: str


_CLAIM_TOKEN = object()


class _ClaimedEmptyRollback:
    __slots__ = ("_evidence", "_token")

    def __init__(
        self, evidence: ClaimedEmptyRollbackEvidence, token: object
    ) -> None:
        if token is not _CLAIM_TOKEN:
            raise EmptyRollbackExecutionError("empty_rollback_claim_invalid")
        self._evidence = evidence
        self._token = token

    def __repr__(self) -> str:
        return "ClaimedEmptyRollback(<content-redacted>)"


def claimed_empty_rollback_evidence(
    value: object,
) -> ClaimedEmptyRollbackEvidence:
    if (
        type(value) is not _ClaimedEmptyRollback
        or value._token is not _CLAIM_TOKEN
        or type(value._evidence) is not ClaimedEmptyRollbackEvidence
    ):
        raise EmptyRollbackExecutionError("empty_rollback_claim_invalid")
    return value._evidence


@dataclass(frozen=True, slots=True)
class RollbackOperationRequest:
    step: EmptyRollbackPlanStep
    resource: ExactRollbackResource | None
    execution_id: str
    attempt_id: str
    journal_binding_sha256: str
    plan_sha256: str
    package_manifest_sha256: str
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
    installation_execution_id: str
    installation_receipt_sha256: str
    recovery_reservation_claim_sha256: str
    retained_evidence_sha256: str
    eligibility_receipt_sha256: str
    resource_ledger_binding_sha256: str
    resource_ledger_head_sha256: str
    resource_ledger_sequence: int
    exact_targets_sha256: str

    def __post_init__(self) -> None:
        expected = next(
            (item for item in EMPTY_ROLLBACK_STEPS if item.step_id == self.step.step_id),
            None,
        )
        if (
            type(self.step) is not EmptyRollbackPlanStep
            or self.step != expected
            or (
                self.step.resource_key is None
                and self.resource is not None
            )
            or (
                self.step.resource_key is not None
                and (
                    type(self.resource) is not ExactRollbackResource
                    or self.resource.resource_key != self.step.resource_key
                )
            )
            or _HASH_RE.fullmatch(self.execution_id) is None
            or _ATTEMPT_RE.fullmatch(self.attempt_id) is None
            or _HASH_RE.fullmatch(self.journal_binding_sha256) is None
            or _HASH_RE.fullmatch(self.plan_sha256) is None
            or _HASH_RE.fullmatch(self.package_manifest_sha256) is None
            or any(
                _HASH_RE.fullmatch(value) is None
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
            or self.controller_release_root
            != (
                "/opt/governed-memory-controller/releases/"
                + self.package_manifest_sha256
            )
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
            or _HASH_RE.fullmatch(self.installation_receipt_sha256) is None
            or _HASH_RE.fullmatch(
                self.recovery_reservation_claim_sha256
            )
            is None
            or _HASH_RE.fullmatch(self.retained_evidence_sha256) is None
            or _HASH_RE.fullmatch(self.installation_execution_id) is None
            or _HASH_RE.fullmatch(self.eligibility_receipt_sha256) is None
            or _HASH_RE.fullmatch(self.resource_ledger_binding_sha256) is None
            or _HASH_RE.fullmatch(self.resource_ledger_head_sha256) is None
            or type(self.resource_ledger_sequence) is not int
            or self.resource_ledger_sequence < len(ROLLBACK_RESOURCE_KEYS)
            or _HASH_RE.fullmatch(self.exact_targets_sha256) is None
        ):
            raise EmptyRollbackExecutionError(
                "empty_rollback_operation_request_invalid"
            )


@dataclass(frozen=True, slots=True)
class RollbackObservation:
    state: str
    revision_sha256: str
    ownership_sha256: str
    controller_runtime_tree_sha256: str
    controller_release_tree_sha256: str

    def __post_init__(self) -> None:
        if self.state not in {"before", "after", "recoverable", "drift"} or any(
            _HASH_RE.fullmatch(value) is None
            for value in (
                self.revision_sha256,
                self.ownership_sha256,
                self.controller_runtime_tree_sha256,
                self.controller_release_tree_sha256,
            )
        ):
            raise EmptyRollbackExecutionError(
                "empty_rollback_observation_invalid"
            )


def canonical_live_rollback_controller_authority_marker_sha256(
    request: RollbackOperationRequest,
    *,
    marker_bound_postgres_identity_sha256: str,
    marker_bound_qdrant_identity_sha256: str,
) -> str:
    if (
        type(request) is not RollbackOperationRequest
        or _HASH_RE.fullmatch(
            marker_bound_postgres_identity_sha256
        ) is None
        or _HASH_RE.fullmatch(
            marker_bound_qdrant_identity_sha256
        ) is None
    ):
        raise EmptyRollbackExecutionError(
            "empty_rollback_controller_authority_marker_identity_invalid"
        )
    return hashlib.sha256(
        _LIVE_ROLLBACK_CONTROLLER_AUTHORITY_MARKER_DOMAIN
        + canonical_json_bytes(
            {
                "schema_version": "governed-memory-live-rollback-controller-authority-marker-v1",
                "execution_id": request.execution_id,
                "attempt_id": request.attempt_id,
                "plan_sha256": request.plan_sha256,
                "eligibility_receipt_sha256": (
                    request.eligibility_receipt_sha256
                ),
                "recovery_reservation_claim_sha256": (
                    request.recovery_reservation_claim_sha256
                ),
                "retained_evidence_sha256": (
                    request.retained_evidence_sha256
                ),
                "exact_targets_sha256": request.exact_targets_sha256,
                "controller_runtime_tree_sha256": (
                    request.controller_runtime_tree_sha256
                ),
                "controller_release_tree_sha256": (
                    request.controller_release_tree_sha256
                ),
                "marker_bound_postgres_identity_sha256": (
                    marker_bound_postgres_identity_sha256
                ),
                "marker_bound_qdrant_identity_sha256": (
                    marker_bound_qdrant_identity_sha256
                ),
            }
        )
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class EmptyRollbackControllerAuthorityMarkerAcquisition:
    execution_id: str
    attempt_id: str
    plan_sha256: str
    exact_targets_sha256: str
    controller_runtime_tree_sha256: str
    controller_release_tree_sha256: str
    marker_bound_postgres_identity_sha256: str
    marker_bound_qdrant_identity_sha256: str
    controller_authority_marker_sha256: str

    def __post_init__(self) -> None:
        if (
            _HASH_RE.fullmatch(self.execution_id) is None
            or _ATTEMPT_RE.fullmatch(self.attempt_id) is None
            or _HASH_RE.fullmatch(self.plan_sha256) is None
            or _HASH_RE.fullmatch(self.exact_targets_sha256) is None
            or _HASH_RE.fullmatch(self.controller_runtime_tree_sha256) is None
            or _HASH_RE.fullmatch(self.controller_release_tree_sha256) is None
            or _HASH_RE.fullmatch(
                self.marker_bound_postgres_identity_sha256
            ) is None
            or _HASH_RE.fullmatch(
                self.marker_bound_qdrant_identity_sha256
            ) is None
            or _HASH_RE.fullmatch(self.controller_authority_marker_sha256) is None
        ):
            raise EmptyRollbackExecutionError(
                "empty_rollback_controller_authority_marker_acquisition_invalid"
            )


@dataclass(frozen=True, slots=True)
class EmptyRollbackControllerAuthorityMarkerEvidence:
    execution_id: str
    attempt_id: str
    plan_sha256: str
    signed_eligibility_receipt_sha256: str
    exact_targets_sha256: str
    controller_runtime_tree_sha256: str
    controller_release_tree_sha256: str
    marker_bound_postgres_identity_sha256: str
    marker_bound_qdrant_identity_sha256: str
    controller_authority_marker_sha256: str


_CONTROLLER_AUTHORITY_MARKER_TOKEN = object()


class _EmptyRollbackControllerAuthorityMarkerCapability:
    __slots__ = ("_evidence", "_token")

    def __init__(
        self, evidence: EmptyRollbackControllerAuthorityMarkerEvidence, token: object
    ) -> None:
        if token is not _CONTROLLER_AUTHORITY_MARKER_TOKEN:
            raise EmptyRollbackExecutionError(
                "empty_rollback_controller_authority_marker_capability_invalid"
            )
        self._evidence = evidence
        self._token = token

    def __repr__(self) -> str:
        return "EmptyRollbackControllerAuthorityMarkerCapability(<content-redacted>)"


@dataclass(frozen=True, slots=True)
class VerifiedInstallReceiptLedgerEvidence:
    canonical_receipt_sha256: str
    installation_execution_id: str
    installation_receipt_sha256: str
    candidate_git_commit: str
    candidate_git_tree: str
    package_manifest_sha256: str
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
    resource_ledger_binding_sha256: str
    resource_ledger_head_sha256: str
    resource_ledger_sequence: int
    exact_targets_sha256: str
    exact_rollback_resources_count: int
    recovery_reservation_claim_sha256: str
    retained_evidence_sha256: str


_INSTALL_LEDGER_TOKEN = object()


class _VerifiedInstallReceiptLedgerCapability:
    """Opaque result minted only by the trusted retained-artifact adapter.

    This is a composition boundary, not a claim of isolation from hostile code
    executing in this same Python interpreter.  Such code is outside the
    controller's trusted adapter model.
    """

    __slots__ = ("_evidence", "_token")

    def __init__(
        self,
        evidence: VerifiedInstallReceiptLedgerEvidence,
        token: object,
    ) -> None:
        if token is not _INSTALL_LEDGER_TOKEN:
            raise EmptyRollbackExecutionError(
                "empty_rollback_install_ledger_capability_invalid"
            )
        self._evidence = evidence
        self._token = token

    def __repr__(self) -> str:
        return "VerifiedInstallReceiptLedgerCapability(<content-redacted>)"


_RETAINED_ROLLBACK_EVIDENCE_TOKEN = object()


class _VerifiedRetainedRollbackEvidenceCapability:
    """Opaque preclaim proof of the durable receipt and anchored exact ledger."""

    __slots__ = ("_evidence", "_token")

    def __init__(
        self,
        evidence: VerifiedInstallReceiptLedgerEvidence,
        token: object,
    ) -> None:
        if token is not _RETAINED_ROLLBACK_EVIDENCE_TOKEN:
            raise EmptyRollbackExecutionError(
                "empty_rollback_retained_evidence_capability_invalid"
            )
        self._evidence = evidence
        self._token = token

    def __repr__(self) -> str:
        return "VerifiedRetainedRollbackEvidenceCapability(<content-redacted>)"


def _retained_evidence_material(
    evidence: VerifiedInstallReceiptLedgerEvidence,
) -> dict[str, object]:
    return {
        "canonical_receipt_sha256": evidence.canonical_receipt_sha256,
        "installation_execution_id": evidence.installation_execution_id,
        "installation_receipt_sha256": evidence.installation_receipt_sha256,
        "candidate_git_commit": evidence.candidate_git_commit,
        "candidate_git_tree": evidence.candidate_git_tree,
        "package_manifest_sha256": evidence.package_manifest_sha256,
        "controller_runtime_receipt_sha256": (
            evidence.controller_runtime_receipt_sha256
        ),
        "controller_runtime_tree_sha256": (
            evidence.controller_runtime_tree_sha256
        ),
        "controller_release_tree_sha256": (
            evidence.controller_release_tree_sha256
        ),
        "resource_ledger_binding_sha256": (
            evidence.resource_ledger_binding_sha256
        ),
        "resource_ledger_head_sha256": evidence.resource_ledger_head_sha256,
        "resource_ledger_sequence": evidence.resource_ledger_sequence,
        "exact_targets_sha256": evidence.exact_targets_sha256,
        "exact_rollback_resources_count": (
            evidence.exact_rollback_resources_count
        ),
        "recovery_reservation_claim_sha256": (
            evidence.recovery_reservation_claim_sha256
        ),
    }


def _retained_evidence_sha256(
    evidence: VerifiedInstallReceiptLedgerEvidence,
) -> str:
    return _hash_domain(
        _RETAINED_ROLLBACK_EVIDENCE_DOMAIN,
        _retained_evidence_material(evidence),
    )


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise EmptyRollbackExecutionError(
                "empty_rollback_install_receipt_invalid"
            )
        value[key] = item
    return value


def verify_retained_install_receipt_and_ledger(
    canonical_install_receipt: bytes,
    verified_resources: object,
) -> object:
    """Trusted-adapter boundary for retained install receipt and ledger proof."""

    if (
        type(canonical_install_receipt) is not bytes
        or not 1 <= len(canonical_install_receipt) <= MAX_RETAINED_INSTALL_RECEIPT_BYTES
    ):
        raise EmptyRollbackExecutionError(
            "empty_rollback_install_receipt_invalid"
        )
    try:
        document = json.loads(
            canonical_install_receipt.decode("ascii"),
            object_pairs_hook=_strict_json_object,
            parse_constant=lambda unused: (_ for _ in ()).throw(
                EmptyRollbackExecutionError(
                    "empty_rollback_install_receipt_invalid"
                )
            ),
        )
    except EmptyRollbackExecutionError:
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise EmptyRollbackExecutionError(
            "empty_rollback_install_receipt_invalid"
        ) from error
    if (
        type(document) is not dict
        or canonical_json_bytes(document) != canonical_install_receipt
    ):
        raise EmptyRollbackExecutionError(
            "empty_rollback_install_receipt_not_canonical"
        )
    try:
        receipt = verify_install_receipt(document)
        resource_evidence, resources = verified_rollback_resource_parts(
            verified_resources
        )
        targets_sha256 = exact_rollback_targets_sha256(
            verified_resources,
            package_manifest_sha256=resource_evidence.package_manifest_sha256,
        )
    except (ReceiptError, EmptyRollbackError) as error:
        raise EmptyRollbackExecutionError(
            "empty_rollback_install_ledger_evidence_invalid"
        ) from error
    if (
        receipt["package_manifest_sha256"]
        != resource_evidence.package_manifest_sha256
        or receipt["resource_ledger_head_sha256"]
        != resource_evidence.resource_ledger_head_sha256
        or receipt["resource_ledger_sequence"]
        != resource_evidence.resource_ledger_sequence
        or len(resources) != len(ROLLBACK_RESOURCE_KEYS)
        or targets_sha256 != resource_evidence.exact_targets_sha256
    ):
        raise EmptyRollbackExecutionError(
            "empty_rollback_install_ledger_evidence_mismatch"
        )
    preliminary = VerifiedInstallReceiptLedgerEvidence(
        canonical_receipt_sha256=hashlib.sha256(
            canonical_install_receipt
        ).hexdigest(),
        installation_execution_id=str(receipt["execution_id"]),
        installation_receipt_sha256=str(receipt["receipt_sha256"]),
        candidate_git_commit=str(receipt["candidate_git_commit"]),
        candidate_git_tree=str(receipt["candidate_git_tree"]),
        package_manifest_sha256=str(receipt["package_manifest_sha256"]),
        controller_runtime_receipt_sha256=str(
            receipt["controller_runtime_receipt_sha256"]
        ),
        controller_runtime_root=str(receipt["controller_runtime_root"]),
        controller_runtime_tree_sha256=str(
            receipt["controller_runtime_tree_sha256"]
        ),
        controller_release_root=str(receipt["controller_release_root"]),
        controller_release_tree_sha256=str(
            receipt["controller_release_tree_sha256"]
        ),
        controller_release_package_manifest_path=str(
            receipt["controller_release_package_manifest_path"]
        ),
        controller_runtime_interpreter_path=str(
            receipt["controller_runtime_interpreter_path"]
        ),
        controller_runtime_interpreter_sha256=str(
            receipt["controller_runtime_interpreter_sha256"]
        ),
        controller_runtime_inventory_path=str(
            receipt["controller_runtime_inventory_path"]
        ),
        controller_runtime_inventory_sha256=str(
            receipt["controller_runtime_inventory_sha256"]
        ),
        controller_requirements_lock_sha256=str(
            receipt["controller_requirements_lock_sha256"]
        ),
        supervisor_launcher_path=str(receipt["supervisor_launcher_path"]),
        supervisor_launcher_sha256=str(receipt["supervisor_launcher_sha256"]),
        resource_ledger_binding_sha256=(
            resource_evidence.resource_ledger_binding_sha256
        ),
        resource_ledger_head_sha256=(
            resource_evidence.resource_ledger_head_sha256
        ),
        resource_ledger_sequence=resource_evidence.resource_ledger_sequence,
        exact_targets_sha256=resource_evidence.exact_targets_sha256,
        exact_rollback_resources_count=len(resources),
        recovery_reservation_claim_sha256=ZERO_HEAD,
        retained_evidence_sha256="0" * 64,
    )
    evidence = replace(
        preliminary,
        retained_evidence_sha256=_retained_evidence_sha256(preliminary),
    )
    return _VerifiedInstallReceiptLedgerCapability(
        evidence,
        _INSTALL_LEDGER_TOKEN,
    )


def verify_retained_install_receipt_and_anchored_ledger(
    canonical_install_receipt: bytes,
    verified_resources: object,
    *,
    authority_state: AuthorityState,
    held_lock: HeldExecutionLockCapability,
    recovery_reservation_claim_sha256: str = ZERO_HEAD,
) -> object:
    """Mint preclaim evidence only after the exact durable ledger anchor agrees."""

    if type(authority_state) is not AuthorityState:
        raise EmptyRollbackExecutionError(
            "empty_rollback_retained_evidence_state_invalid"
        )
    try:
        validate_held_execution_lock(held_lock)
        if _HASH_RE.fullmatch(recovery_reservation_claim_sha256) is None:
            raise EmptyRollbackExecutionError(
                "empty_rollback_recovery_reservation_claim_invalid"
            )
        capability = verify_retained_install_receipt_and_ledger(
            canonical_install_receipt,
            verified_resources,
        )
        if (
            type(capability) is not _VerifiedInstallReceiptLedgerCapability
            or capability._token is not _INSTALL_LEDGER_TOKEN
            or type(capability._evidence)
            is not VerifiedInstallReceiptLedgerEvidence
        ):
            raise EmptyRollbackExecutionError(
                "empty_rollback_retained_evidence_capability_invalid"
            )
        evidence = replace(
            capability._evidence,
            recovery_reservation_claim_sha256=(
                recovery_reservation_claim_sha256
            ),
            retained_evidence_sha256=ZERO_HEAD,
        )
        evidence = replace(
            evidence,
            retained_evidence_sha256=_retained_evidence_sha256(evidence),
        )
        if (
            type(evidence) is not VerifiedInstallReceiptLedgerEvidence
            or evidence.exact_rollback_resources_count
            != len(ROLLBACK_RESOURCE_KEYS)
        ):
            raise EmptyRollbackExecutionError(
                "empty_rollback_retained_evidence_capability_invalid"
            )
        anchor = authority_state.read_resource_ledger_anchor(
            evidence.resource_ledger_binding_sha256,
            held_lock=held_lock,
        )
        validate_held_execution_lock(held_lock)
    except EmptyRollbackExecutionError:
        raise
    except (AuthorityStateError, ExecutionLockError) as error:
        raise EmptyRollbackExecutionError(
            "empty_rollback_resource_ledger_anchor_invalid"
        ) from error
    if (
        anchor.result != "resource_anchor_current"
        or anchor.sequence != evidence.resource_ledger_sequence
        or anchor.head_sha256 != evidence.resource_ledger_head_sha256
        or evidence.retained_evidence_sha256
        != _retained_evidence_sha256(evidence)
    ):
        raise EmptyRollbackExecutionError(
            "empty_rollback_resource_ledger_anchor_mismatch"
        )
    return _VerifiedRetainedRollbackEvidenceCapability(
        evidence,
        _RETAINED_ROLLBACK_EVIDENCE_TOKEN,
    )


def validate_install_receipt_ledger_capability(
    value: object,
    *,
    request: RollbackOperationRequest,
    expected_candidate_git_commit: str,
    expected_candidate_git_tree: str,
) -> VerifiedInstallReceiptLedgerEvidence:
    if (
        type(value) is not _VerifiedInstallReceiptLedgerCapability
        or value._token is not _INSTALL_LEDGER_TOKEN
        or type(value._evidence) is not VerifiedInstallReceiptLedgerEvidence
    ):
        raise EmptyRollbackExecutionError(
            "empty_rollback_install_ledger_capability_invalid"
        )
    evidence = replace(
        value._evidence,
        recovery_reservation_claim_sha256=(
            request.recovery_reservation_claim_sha256
        ),
        retained_evidence_sha256=ZERO_HEAD,
    )
    evidence = replace(
        evidence,
        retained_evidence_sha256=_retained_evidence_sha256(evidence),
    )
    if (
        _HASH_RE.fullmatch(evidence.canonical_receipt_sha256) is None
        or evidence.installation_execution_id
        != request.installation_execution_id
        or evidence.installation_receipt_sha256
        != request.installation_receipt_sha256
        or evidence.candidate_git_commit != expected_candidate_git_commit
        or evidence.candidate_git_tree != expected_candidate_git_tree
        or evidence.package_manifest_sha256 != request.package_manifest_sha256
        or evidence.controller_runtime_receipt_sha256
        != request.controller_runtime_receipt_sha256
        or evidence.controller_runtime_root != request.controller_runtime_root
        or evidence.controller_runtime_tree_sha256
        != request.controller_runtime_tree_sha256
        or evidence.controller_release_root != request.controller_release_root
        or evidence.controller_release_tree_sha256
        != request.controller_release_tree_sha256
        or evidence.controller_release_package_manifest_path
        != request.controller_release_package_manifest_path
        or evidence.controller_runtime_interpreter_path
        != request.controller_runtime_interpreter_path
        or evidence.controller_runtime_interpreter_sha256
        != request.controller_runtime_interpreter_sha256
        or evidence.controller_runtime_inventory_path
        != request.controller_runtime_inventory_path
        or evidence.controller_runtime_inventory_sha256
        != request.controller_runtime_inventory_sha256
        or evidence.controller_requirements_lock_sha256
        != request.controller_requirements_lock_sha256
        or evidence.supervisor_launcher_path != request.supervisor_launcher_path
        or evidence.supervisor_launcher_sha256
        != request.supervisor_launcher_sha256
        or evidence.resource_ledger_binding_sha256
        != request.resource_ledger_binding_sha256
        or evidence.resource_ledger_head_sha256
        != request.resource_ledger_head_sha256
        or evidence.resource_ledger_sequence
        != request.resource_ledger_sequence
        or evidence.exact_targets_sha256 != request.exact_targets_sha256
        or evidence.exact_rollback_resources_count
        != len(ROLLBACK_RESOURCE_KEYS)
        or _HASH_RE.fullmatch(
            evidence.recovery_reservation_claim_sha256
        )
        is None
        or evidence.retained_evidence_sha256
        != request.retained_evidence_sha256
        or evidence.retained_evidence_sha256
        != _retained_evidence_sha256(evidence)
    ):
        raise EmptyRollbackExecutionError(
            "empty_rollback_install_ledger_capability_mismatch"
        )
    return evidence


def validate_empty_rollback_controller_authority_marker_capability(
    value: object,
    *,
    expected_execution_id: str,
    expected_attempt_id: str,
    expected_plan_sha256: str,
    expected_eligibility_receipt_sha256: str,
    expected_exact_targets_sha256: str,
    expected_controller_runtime_tree_sha256: str,
    expected_controller_release_tree_sha256: str,
    expected_marker_bound_postgres_identity_sha256: str | None = None,
    expected_marker_bound_qdrant_identity_sha256: str | None = None,
    expected_controller_authority_marker_sha256: str | None = None,
) -> EmptyRollbackControllerAuthorityMarkerEvidence:
    """Validate the one controller-authority marker held across every destructive boundary."""

    if (
        type(value) is not _EmptyRollbackControllerAuthorityMarkerCapability
        or value._token is not _CONTROLLER_AUTHORITY_MARKER_TOKEN
        or type(value._evidence) is not EmptyRollbackControllerAuthorityMarkerEvidence
    ):
        raise EmptyRollbackExecutionError(
            "empty_rollback_controller_authority_marker_capability_invalid"
        )
    evidence = value._evidence
    if (
        evidence.execution_id != expected_execution_id
        or evidence.attempt_id != expected_attempt_id
        or evidence.plan_sha256 != expected_plan_sha256
        or evidence.signed_eligibility_receipt_sha256
        != expected_eligibility_receipt_sha256
        or evidence.exact_targets_sha256 != expected_exact_targets_sha256
        or evidence.controller_runtime_tree_sha256
        != expected_controller_runtime_tree_sha256
        or evidence.controller_release_tree_sha256
        != expected_controller_release_tree_sha256
        or _HASH_RE.fullmatch(
            evidence.marker_bound_postgres_identity_sha256
        ) is None
        or _HASH_RE.fullmatch(
            evidence.marker_bound_qdrant_identity_sha256
        ) is None
        or (
            expected_marker_bound_postgres_identity_sha256 is not None
            and evidence.marker_bound_postgres_identity_sha256
            != expected_marker_bound_postgres_identity_sha256
        )
        or (
            expected_marker_bound_qdrant_identity_sha256 is not None
            and evidence.marker_bound_qdrant_identity_sha256
            != expected_marker_bound_qdrant_identity_sha256
        )
        or _HASH_RE.fullmatch(evidence.controller_authority_marker_sha256) is None
        or (
            expected_controller_authority_marker_sha256 is not None
            and evidence.controller_authority_marker_sha256 != expected_controller_authority_marker_sha256
        )
    ):
        raise EmptyRollbackExecutionError(
            "empty_rollback_controller_authority_marker_binding_mismatch"
        )
    return evidence


class RollbackJournalAdapter(Protocol):
    plan_sha256: str
    attempt_id: str
    execution_id: str
    binding_sha256: str

    def records(self) -> tuple[RollbackJournalRecord, ...]: ...

    def reserve_effect_capacity(self, remaining_record_count: int) -> None: ...

    def append(self, record: RollbackJournalRecord) -> None: ...


class EmptyRollbackOperations(Protocol):
    def verify_install_receipt_and_ledger(
        self,
        request: RollbackOperationRequest,
    ) -> object: ...

    def observe_empty_eligibility(
        self,
        request: RollbackOperationRequest,
        controller_authority_marker_capability: object | None,
    ) -> Mapping[str, object]: ...

    def acquire_empty_rollback_controller_authority_marker(
        self,
        request: RollbackOperationRequest,
    ) -> EmptyRollbackControllerAuthorityMarkerAcquisition: ...

    def release_empty_rollback_controller_authority_marker(
        self,
        request: RollbackOperationRequest,
        controller_authority_marker_capability: object,
    ) -> None: ...

    def observe(self, request: RollbackOperationRequest) -> RollbackObservation: ...

    def apply_if_still_empty(
        self,
        request: RollbackOperationRequest,
        expected: RollbackObservation,
        controller_authority_marker_capability: object,
        signed_eligibility: Mapping[str, object],
    ) -> None: ...

    def exact_resources_absent(
        self,
        request: RollbackOperationRequest,
        resources: tuple[ExactRollbackResource, ...],
        controller_authority_marker_capability: object,
    ) -> bool: ...

    def observe_retained_audit_set(
        self,
        request: RollbackOperationRequest,
        retained_audit_keys: tuple[str, ...],
        controller_authority_marker_capability: object,
    ) -> Mapping[str, str]: ...


class RollbackJournalFactory(Protocol):
    def __call__(self, claimed_rollback: object) -> RollbackJournalAdapter: ...


def _production_rollback_journal(
    claimed_rollback: object,
    *,
    authority_state: AuthorityState,
    held_lock: HeldExecutionLockCapability,
) -> object:
    """Open/create only the claim-derived root-owned rollback journal."""

    try:
        validate_held_execution_lock(held_lock)
        claim = claimed_empty_rollback_evidence(claimed_rollback)
    except Exception as error:
        raise EmptyRollbackExecutionError(
            "empty_rollback_production_journal_invalid"
        ) from error
    execution_root = PRODUCTION_EXECUTIONS_ROOT / claim.execution_id
    journal_path = execution_root / "rollback.jsonl"
    canonical_template = str(
        PRODUCTION_EXECUTIONS_ROOT / "{execution_id}" / "rollback.jsonl"
    )
    if (
        ROLLBACK_JOURNAL_TEMPLATE != canonical_template
        or Path(claim.journal_path) != journal_path
        or authority_state.path != AUTHORITY_STATE_PATH
        or held_lock._owner.path != GLOBAL_LOCK_PATH
    ):
        raise EmptyRollbackExecutionError(
            "empty_rollback_production_journal_binding_invalid"
        )
    root_fd = execution_fd = -1
    exists = False
    created = False
    try:
        root_fd = _open_directory_nofollow(
            PRODUCTION_EXECUTIONS_ROOT,
            expected_uid=0,
        )
        root = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root.st_mode)
            or stat.S_IMODE(root.st_mode) != 0o700
            or root.st_uid != 0
            or root.st_gid != 0
        ):
            raise EmptyRollbackExecutionError(
                "empty_rollback_production_executions_root_invalid"
            )
        try:
            os.mkdir(claim.execution_id, 0o700, dir_fd=root_fd)
            created = True
        except FileExistsError:
            pass
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if nofollow == 0:
            raise EmptyRollbackExecutionError(
                "empty_rollback_production_nofollow_unavailable"
            )
        execution_fd = os.open(
            claim.execution_id,
            os.O_RDONLY
            | os.O_CLOEXEC
            | nofollow
            | getattr(os, "O_DIRECTORY", 0),
            dir_fd=root_fd,
        )
        opened = os.fstat(execution_fd)
        named = os.stat(
            claim.execution_id,
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
            raise EmptyRollbackExecutionError(
                "empty_rollback_production_execution_directory_invalid"
            )
        try:
            observed = os.stat(
                "rollback.jsonl",
                dir_fd=execution_fd,
                follow_symlinks=False,
            )
            exists = True
        except FileNotFoundError:
            observed = None
        if observed is not None and (
            not stat.S_ISREG(observed.st_mode)
            or stat.S_IMODE(observed.st_mode) != 0o600
            or observed.st_uid != 0
            or observed.st_gid != 0
            or observed.st_nlink != 1
        ):
            raise EmptyRollbackExecutionError(
                "empty_rollback_production_journal_file_invalid"
            )
        if created:
            os.fsync(execution_fd)
            os.fsync(root_fd)
        validate_held_execution_lock(held_lock)
    except EmptyRollbackExecutionError:
        raise
    except Exception as error:
        raise EmptyRollbackExecutionError(
            "empty_rollback_production_journal_invalid"
        ) from error
    finally:
        if execution_fd >= 0:
            os.close(execution_fd)
        if root_fd >= 0:
            os.close(root_fd)

    journal = None
    try:
        from .rollback_journal import DurableRollbackJournal

        journal = DurableRollbackJournal(
            journal_path,
            claimed_rollback=claimed_rollback,
            authority_state=authority_state,
            held_lock=held_lock,
            expected_uid=0,
            create=not exists,
        )
        opened = os.fstat(journal._fd)
        named = journal_path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_uid != 0
            or opened.st_gid != 0
            or opened.st_nlink != 1
            or named.st_uid != 0
            or named.st_gid != 0
            or (opened.st_dev, opened.st_ino)
            != (named.st_dev, named.st_ino)
        ):
            raise EmptyRollbackExecutionError(
                "empty_rollback_production_journal_file_invalid"
            )
        return journal
    except EmptyRollbackExecutionError:
        if journal is not None:
            journal.close()
        raise
    except Exception as error:
        if journal is not None:
            journal.close()
        raise EmptyRollbackExecutionError(
            "empty_rollback_production_journal_invalid"
        ) from error


@dataclass(frozen=True, slots=True)
class EmptyRollbackExecutionReceipt:
    outcome: str
    execution_id: str
    attempt_id: str
    plan_sha256: str
    authorization_sha256: str
    journal_sequence: int
    journal_head_sha256: str
    applied_step_ids: tuple[str, ...]
    canonical_receipt: Mapping[str, object]


def _parse_timestamp(value: str) -> datetime:
    if _TIMESTAMP_RE.fullmatch(value) is None:
        raise EmptyRollbackExecutionError("empty_rollback_authority_time_invalid")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise EmptyRollbackExecutionError(
            "empty_rollback_authority_time_invalid"
        ) from error


def _hash_domain(domain: bytes, value: object) -> str:
    return hashlib.sha256(domain + canonical_json_bytes(value)).hexdigest()


def _validate_retained_rollback_evidence_capability(
    value: object,
    *,
    plan: EmptyRollbackPlan,
    expected_resource_evidence: VerifiedRollbackResourceEvidence,
    expected_candidate_git_commit: str,
    expected_candidate_git_tree: str,
) -> VerifiedInstallReceiptLedgerEvidence:
    if (
        type(value) is not _VerifiedRetainedRollbackEvidenceCapability
        or value._token is not _RETAINED_ROLLBACK_EVIDENCE_TOKEN
        or type(value._evidence) is not VerifiedInstallReceiptLedgerEvidence
    ):
        raise EmptyRollbackExecutionError(
            "empty_rollback_retained_evidence_capability_invalid"
        )
    evidence = value._evidence
    if (
        evidence.installation_execution_id != plan.installation_execution_id
        or evidence.installation_receipt_sha256
        != plan.installation_receipt_sha256
        or evidence.candidate_git_commit != expected_candidate_git_commit
        or evidence.candidate_git_tree != expected_candidate_git_tree
        or evidence.package_manifest_sha256 != plan.package_manifest_sha256
        or evidence.resource_ledger_binding_sha256
        != expected_resource_evidence.resource_ledger_binding_sha256
        or evidence.resource_ledger_head_sha256
        != expected_resource_evidence.resource_ledger_head_sha256
        or evidence.resource_ledger_sequence
        != expected_resource_evidence.resource_ledger_sequence
        or evidence.exact_targets_sha256 != plan.exact_targets_sha256
        or evidence.exact_rollback_resources_count
        != len(ROLLBACK_RESOURCE_KEYS)
        or evidence.retained_evidence_sha256
        != _retained_evidence_sha256(evidence)
    ):
        raise EmptyRollbackExecutionError(
            "empty_rollback_retained_evidence_capability_mismatch"
        )
    return evidence


def _verify_recovery_reservation_claim(
    evidence: object,
    *,
    authority_state: AuthorityState,
    held_lock: HeldExecutionLockCapability,
) -> str:
    reservation = getattr(evidence, "recovery_reservation", None)
    if reservation is None:
        return ZERO_HEAD
    rollback_nonce_sha256 = nonce_sha256(str(getattr(evidence, "nonce", "")))
    installation_nonce_sha256 = str(
        getattr(evidence, "installation_nonce_sha256", "")
    )
    if (
        reservation.operation != RECOVERY_RESERVATION_OPERATION
        or reservation.installation_execution_id
        != getattr(evidence, "installation_execution_id", None)
        or reservation.not_before != getattr(evidence, "not_before", None)
        or reservation.expires_at != getattr(evidence, "expires_at", None)
        or nonce_sha256(reservation.nonce)
        in {rollback_nonce_sha256, installation_nonce_sha256}
    ):
        raise EmptyRollbackExecutionError(
            "empty_rollback_recovery_reservation_invalid"
        )
    try:
        reservation_claim = authority_state.claim_nonce(
            reservation.nonce,
            operation=reservation.operation,
            execution_sha256=reservation.execution_sha256,
            authorization_sha256=reservation.authorization_sha256,
            scope_sha256=reservation.scope_sha256,
            trust_bundle_sha256=reservation.trust_bundle_sha256,
            held_lock=held_lock,
            allow_new_claim=False,
        )
        validate_held_execution_lock(held_lock)
    except AuthorityClaimNotAllowedError as error:
        raise EmptyRollbackExecutionError(
            "empty_rollback_recovery_reservation_absent"
        ) from error
    except (AuthorityStateError, ExecutionLockError) as error:
        raise EmptyRollbackExecutionError(
            "empty_rollback_recovery_reservation_invalid"
        ) from error
    if reservation_claim.result != "exact_execution_resumed":
        raise EmptyRollbackExecutionError(
            "empty_rollback_recovery_reservation_invalid"
        )
    return reservation_claim.claim_sha256


def _claim_empty_rollback(
    capability: object,
    plan: EmptyRollbackPlan,
    *,
    retained_evidence_capability: object,
    expected_resource_evidence: VerifiedRollbackResourceEvidence,
    authority_mode: RollbackAuthorityMode,
    authority_state: AuthorityState,
    clock: TrustedUtcClock,
    held_lock: HeldExecutionLockCapability,
) -> object:
    try:
        evidence = rollback_capability_evidence(capability)
        validate_held_execution_lock(held_lock)
    except (RollbackAuthorityError, ExecutionLockError) as error:
        raise EmptyRollbackExecutionError(
            "empty_rollback_execution_authority_invalid"
        ) from error
    if (
        type(authority_state) is not AuthorityState
        or authority_state.path != AUTHORITY_STATE_PATH
        or held_lock._owner.path != GLOBAL_LOCK_PATH
        or type(authority_mode) is not RollbackAuthorityMode
        or evidence.operation != ROLLBACK_OPERATION
        or evidence.rollback_plan_sha256 != plan.plan_sha256
        or evidence.package_manifest_sha256 != plan.package_manifest_sha256
    ):
        raise EmptyRollbackExecutionError(
            "empty_rollback_execution_authority_invalid"
        )
    retained_evidence = _validate_retained_rollback_evidence_capability(
        retained_evidence_capability,
        plan=plan,
        expected_resource_evidence=expected_resource_evidence,
        expected_candidate_git_commit=evidence.candidate_git_commit,
        expected_candidate_git_tree=evidence.candidate_git_tree,
    )
    rollback_nonce_sha256 = nonce_sha256(evidence.nonce)
    if rollback_nonce_sha256 == evidence.installation_nonce_sha256:
        raise EmptyRollbackExecutionError(
            "empty_rollback_authority_nonce_collision"
        )
    reading = read_trusted_utc(clock)
    not_before = _parse_timestamp(evidence.not_before)
    expires_at = _parse_timestamp(evidence.expires_at)
    if reading.observed_at < not_before:
        raise EmptyRollbackExecutionError("empty_rollback_authority_not_yet_valid")
    reservation = evidence.recovery_reservation
    reservation_claim_sha256 = _verify_recovery_reservation_claim(
        evidence,
        authority_state=authority_state,
        held_lock=held_lock,
    )
    if retained_evidence.recovery_reservation_claim_sha256 != (
        reservation_claim_sha256
    ):
        raise EmptyRollbackExecutionError(
            "empty_rollback_recovery_reservation_claim_mismatch"
        )
    reserved_recovery = reservation_claim_sha256 != ZERO_HEAD
    if (
        authority_mode is RollbackAuthorityMode.START_RESERVED_ROLLBACK
        and not reserved_recovery
    ):
        raise EmptyRollbackExecutionError(
            "empty_rollback_recovery_reservation_required"
        )
    if (
        authority_mode is RollbackAuthorityMode.START_ROLLBACK
        and reservation is not None
    ):
        raise EmptyRollbackExecutionError(
            "empty_rollback_reserved_authority_mode_required"
        )
    runtime_binding = {
        "package_manifest_sha256": evidence.package_manifest_sha256,
        "installation_execution_id": evidence.installation_execution_id,
        "controller_runtime_receipt_sha256": (
            evidence.controller_runtime_receipt_sha256
        ),
        "controller_runtime_root": evidence.controller_runtime_root,
        "controller_runtime_tree_sha256": (
            evidence.controller_runtime_tree_sha256
        ),
        "controller_release_root": evidence.controller_release_root,
        "controller_release_tree_sha256": (
            evidence.controller_release_tree_sha256
        ),
        "controller_release_package_manifest_path": (
            evidence.controller_release_package_manifest_path
        ),
        "controller_runtime_interpreter_path": (
            evidence.controller_runtime_interpreter_path
        ),
        "controller_runtime_interpreter_sha256": (
            evidence.controller_runtime_interpreter_sha256
        ),
        "controller_runtime_inventory_path": (
            evidence.controller_runtime_inventory_path
        ),
        "controller_runtime_inventory_sha256": (
            evidence.controller_runtime_inventory_sha256
        ),
        "controller_requirements_lock_sha256": (
            evidence.controller_requirements_lock_sha256
        ),
        "supervisor_launcher_path": evidence.supervisor_launcher_path,
        "supervisor_launcher_sha256": evidence.supervisor_launcher_sha256,
    }
    execution_sha = _hash_domain(
        _EXECUTION_DOMAIN,
        {
            "operation": evidence.operation,
            "scope_sha256": evidence.scope_sha256,
            "authorization_sha256": evidence.authorization_sha256,
            "trust_bundle_sha256": evidence.trust_bundle_sha256,
            "plan_sha256": plan.plan_sha256,
            "retained_evidence_sha256": (
                retained_evidence.retained_evidence_sha256
            ),
            **runtime_binding,
        },
    )
    try:
        claim = authority_state.claim_nonce(
            evidence.nonce,
            operation=ROLLBACK_OPERATION,
            execution_sha256=execution_sha,
            authorization_sha256=evidence.authorization_sha256,
            scope_sha256=evidence.scope_sha256,
            trust_bundle_sha256=evidence.trust_bundle_sha256,
            held_lock=held_lock,
            allow_new_claim=(
                authority_mode
                is not RollbackAuthorityMode.RESUME_ROLLBACK
                and (
                    reading.observed_at < expires_at
                    or (
                        authority_mode
                        is RollbackAuthorityMode.START_RESERVED_ROLLBACK
                        and reserved_recovery
                    )
                )
            ),
        )
        validate_held_execution_lock(held_lock)
    except AuthorityClaimNotAllowedError as error:
        code = (
            "empty_rollback_resume_claim_absent"
            if authority_mode is RollbackAuthorityMode.RESUME_ROLLBACK
            else "empty_rollback_authority_expired"
        )
        raise EmptyRollbackExecutionError(code) from error
    except (AuthorityStateError, ExecutionLockError) as error:
        raise EmptyRollbackExecutionError("empty_rollback_nonce_claim_failed") from error
    if (
        authority_mode is RollbackAuthorityMode.RESUME_ROLLBACK
        and claim.result != "exact_execution_resumed"
    ):
        raise EmptyRollbackExecutionError("empty_rollback_resume_claim_invalid")
    if (
        authority_mode
        in {
            RollbackAuthorityMode.START_ROLLBACK,
            RollbackAuthorityMode.START_RESERVED_ROLLBACK,
        }
        and claim.result != "nonce_claimed"
    ):
        raise EmptyRollbackExecutionError("empty_rollback_start_already_claimed")
    execution_id = execution_sha
    attempt_digest = _hash_domain(
        _ATTEMPT_DOMAIN,
        {"claim_sha256": claim.claim_sha256, "execution_id": execution_id},
    )
    attempt_id = "rollback-" + attempt_digest[:40]
    journal_path = ROLLBACK_JOURNAL_TEMPLATE.replace(
        "{execution_id}", execution_id
    )
    binding_sha = _hash_domain(
        _JOURNAL_DOMAIN,
        {
            "claim_sha256": claim.claim_sha256,
            "execution_id": execution_id,
            "attempt_id": attempt_id,
            "plan_sha256": plan.plan_sha256,
            "journal_path": journal_path,
            "retained_evidence_sha256": (
                retained_evidence.retained_evidence_sha256
            ),
            **runtime_binding,
        },
    )
    claimed = ClaimedEmptyRollbackEvidence(
        schema_version=ROLLBACK_CLAIM_SCHEMA,
        operation=ROLLBACK_OPERATION,
        claim_result=claim.result,
        execution_id=execution_id,
        attempt_id=attempt_id,
        claim_sha256=claim.claim_sha256,
        authorization_sha256=evidence.authorization_sha256,
        scope_sha256=evidence.scope_sha256,
        plan_sha256=plan.plan_sha256,
        package_manifest_sha256=evidence.package_manifest_sha256,
        controller_runtime_receipt_sha256=(
            evidence.controller_runtime_receipt_sha256
        ),
        controller_runtime_root=evidence.controller_runtime_root,
        controller_runtime_tree_sha256=evidence.controller_runtime_tree_sha256,
        controller_release_root=evidence.controller_release_root,
        controller_release_tree_sha256=evidence.controller_release_tree_sha256,
        controller_release_package_manifest_path=(
            evidence.controller_release_package_manifest_path
        ),
        controller_runtime_interpreter_path=(
            evidence.controller_runtime_interpreter_path
        ),
        controller_runtime_interpreter_sha256=(
            evidence.controller_runtime_interpreter_sha256
        ),
        controller_runtime_inventory_path=evidence.controller_runtime_inventory_path,
        controller_runtime_inventory_sha256=(
            evidence.controller_runtime_inventory_sha256
        ),
        controller_requirements_lock_sha256=(
            evidence.controller_requirements_lock_sha256
        ),
        supervisor_launcher_path=evidence.supervisor_launcher_path,
        supervisor_launcher_sha256=evidence.supervisor_launcher_sha256,
        installation_execution_id=evidence.installation_execution_id,
        recovery_reservation_claim_sha256=(
            reservation_claim_sha256
        ),
        retained_evidence_sha256=(
            retained_evidence.retained_evidence_sha256
        ),
        journal_binding_sha256=binding_sha,
        journal_path=journal_path,
        authority_state_path_sha256=hashlib.sha256(
            str(authority_state.path).encode("utf-8")
        ).hexdigest(),
        global_lock_path_sha256=hashlib.sha256(
            str(held_lock._owner.path).encode("utf-8")
        ).hexdigest(),
    )
    return _ClaimedEmptyRollback(claimed, _CLAIM_TOKEN)


def _equivalent_empty_state(
    signed: Mapping[str, object], observed: Mapping[str, object]
) -> None:
    signed_receipt = verify_empty_rollback_eligibility_receipt(signed)
    observed_receipt = verify_empty_rollback_eligibility_receipt(observed)
    ignored = {"observation_set_sha256", "receipt_sha256"}
    if {
        key: value for key, value in signed_receipt.items() if key not in ignored
    } != {
        key: value for key, value in observed_receipt.items() if key not in ignored
    }:
        raise EmptyRollbackExecutionError(
            "empty_rollback_eligibility_recheck_mismatch"
        )


class _EmptyRollbackController:
    def __init__(
        self,
        *,
        plan: EmptyRollbackPlan,
        signed_eligibility: Mapping[str, object],
        expected_resource_evidence: VerifiedRollbackResourceEvidence,
        claimed: object,
        journal: RollbackJournalAdapter,
        operations: EmptyRollbackOperations,
        authority_state: AuthorityState,
        held_lock: HeldExecutionLockCapability,
        receipt_store: DurableReceiptStore,
    ) -> None:
        claim = claimed_empty_rollback_evidence(claimed)
        if (
            journal.plan_sha256 != plan.plan_sha256
            or journal.attempt_id != claim.attempt_id
            or journal.execution_id != claim.execution_id
            or journal.binding_sha256 != claim.journal_binding_sha256
            or verify_built_empty_rollback_plan(plan) is not plan
            or type(expected_resource_evidence)
            is not VerifiedRollbackResourceEvidence
            or expected_resource_evidence.package_manifest_sha256
            != plan.package_manifest_sha256
            or expected_resource_evidence.resource_ledger_head_sha256
            != plan.resource_ledger_head_sha256
            or expected_resource_evidence.exact_targets_sha256
            != plan.exact_targets_sha256
            or expected_resource_evidence.resource_ledger_sequence
            < len(plan.resources)
            or not all(
                callable(getattr(journal, name, None))
                for name in ("records", "reserve_effect_capacity", "append")
            )
            or type(receipt_store) is not DurableReceiptStore
            or type(authority_state) is not AuthorityState
            or not all(
                callable(getattr(operations, name, None))
                for name in (
                    "verify_install_receipt_and_ledger",
                    "observe_empty_eligibility",
                    "acquire_empty_rollback_controller_authority_marker",
                    "release_empty_rollback_controller_authority_marker",
                    "observe",
                    "apply_if_still_empty",
                    "exact_resources_absent",
                    "observe_retained_audit_set",
                )
            )
        ):
            raise EmptyRollbackExecutionError(
                "empty_rollback_controller_dependency_invalid"
            )
        self.plan = plan
        self.resources = plan.resources
        self.expected_resource_evidence = expected_resource_evidence
        self.signed_eligibility = signed_eligibility
        self.claim = claim
        self.journal = journal
        self.operations = operations
        self.authority_state = authority_state
        self.held_lock = held_lock
        self.receipt_store = receipt_store
        self.install_ledger_evidence: (
            VerifiedInstallReceiptLedgerEvidence | None
        ) = None
        self.controller_authority_marker_capability: object | None = None
        self.retained_audit_set_sha256: str | None = None
        self.resource_by_key = {
            item.resource_key: item for item in self.resources
        }

    def _require_lock(self) -> None:
        try:
            validate_held_execution_lock(self.held_lock)
        except ExecutionLockError as error:
            raise EmptyRollbackExecutionError(
                "empty_rollback_global_lock_not_held"
            ) from error

    def _records(self) -> tuple[RollbackJournalRecord, ...]:
        self._require_lock()
        return validate_rollback_records(
            self.journal.records(),
            plan_sha256=self.plan.plan_sha256,
            attempt_id=self.claim.attempt_id,
        )

    def _append(self, step_id: str, event: RollbackEvent) -> None:
        before = self._records()
        desired = RollbackJournalRecord.create(
            plan_sha256=self.plan.plan_sha256,
            attempt_id=self.claim.attempt_id,
            sequence=len(before) + 1,
            step_id=step_id,
            event=event,
            prior_record_sha256=(
                before[-1].record_sha256 if before else ZERO_HEAD
            ),
        )
        try:
            self.journal.append(desired)
        except Exception as error:
            after = self._records()
            if len(after) == len(before) + 1 and after[-1] == desired:
                return
            raise EmptyRollbackExecutionError(
                "empty_rollback_journal_append_failed"
            ) from error
        after = self._records()
        if len(after) != len(before) + 1 or after[-1] != desired:
            raise EmptyRollbackExecutionError(
                "empty_rollback_journal_append_unconfirmed"
            )

    def _request(self, step: EmptyRollbackPlanStep) -> RollbackOperationRequest:
        resource = (
            None
            if step.resource_key is None
            else self.resource_by_key[step.resource_key]
        )
        return RollbackOperationRequest(
            step=step,
            resource=resource,
            execution_id=self.claim.execution_id,
            attempt_id=self.claim.attempt_id,
            journal_binding_sha256=self.claim.journal_binding_sha256,
            plan_sha256=self.plan.plan_sha256,
            package_manifest_sha256=self.plan.package_manifest_sha256,
            controller_runtime_receipt_sha256=(
                self.claim.controller_runtime_receipt_sha256
            ),
            controller_runtime_root=self.claim.controller_runtime_root,
            controller_runtime_tree_sha256=(
                self.claim.controller_runtime_tree_sha256
            ),
            controller_release_root=self.claim.controller_release_root,
            controller_release_tree_sha256=(
                self.claim.controller_release_tree_sha256
            ),
            controller_release_package_manifest_path=(
                self.claim.controller_release_package_manifest_path
            ),
            controller_runtime_interpreter_path=(
                self.claim.controller_runtime_interpreter_path
            ),
            controller_runtime_interpreter_sha256=(
                self.claim.controller_runtime_interpreter_sha256
            ),
            controller_runtime_inventory_path=(
                self.claim.controller_runtime_inventory_path
            ),
            controller_runtime_inventory_sha256=(
                self.claim.controller_runtime_inventory_sha256
            ),
            controller_requirements_lock_sha256=(
                self.claim.controller_requirements_lock_sha256
            ),
            supervisor_launcher_path=self.claim.supervisor_launcher_path,
            supervisor_launcher_sha256=self.claim.supervisor_launcher_sha256,
            installation_execution_id=self.plan.installation_execution_id,
            installation_receipt_sha256=self.plan.installation_receipt_sha256,
            recovery_reservation_claim_sha256=(
                self.claim.recovery_reservation_claim_sha256
            ),
            retained_evidence_sha256=self.claim.retained_evidence_sha256,
            eligibility_receipt_sha256=self.plan.eligibility_receipt_sha256,
            resource_ledger_binding_sha256=(
                self.expected_resource_evidence.resource_ledger_binding_sha256
            ),
            resource_ledger_head_sha256=self.plan.resource_ledger_head_sha256,
            resource_ledger_sequence=(
                self.expected_resource_evidence.resource_ledger_sequence
            ),
            exact_targets_sha256=self.plan.exact_targets_sha256,
        )

    def _validate_controller_authority_marker(self) -> EmptyRollbackControllerAuthorityMarkerEvidence:
        return validate_empty_rollback_controller_authority_marker_capability(
            self.controller_authority_marker_capability,
            expected_execution_id=self.claim.execution_id,
            expected_attempt_id=self.claim.attempt_id,
            expected_plan_sha256=self.plan.plan_sha256,
            expected_eligibility_receipt_sha256=(
                self.plan.eligibility_receipt_sha256
            ),
            expected_exact_targets_sha256=self.plan.exact_targets_sha256,
            expected_controller_runtime_tree_sha256=(
                self.claim.controller_runtime_tree_sha256
            ),
            expected_controller_release_tree_sha256=(
                self.claim.controller_release_tree_sha256
            ),
        )

    def _acquire_controller_authority_marker(self) -> None:
        if self.controller_authority_marker_capability is not None:
            raise EmptyRollbackExecutionError(
                "empty_rollback_controller_authority_marker_already_held"
            )
        request = self._request(self.plan.steps[3])
        acquisition = self.operations.acquire_empty_rollback_controller_authority_marker(request)
        if (
            type(acquisition) is not EmptyRollbackControllerAuthorityMarkerAcquisition
            or acquisition.execution_id != self.claim.execution_id
            or acquisition.attempt_id != self.claim.attempt_id
            or acquisition.plan_sha256 != self.plan.plan_sha256
            or acquisition.exact_targets_sha256 != self.plan.exact_targets_sha256
            or acquisition.controller_runtime_tree_sha256
            != self.claim.controller_runtime_tree_sha256
            or acquisition.controller_release_tree_sha256
            != self.claim.controller_release_tree_sha256
        ):
            raise EmptyRollbackExecutionError(
                "empty_rollback_controller_authority_marker_acquisition_invalid"
            )
        self.controller_authority_marker_capability = _EmptyRollbackControllerAuthorityMarkerCapability(
            EmptyRollbackControllerAuthorityMarkerEvidence(
                execution_id=self.claim.execution_id,
                attempt_id=self.claim.attempt_id,
                plan_sha256=self.plan.plan_sha256,
                signed_eligibility_receipt_sha256=(
                    self.plan.eligibility_receipt_sha256
                ),
                exact_targets_sha256=self.plan.exact_targets_sha256,
                controller_runtime_tree_sha256=(
                    self.claim.controller_runtime_tree_sha256
                ),
                controller_release_tree_sha256=(
                    self.claim.controller_release_tree_sha256
                ),
                marker_bound_postgres_identity_sha256=(
                    acquisition.marker_bound_postgres_identity_sha256
                ),
                marker_bound_qdrant_identity_sha256=(
                    acquisition.marker_bound_qdrant_identity_sha256
                ),
                controller_authority_marker_sha256=acquisition.controller_authority_marker_sha256,
            ),
            _CONTROLLER_AUTHORITY_MARKER_TOKEN,
        )
        self._validate_controller_authority_marker()

    def _release_controller_authority_marker(self) -> None:
        if self.controller_authority_marker_capability is None:
            return
        self._require_lock()
        self._validate_controller_authority_marker()
        request = self._request(self.plan.steps[3])
        try:
            self.operations.release_empty_rollback_controller_authority_marker(
                request,
                self.controller_authority_marker_capability,
            )
        except Exception as error:
            raise EmptyRollbackExecutionError(
                "empty_rollback_controller_authority_marker_release_failed"
            ) from error
        self.controller_authority_marker_capability = None
        self._require_lock()

    @staticmethod
    def _expected_ownership(
        request: RollbackOperationRequest, revision_sha256: str
    ) -> str:
        return hashlib.sha256(
            canonical_json_bytes(
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
                    "package_manifest_sha256": (
                        request.package_manifest_sha256
                    ),
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
                    "eligibility_receipt_sha256": request.eligibility_receipt_sha256,
                    "resource_ledger_binding_sha256": (
                        request.resource_ledger_binding_sha256
                    ),
                    "resource_ledger_head_sha256": (
                        request.resource_ledger_head_sha256
                    ),
                    "resource_ledger_sequence": (
                        request.resource_ledger_sequence
                    ),
                    "exact_targets_sha256": request.exact_targets_sha256,
                    "revision_sha256": revision_sha256,
                }
            )
        ).hexdigest()

    def _perform(self, step: EmptyRollbackPlanStep, *, recovering: bool) -> None:
        request = self._request(step)
        if step.step_id in {"R01_REVERIFY_GLOBAL_LOCK", "R02_VERIFY_CLAIMED_ROLLBACK_AUTHORITY"}:
            self._require_lock()
            return
        if step.step_id == "R03_VERIFY_INSTALL_RECEIPT_AND_LEDGER":
            capability = self.operations.verify_install_receipt_and_ledger(
                request
            )
            self.install_ledger_evidence = (
                validate_install_receipt_ledger_capability(
                    capability,
                    request=request,
                    expected_candidate_git_commit=(
                        self.plan.candidate_git_commit
                    ),
                    expected_candidate_git_tree=self.plan.candidate_git_tree,
                )
            )
            self._require_current_resource_ledger_anchor()
            return
        if step.step_id == "R04_ACQUIRE_ROLLBACK_CONTROLLER_AUTHORITY_MARKER":
            self._acquire_controller_authority_marker()
            return
        if step.step_id == (
            "R06_ESTABLISH_ADMINISTRATIVE_WRITER_FENCE_AND_RECHECK_SEMANTIC_EMPTY"
        ):
            self._establish_administrative_writer_fence_and_recheck_empty()
            return
        if step.operation == "verify_exact_absence_and_retain_audit":
            self._validate_controller_authority_marker()
            if self.operations.exact_resources_absent(
                request,
                self.resources,
                self.controller_authority_marker_capability,
            ) is not True:
                raise EmptyRollbackExecutionError(
                    "empty_rollback_resource_absence_unproved"
                )
            return
        try:
            observed = self.operations.observe(request)
        except Exception as error:
            raise EmptyRollbackExecutionError(
                "empty_rollback_observation_failed"
            ) from error
        if (
            type(observed) is not RollbackObservation
            or observed.ownership_sha256
            != self._expected_ownership(request, observed.revision_sha256)
            or observed.controller_runtime_tree_sha256
            != request.controller_runtime_tree_sha256
            or observed.controller_release_tree_sha256
            != request.controller_release_tree_sha256
            or observed.state == "drift"
        ):
            raise EmptyRollbackExecutionError(
                "empty_rollback_resource_drift"
            )
        if observed.state == "after":
            if step.invariant_only:
                self._validate_controller_authority_marker()
                self.operations.apply_if_still_empty(
                    request,
                    observed,
                    self.controller_authority_marker_capability,
                    self.signed_eligibility,
                )
                return
            if recovering:
                self._validate_controller_authority_marker()
                self.operations.apply_if_still_empty(
                    request,
                    observed,
                    self.controller_authority_marker_capability,
                    self.signed_eligibility,
                )
                return
            raise EmptyRollbackExecutionError(
                "empty_rollback_unowned_prior_effect"
            )
        if observed.state not in {"before", "recoverable"}:
            raise EmptyRollbackExecutionError(
                "empty_rollback_resource_state_invalid"
            )
        try:
            self._require_lock()
            self._validate_controller_authority_marker()
            self.operations.apply_if_still_empty(
                request,
                observed,
                self.controller_authority_marker_capability,
                self.signed_eligibility,
            )
            self._require_lock()
        except Exception as error:
            raise EmptyRollbackExecutionError(
                "empty_rollback_effect_failed"
            ) from error

    def _verify_install_binding_for_receipt(self) -> None:
        request = self._request(self.plan.steps[2])
        capability = self.operations.verify_install_receipt_and_ledger(
            request
        )
        self.install_ledger_evidence = validate_install_receipt_ledger_capability(
            capability,
            request=request,
            expected_candidate_git_commit=self.plan.candidate_git_commit,
            expected_candidate_git_tree=self.plan.candidate_git_tree,
        )
        self._require_current_resource_ledger_anchor()

    def _require_current_resource_ledger_anchor(self) -> None:
        self._require_lock()
        try:
            anchor = self.authority_state.read_resource_ledger_anchor(
                self.expected_resource_evidence.resource_ledger_binding_sha256,
                held_lock=self.held_lock,
            )
        except AuthorityStateError as error:
            raise EmptyRollbackExecutionError(
                "empty_rollback_resource_ledger_anchor_invalid"
            ) from error
        self._require_lock()
        if (
            anchor.result != "resource_anchor_current"
            or anchor.sequence
            != self.expected_resource_evidence.resource_ledger_sequence
            or anchor.head_sha256
            != self.expected_resource_evidence.resource_ledger_head_sha256
        ):
            raise EmptyRollbackExecutionError(
                "empty_rollback_resource_ledger_anchor_mismatch"
            )

    def _require_current_install_binding(self) -> None:
        self._require_lock()
        self._verify_install_binding_for_receipt()
        self._require_lock()
        if self.install_ledger_evidence is None:
            raise EmptyRollbackExecutionError(
                "empty_rollback_install_ledger_evidence_invalid"
            )

    def _execution_receipt(
        self,
        *,
        outcome: str,
        records: tuple[RollbackJournalRecord, ...],
    ) -> EmptyRollbackExecutionReceipt:
        evidence = self.install_ledger_evidence
        if (
            evidence is None
            or self.retained_audit_set_sha256 is None
            or not records
            or records[-1].event != RollbackEvent.COMPLETE.value
        ):
            raise EmptyRollbackExecutionError(
                "empty_rollback_receipt_evidence_invalid"
            )
        canonical = build_empty_rollback_receipt(
            execution_id=self.claim.execution_id,
            attempt_id=self.claim.attempt_id,
            candidate_git_commit=self.plan.candidate_git_commit,
            candidate_git_tree=self.plan.candidate_git_tree,
            package_manifest_sha256=self.plan.package_manifest_sha256,
            controller_runtime_receipt_sha256=(
                self.claim.controller_runtime_receipt_sha256
            ),
            controller_runtime_root=self.claim.controller_runtime_root,
            controller_runtime_tree_sha256=(
                self.claim.controller_runtime_tree_sha256
            ),
            controller_release_root=self.claim.controller_release_root,
            controller_release_tree_sha256=(
                self.claim.controller_release_tree_sha256
            ),
            controller_release_package_manifest_path=(
                self.claim.controller_release_package_manifest_path
            ),
            controller_runtime_interpreter_path=(
                self.claim.controller_runtime_interpreter_path
            ),
            controller_runtime_interpreter_sha256=(
                self.claim.controller_runtime_interpreter_sha256
            ),
            controller_runtime_inventory_path=(
                self.claim.controller_runtime_inventory_path
            ),
            controller_runtime_inventory_sha256=(
                self.claim.controller_runtime_inventory_sha256
            ),
            controller_requirements_lock_sha256=(
                self.claim.controller_requirements_lock_sha256
            ),
            supervisor_launcher_path=self.claim.supervisor_launcher_path,
            supervisor_launcher_sha256=self.claim.supervisor_launcher_sha256,
            installation_receipt_sha256=self.plan.installation_receipt_sha256,
            authorization_sha256=self.claim.authorization_sha256,
            plan_sha256=self.plan.plan_sha256,
            journal_head_sha256=records[-1].record_sha256,
            journal_sequence=len(records),
            resource_ledger_head_sha256=self.plan.resource_ledger_head_sha256,
            resource_ledger_sequence=evidence.resource_ledger_sequence,
            eligibility_receipt_sha256=self.plan.eligibility_receipt_sha256,
            retained_audit_set_sha256=self.retained_audit_set_sha256,
            exact_rollback_resources_absent_count=len(self.resources),
        )
        try:
            durable = self.receipt_store.write_once(
                ReceiptArtifact.EMPTY_ROLLBACK,
                self.claim.execution_id,
                canonical,
            )
        except DurableReceiptError as error:
            raise EmptyRollbackExecutionError(
                "empty_rollback_durable_receipt_persistence_failed"
            ) from error
        if (
            durable.receipt_sha256 != canonical["receipt_sha256"]
            or dict(durable.canonical_receipt) != canonical
        ):
            raise EmptyRollbackExecutionError(
                "empty_rollback_durable_receipt_unconfirmed"
            )
        return EmptyRollbackExecutionReceipt(
            outcome=outcome,
            execution_id=self.claim.execution_id,
            attempt_id=self.claim.attempt_id,
            plan_sha256=self.plan.plan_sha256,
            authorization_sha256=self.claim.authorization_sha256,
            journal_sequence=len(records),
            journal_head_sha256=records[-1].record_sha256,
            applied_step_ids=tuple(step.step_id for step in self.plan.steps),
            canonical_receipt=MappingProxyType(
                dict(durable.canonical_receipt)
            ),
        )

    def _verify_terminal_state_and_evidence(self) -> None:
        """Reverify all terminal evidence after COMPLETE while the controller-authority marker is held."""

        self._validate_controller_authority_marker()
        final_request = self._request(self.plan.steps[-1])
        if self.operations.exact_resources_absent(
            final_request,
            self.resources,
            self.controller_authority_marker_capability,
        ) is not True:
            raise EmptyRollbackExecutionError(
                "empty_rollback_completed_state_drift"
            )
        self._require_current_install_binding()
        evidence = self.install_ledger_evidence
        if evidence is None:
            raise EmptyRollbackExecutionError(
                "empty_rollback_install_ledger_evidence_invalid"
            )
        try:
            observed = self.operations.observe_retained_audit_set(
                final_request,
                self.plan.retained_audit_keys,
                self.controller_authority_marker_capability,
            )
        except Exception as error:
            raise EmptyRollbackExecutionError(
                "empty_rollback_retained_audit_observation_failed"
            ) from error
        if (
            type(observed) is not dict
            or set(observed) != set(self.plan.retained_audit_keys)
            or any(
                type(key) is not str
                or type(value) is not str
                or _HASH_RE.fullmatch(value) is None
                for key, value in observed.items()
            )
            or observed["installation_receipt"]
            != evidence.canonical_receipt_sha256
            or observed["eligibility_receipt"]
            != hashlib.sha256(
                canonical_json_bytes(self.signed_eligibility)
            ).hexdigest()
        ):
            raise EmptyRollbackExecutionError(
                "empty_rollback_retained_audit_evidence_invalid"
            )
        self.retained_audit_set_sha256 = _hash_domain(
            _RETAINED_AUDIT_SET_DOMAIN,
            {
                "retained_audit_evidence": {
                    key: observed[key]
                    for key in sorted(observed)
                },
            },
        )
        self._require_lock()

    def _establish_administrative_writer_fence_and_recheck_empty(
        self,
    ) -> None:
        """Bind emptiness only after the exact store supervisor is absent.

        This excludes cooperating and unprivileged runtime writers through the
        held global lock, absent application services, zero live clients, and
        root-controlled fresh credentials.  It is an administrative trust
        boundary and does not claim to exclude an equivalently privileged root
        administrator.
        """

        self._validate_controller_authority_marker()
        request = self._request(self.plan.steps[5])
        observed = self.operations.observe_empty_eligibility(
            request,
            self.controller_authority_marker_capability,
        )
        _equivalent_empty_state(self.signed_eligibility, observed)

    def _run_while_managing_controller_authority_marker(
        self,
    ) -> EmptyRollbackExecutionReceipt:
        records = self._records()
        if records and records[-1].event == RollbackEvent.COMPLETE.value:
            self._acquire_controller_authority_marker()
            self._establish_administrative_writer_fence_and_recheck_empty()
            self._verify_terminal_state_and_evidence()
            return self._execution_receipt(
                outcome="empty_store_rollback_already_complete",
                records=records,
            )
        applied_count = sum(
            record.event == RollbackEvent.APPLIED.value for record in records
        )
        if applied_count >= 3:
            self._require_current_install_binding()
        open_intent = bool(records and records[-1].event == RollbackEvent.INTENT.value)
        if 4 <= applied_count < len(self.plan.steps):
            self._acquire_controller_authority_marker()
        for index, step in enumerate(self.plan.steps):
            if index < applied_count:
                continue
            remaining_records = 2 * (len(self.plan.steps) - index) + 1
            self.journal.reserve_effect_capacity(remaining_records)
            if not open_intent:
                self._append(step.step_id, RollbackEvent.INTENT)
            self._perform(step, recovering=open_intent)
            self._append(step.step_id, RollbackEvent.APPLIED)
            open_intent = False
        self._append("R99_ROLLBACK_COMPLETE", RollbackEvent.COMPLETE)
        records = self._records()
        self._verify_terminal_state_and_evidence()
        return self._execution_receipt(
            outcome="empty_store_rollback_complete",
            records=records,
        )

    def run(self) -> EmptyRollbackExecutionReceipt:
        receipt = self._run_while_managing_controller_authority_marker()
        self._release_controller_authority_marker()
        return receipt


def _run_authorized_empty_store_rollback(
    *,
    verified_rollback_capability: object,
    verified_package_capability: object,
    verified_controller_runtime_capability: object,
    eligibility_receipt: Mapping[str, object],
    resources: object,
    authority_state: AuthorityState,
    clock: TrustedUtcClock,
    held_lock: HeldExecutionLockCapability,
    journal_factory: RollbackJournalFactory | None,
    operations: EmptyRollbackOperations | None,
    resolved_store_spec: Mapping[str, object] | None,
    receipt_store: DurableReceiptStore,
    authority_mode: RollbackAuthorityMode,
    require_durable_journal: bool,
    require_production_receipt_store: bool,
) -> EmptyRollbackExecutionReceipt:
    """Shared composition; non-durable use is restricted to in-process tests."""

    try:
        package_evidence, _unused_package_scope, package_artifacts = (
            _package_capability_parts(verified_package_capability)
        )
        runtime_evidence = _controller_runtime_capability_evidence(
            verified_controller_runtime_capability
        )
        rollback_evidence = rollback_capability_evidence(
            verified_rollback_capability
        )
        authority_runtime = (
            rollback_evidence.controller_runtime_receipt_sha256,
            rollback_evidence.controller_runtime_root,
            rollback_evidence.controller_runtime_tree_sha256,
            rollback_evidence.controller_release_root,
            rollback_evidence.controller_release_tree_sha256,
            rollback_evidence.controller_release_package_manifest_path,
            rollback_evidence.controller_runtime_interpreter_path,
            rollback_evidence.controller_runtime_interpreter_sha256,
            rollback_evidence.controller_runtime_inventory_path,
            rollback_evidence.controller_runtime_inventory_sha256,
            rollback_evidence.controller_requirements_lock_sha256,
            rollback_evidence.supervisor_launcher_path,
            rollback_evidence.supervisor_launcher_sha256,
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
        if (
            package_evidence.package_manifest_sha256
            != rollback_evidence.package_manifest_sha256
            or package_evidence.package_manifest_sha256
            != runtime_evidence.package_manifest_sha256
            or package_evidence.controller_runtime_receipt_sha256
            != runtime_evidence.controller_runtime_receipt_sha256
            or authority_runtime != verified_runtime
        ):
            raise EmptyRollbackExecutionError(
                "empty_rollback_controller_runtime_binding_mismatch"
            )
        resource_evidence, normalized_resources = (
            verified_rollback_resource_parts(resources)
        )
        plan = build_empty_rollback_plan(
            verified_rollback_capability,
            eligibility_receipt,
            resources,
        )
        if normalized_resources != plan.resources:
            raise EmptyRollbackError(
                "empty_rollback_resource_provenance_mismatch"
            )
    except (
        EmptyRollbackError,
        RollbackAuthorityError,
        PackageCapabilityError,
        ControllerRuntimeCapabilityError,
    ) as error:
        raise EmptyRollbackExecutionError("empty_rollback_plan_refused") from error
    if (
        type(receipt_store) is not DurableReceiptStore
        or type(require_durable_journal) is not bool
        or type(require_production_receipt_store) is not bool
        or type(authority_mode) is not RollbackAuthorityMode
        or (
            require_production_receipt_store
            and (
                journal_factory is not None
                or require_durable_journal is not True
                or operations is not None
                or not isinstance(resolved_store_spec, Mapping)
            )
        )
        or (
            not require_production_receipt_store
            and (
                not callable(journal_factory)
                or operations is None
                or resolved_store_spec is not None
            )
        )
    ):
        raise EmptyRollbackExecutionError(
            "empty_rollback_execution_dependency_invalid"
        )
    try:
        if require_production_receipt_store:
            receipt_store.require_production_binding()
        else:
            receipt_store.require_synthetic_binding()
    except DurableReceiptError as error:
        raise EmptyRollbackExecutionError(
            "empty_rollback_receipt_store_invalid"
        ) from error
    try:
        preclaim_rollback_evidence = rollback_capability_evidence(
            verified_rollback_capability
        )
        recovery_reservation_claim_sha256 = (
            _verify_recovery_reservation_claim(
                preclaim_rollback_evidence,
                authority_state=authority_state,
                held_lock=held_lock,
            )
        )
    except RollbackAuthorityError as error:
        raise EmptyRollbackExecutionError(
            "empty_rollback_execution_authority_invalid"
        ) from error
    try:
        durable_install = receipt_store.read(
            ReceiptArtifact.INSTALL,
            plan.installation_execution_id,
        )
        if (
            durable_install.receipt_sha256
            != plan.installation_receipt_sha256
            or durable_install.canonical_receipt.get("package_manifest_sha256")
            != plan.package_manifest_sha256
        ):
            raise EmptyRollbackExecutionError(
                "empty_rollback_install_receipt_preclaim_mismatch"
            )
        retained_evidence_capability = (
            verify_retained_install_receipt_and_anchored_ledger(
                canonical_json_bytes(dict(durable_install.canonical_receipt)),
                resources,
                authority_state=authority_state,
                held_lock=held_lock,
                recovery_reservation_claim_sha256=(
                    recovery_reservation_claim_sha256
                ),
            )
        )
    except EmptyRollbackExecutionError:
        raise
    except DurableReceiptError as error:
        raise EmptyRollbackExecutionError(
            "empty_rollback_preclaim_receipt_validation_failed"
        ) from error
    claimed = _claim_empty_rollback(
        verified_rollback_capability,
        plan,
        retained_evidence_capability=retained_evidence_capability,
        expected_resource_evidence=resource_evidence,
        authority_mode=authority_mode,
        authority_state=authority_state,
        clock=clock,
        held_lock=held_lock,
    )
    claim_evidence = claimed_empty_rollback_evidence(claimed)
    if claim_evidence.installation_execution_id != plan.installation_execution_id:
        raise EmptyRollbackExecutionError(
            "empty_rollback_installation_execution_binding_mismatch"
        )
    try:
        durable_eligibility = receipt_store.write_once(
            ReceiptArtifact.EMPTY_ROLLBACK_ELIGIBILITY,
            plan.installation_execution_id,
            eligibility_receipt,
        )
    except EmptyRollbackExecutionError:
        raise
    except DurableReceiptError as error:
        raise EmptyRollbackExecutionError(
            "empty_rollback_preclaim_receipt_validation_failed"
        ) from error
    if (
        durable_eligibility.receipt_sha256
        != plan.eligibility_receipt_sha256
        or dict(durable_eligibility.canonical_receipt)
        != dict(eligibility_receipt)
    ):
        raise EmptyRollbackExecutionError(
            "empty_rollback_eligibility_receipt_persistence_mismatch"
        )
    journal = None
    try:
        if require_production_receipt_store:
            journal = _production_rollback_journal(
                claimed,
                authority_state=authority_state,
                held_lock=held_lock,
            )
        else:
            assert journal_factory is not None
            journal = journal_factory(claimed)
        if require_durable_journal:
            # Imported lazily because rollback_journal imports the record types
            # above.  Exact type prevents a duck-typed or volatile journal from
            # entering the public destructive composition.
            from .rollback_journal import DurableRollbackJournal

            if type(journal) is not DurableRollbackJournal:
                raise EmptyRollbackExecutionError(
                    "empty_rollback_durable_journal_required"
                )
        if require_production_receipt_store:
            # The public path accepts immutable evidence, never an effect
            # implementation.  Select the exact closed Linux composition only
            # after the authority claim, retained receipts, and durable journal
            # have all been verified.
            from .rollback_live_adapter import (
                ProductionLinuxEmptyRollbackOperationsFactory,
            )

            try:
                operations = ProductionLinuxEmptyRollbackOperationsFactory(
                    held_lock=held_lock,
                    verified_controller_runtime_capability=(
                        verified_controller_runtime_capability
                    ),
                    authority_state=authority_state,
                    receipt_store=receipt_store,
                )(
                    claimed_rollback=claimed,
                    verified_rollback_capability=(
                        verified_rollback_capability
                    ),
                    signed_eligibility=eligibility_receipt,
                    resolved_store_spec=resolved_store_spec,
                    verified_artifacts=package_artifacts,
                    verified_resources=resources,
                    installation_receipt=durable_install.canonical_receipt,
                    rollback_journal=journal,
                )
            except Exception as error:
                raise EmptyRollbackExecutionError(
                    "empty_rollback_production_operations_binding_failed"
                ) from error
        if operations is None:
            raise EmptyRollbackExecutionError(
                "empty_rollback_execution_dependency_invalid"
            )
        return _EmptyRollbackController(
            plan=plan,
            signed_eligibility=eligibility_receipt,
            expected_resource_evidence=resource_evidence,
            claimed=claimed,
            journal=journal,
            operations=operations,
            authority_state=authority_state,
            held_lock=held_lock,
            receipt_store=receipt_store,
        ).run()
    finally:
        close = getattr(journal, "close", None)
        if callable(close):
            close()


def _run_authorized_empty_store_rollback_synthetic(
    *,
    verified_rollback_capability: object,
    verified_package_capability: object,
    verified_controller_runtime_capability: object,
    eligibility_receipt: Mapping[str, object],
    resources: object,
    authority_state: AuthorityState,
    clock: TrustedUtcClock,
    held_lock: HeldExecutionLockCapability,
    journal_factory: RollbackJournalFactory,
    operations: EmptyRollbackOperations,
    receipt_store: DurableReceiptStore,
    authority_mode: RollbackAuthorityMode = RollbackAuthorityMode.START_ROLLBACK,
) -> EmptyRollbackExecutionReceipt:
    """Private test harness path for fault-injected in-process journals."""

    return _run_authorized_empty_store_rollback(
        verified_rollback_capability=verified_rollback_capability,
        verified_package_capability=verified_package_capability,
        verified_controller_runtime_capability=(
            verified_controller_runtime_capability
        ),
        eligibility_receipt=eligibility_receipt,
        resources=resources,
        authority_state=authority_state,
        clock=clock,
        held_lock=held_lock,
        journal_factory=journal_factory,
        operations=operations,
        resolved_store_spec=None,
        receipt_store=receipt_store,
        authority_mode=authority_mode,
        require_durable_journal=False,
        require_production_receipt_store=False,
    )


def _run_authorized_empty_store_rollback_durable_synthetic(
    *,
    verified_rollback_capability: object,
    verified_package_capability: object,
    verified_controller_runtime_capability: object,
    eligibility_receipt: Mapping[str, object],
    resources: object,
    authority_state: AuthorityState,
    clock: TrustedUtcClock,
    held_lock: HeldExecutionLockCapability,
    journal_factory: RollbackJournalFactory,
    operations: EmptyRollbackOperations,
    receipt_store: DurableReceiptStore,
    authority_mode: RollbackAuthorityMode = RollbackAuthorityMode.START_ROLLBACK,
) -> EmptyRollbackExecutionReceipt:
    """Private durable-journal test path requiring a synthetic receipt store."""

    return _run_authorized_empty_store_rollback(
        verified_rollback_capability=verified_rollback_capability,
        verified_package_capability=verified_package_capability,
        verified_controller_runtime_capability=(
            verified_controller_runtime_capability
        ),
        eligibility_receipt=eligibility_receipt,
        resources=resources,
        authority_state=authority_state,
        clock=clock,
        held_lock=held_lock,
        journal_factory=journal_factory,
        operations=operations,
        resolved_store_spec=None,
        receipt_store=receipt_store,
        authority_mode=authority_mode,
        require_durable_journal=True,
        require_production_receipt_store=False,
    )


def run_authorized_empty_store_rollback(
    *,
    verified_rollback_capability: object,
    verified_package_capability: object,
    verified_controller_runtime_capability: object,
    eligibility_receipt: Mapping[str, object],
    resources: object,
    authority_state: AuthorityState,
    clock: TrustedUtcClock,
    held_lock: HeldExecutionLockCapability,
    resolved_store_spec: Mapping[str, object],
    receipt_store: DurableReceiptStore,
) -> EmptyRollbackExecutionReceipt:
    """Execute through the fixed production Linux rollback composition.

    The caller supplies verified capabilities, immutable store evidence, the
    held controller lock, and the canonical durable receipt store.  It cannot
    inject a journal, operation, transport, probe, receipt sink, audit source,
    or physical deletion driver.
    """

    return _run_authorized_empty_store_rollback(
        verified_rollback_capability=verified_rollback_capability,
        verified_package_capability=verified_package_capability,
        verified_controller_runtime_capability=(
            verified_controller_runtime_capability
        ),
        eligibility_receipt=eligibility_receipt,
        resources=resources,
        authority_state=authority_state,
        clock=clock,
        held_lock=held_lock,
        journal_factory=None,
        operations=None,
        resolved_store_spec=resolved_store_spec,
        receipt_store=receipt_store,
        authority_mode=RollbackAuthorityMode.START_ROLLBACK,
        require_durable_journal=True,
        require_production_receipt_store=True,
    )


def start_reserved_authorized_empty_store_rollback(
    *,
    verified_rollback_capability: object,
    verified_package_capability: object,
    verified_controller_runtime_capability: object,
    eligibility_receipt: Mapping[str, object],
    resources: object,
    authority_state: AuthorityState,
    clock: TrustedUtcClock,
    held_lock: HeldExecutionLockCapability,
    resolved_store_spec: Mapping[str, object],
    receipt_store: DurableReceiptStore,
) -> EmptyRollbackExecutionReceipt:
    """Consume a preclaimed recovery reservation for the first rollback only."""

    return _run_authorized_empty_store_rollback(
        verified_rollback_capability=verified_rollback_capability,
        verified_package_capability=verified_package_capability,
        verified_controller_runtime_capability=(
            verified_controller_runtime_capability
        ),
        eligibility_receipt=eligibility_receipt,
        resources=resources,
        authority_state=authority_state,
        clock=clock,
        held_lock=held_lock,
        journal_factory=None,
        operations=None,
        resolved_store_spec=resolved_store_spec,
        receipt_store=receipt_store,
        authority_mode=RollbackAuthorityMode.START_RESERVED_ROLLBACK,
        require_durable_journal=True,
        require_production_receipt_store=True,
    )


def resume_authorized_empty_store_rollback(
    *,
    verified_rollback_capability: object,
    verified_package_capability: object,
    verified_controller_runtime_capability: object,
    eligibility_receipt: Mapping[str, object],
    resources: object,
    authority_state: AuthorityState,
    clock: TrustedUtcClock,
    held_lock: HeldExecutionLockCapability,
    resolved_store_spec: Mapping[str, object],
    receipt_store: DurableReceiptStore,
) -> EmptyRollbackExecutionReceipt:
    """Resume only the exact previously claimed rollback, including after expiry."""

    return _run_authorized_empty_store_rollback(
        verified_rollback_capability=verified_rollback_capability,
        verified_package_capability=verified_package_capability,
        verified_controller_runtime_capability=(
            verified_controller_runtime_capability
        ),
        eligibility_receipt=eligibility_receipt,
        resources=resources,
        authority_state=authority_state,
        clock=clock,
        held_lock=held_lock,
        journal_factory=None,
        operations=None,
        resolved_store_spec=resolved_store_spec,
        receipt_store=receipt_store,
        authority_mode=RollbackAuthorityMode.RESUME_ROLLBACK,
        require_durable_journal=True,
        require_production_receipt_store=True,
    )


__all__ = [
    "AUTHORITY_STATE_PATH",
    "ClaimedEmptyRollbackEvidence",
    "EmptyRollbackExecutionError",
    "EmptyRollbackExecutionReceipt",
    "EmptyRollbackControllerAuthorityMarkerAcquisition",
    "EmptyRollbackControllerAuthorityMarkerEvidence",
    "EmptyRollbackOperations",
    "GLOBAL_LOCK_PATH",
    "MAX_RETAINED_INSTALL_RECEIPT_BYTES",
    "ROLLBACK_JOURNAL_SCHEMA",
    "RollbackAuthorityMode",
    "RollbackEvent",
    "RollbackJournalAdapter",
    "RollbackJournalRecord",
    "RollbackObservation",
    "RollbackOperationRequest",
    "VerifiedInstallReceiptLedgerEvidence",
    "canonical_live_rollback_controller_authority_marker_sha256",
    "claimed_empty_rollback_evidence",
    "run_authorized_empty_store_rollback",
    "resume_authorized_empty_store_rollback",
    "start_reserved_authorized_empty_store_rollback",
    "validate_empty_rollback_controller_authority_marker_capability",
    "validate_install_receipt_ledger_capability",
    "validate_rollback_records",
    "verify_retained_install_receipt_and_ledger",
    "verify_retained_install_receipt_and_anchored_ledger",
]
