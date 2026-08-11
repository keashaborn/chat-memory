from __future__ import annotations

"""Synthetic-only Phase 8A inactive installation state machine.

The controller seals ordering, durable intent, recovery, and empty-only
rollback semantics.  Phase 8A deliberately provides no live backend.
"""

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re
from typing import Final, Protocol, runtime_checkable

from .journal import FileJournal, JournalEntry, JournalIntegrityError


INSTALL_STEPS: Final = (
    "I04_QUARANTINE_LEGACY_SECRET",
    "I05_CREATE_SERVICE_IDENTITY",
    "I06_CREATE_OWNED_ROOTS",
    "I07_STAGE_IMMUTABLE_RELEASE",
    "I08_PROVISION_FRESH_STORE_SECRETS",
    "I09_CREATE_NETWORK",
    "I10_CREATE_POSTGRES_VOLUME",
    "I11_CREATE_QDRANT_VOLUME",
    "I12_CREATE_POSTGRES_CONTAINER",
    "I13_CREATE_QDRANT_CONTAINER",
    "I14_START_VERIFY_STORES",
    "I15_BOOTSTRAP_CANONICAL_CLUSTER",
    "I16_APPLY_FOUNDATION_0001",
    "I17_APPLY_OWNER_CLAIM_DETAIL_0003",
    "I18_APPLY_PILOT_MARKER_0004",
    "I19_CREATE_QDRANT_COLLECTION",
    "I20_CREATE_QDRANT_ALIAS",
    "I21_INSTALL_STORE_SUPERVISOR",
    "I22_ENABLE_START_STORE_SUPERVISOR",
    "I23_INSTALL_HTTP_UNIT_DISABLED",
    "I24_INSTALL_WORKER_UNIT_DISABLED",
    "I25_DAEMON_RELOAD_VERIFY_APP_UNITS_DORMANT",
    "I26_CREATE_ENCRYPTED_EMPTY_BACKUP",
    "I27_COLD_RESTART_SAME_VOLUMES_PROOF",
    "I28_RESTORE_DRILL_EMPTY_CANONICAL_PROOF",
    "I29_SEAL_INACTIVE_POSTFLIGHT",
)

ROLLBACK_STEPS: Final = (
    "R01_VERIFY_EMPTY_EXACT_OWNERSHIP",
    "R02_REMOVE_WORKER_UNIT",
    "R03_REMOVE_HTTP_UNIT",
    "R04_DISABLE_REMOVE_STORE_SUPERVISOR",
    "R05_DELETE_QDRANT_ALIAS_EMPTY_ONLY",
    "R06_DELETE_QDRANT_COLLECTION_EMPTY_ONLY",
    "R07_ROLLBACK_PILOT_MARKER_0004_EMPTY_ONLY",
    "R08_ROLLBACK_OWNER_CLAIM_DETAIL_0003",
    "R09_ROLLBACK_FOUNDATION_0001_EMPTY_ONLY",
    "R10_DROP_CANONICAL_DATABASE_ROLES_EMPTY_ONLY",
    "R11_STOP_REMOVE_QDRANT_CONTAINER",
    "R12_STOP_REMOVE_POSTGRES_CONTAINER",
    "R13_REMOVE_QDRANT_VOLUME_EXACT_EMPTY",
    "R14_REMOVE_POSTGRES_VOLUME_EXACT_EMPTY",
    "R15_REMOVE_NETWORK_EXACT_UNUSED",
    "R16_REMOVE_BACKUP_ATTEMPT_ARTIFACTS",
    "R17_REMOVE_FRESH_STORE_SECRETS",
    "R18_REMOVE_IMMUTABLE_RELEASE",
    "R19_REMOVE_ATTEMPT_CHILDREN_RETAIN_NAMED_ROOTS",
    "R20_VERIFY_FINAL_ABSENCE",
)

# A rollback step is applicable once any listed install effect reached its
# after-state.  R01 and R20 always run.  Quarantined legacy credentials,
# service identity, all exact named roots, and the journal are retained.  R19
# clears the I06 attempt effect after removing exact attempt-owned children; it
# does not remove the named roots themselves.
ROLLBACK_PREREQUISITES: Final = {
    "R02_REMOVE_WORKER_UNIT": ("I24_INSTALL_WORKER_UNIT_DISABLED",),
    "R03_REMOVE_HTTP_UNIT": ("I23_INSTALL_HTTP_UNIT_DISABLED",),
    "R04_DISABLE_REMOVE_STORE_SUPERVISOR": (
        "I21_INSTALL_STORE_SUPERVISOR",
        "I22_ENABLE_START_STORE_SUPERVISOR",
    ),
    "R05_DELETE_QDRANT_ALIAS_EMPTY_ONLY": ("I20_CREATE_QDRANT_ALIAS",),
    "R06_DELETE_QDRANT_COLLECTION_EMPTY_ONLY": (
        "I19_CREATE_QDRANT_COLLECTION",
    ),
    "R07_ROLLBACK_PILOT_MARKER_0004_EMPTY_ONLY": (
        "I18_APPLY_PILOT_MARKER_0004",
    ),
    "R08_ROLLBACK_OWNER_CLAIM_DETAIL_0003": (
        "I17_APPLY_OWNER_CLAIM_DETAIL_0003",
    ),
    "R09_ROLLBACK_FOUNDATION_0001_EMPTY_ONLY": (
        "I16_APPLY_FOUNDATION_0001",
    ),
    "R10_DROP_CANONICAL_DATABASE_ROLES_EMPTY_ONLY": (
        "I15_BOOTSTRAP_CANONICAL_CLUSTER",
    ),
    "R11_STOP_REMOVE_QDRANT_CONTAINER": ("I13_CREATE_QDRANT_CONTAINER",),
    "R12_STOP_REMOVE_POSTGRES_CONTAINER": ("I12_CREATE_POSTGRES_CONTAINER",),
    "R13_REMOVE_QDRANT_VOLUME_EXACT_EMPTY": ("I11_CREATE_QDRANT_VOLUME",),
    "R14_REMOVE_POSTGRES_VOLUME_EXACT_EMPTY": (
        "I10_CREATE_POSTGRES_VOLUME",
    ),
    "R15_REMOVE_NETWORK_EXACT_UNUSED": ("I09_CREATE_NETWORK",),
    "R16_REMOVE_BACKUP_ATTEMPT_ARTIFACTS": (
        "I26_CREATE_ENCRYPTED_EMPTY_BACKUP",
        "I28_RESTORE_DRILL_EMPTY_CANONICAL_PROOF",
    ),
    "R17_REMOVE_FRESH_STORE_SECRETS": (
        "I08_PROVISION_FRESH_STORE_SECRETS",
    ),
    "R18_REMOVE_IMMUTABLE_RELEASE": ("I07_STAGE_IMMUTABLE_RELEASE",),
    "R19_REMOVE_ATTEMPT_CHILDREN_RETAIN_NAMED_ROOTS": (
        "I06_CREATE_OWNED_ROOTS",
    ),
}

RETAINED_INSTALL_EFFECTS: Final = (
    "I04_QUARANTINE_LEGACY_SECRET",
    "I05_CREATE_SERVICE_IDENTITY",
)
EXECUTION_MODE: Final = "phase8a_synthetic_disposable_only"
_SYNTHETIC_BACKEND_SEAL: Final = object()
HASH_RE: Final = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
ATTEMPT_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z", re.ASCII)


class ControllerError(RuntimeError):
    """Base class for typed controller refusals and failures."""


class AuthorizationError(ControllerError):
    pass


class BackendNotAuthorizedError(ControllerError):
    pass


class OperationStateError(ControllerError):
    pass


class StateDriftError(ControllerError):
    pass


class RollbackGateError(ControllerError):
    def __init__(self, refusal_codes: tuple[str, ...]) -> None:
        self.refusal_codes = refusal_codes
        super().__init__("rollback_gate_refused:" + ",".join(refusal_codes))


class InstallationCompensatedError(ControllerError):
    def __init__(self, cause: Exception, receipt: ControllerReceipt) -> None:
        self.cause = cause
        self.receipt = receipt
        super().__init__("installation_failed_and_same_attempt_compensated")


class CompensationFailedError(ControllerError):
    def __init__(self, cause: Exception, compensation_error: Exception) -> None:
        self.cause = cause
        self.compensation_error = compensation_error
        super().__init__("installation_failure_compensation_failed")


class ControllerInterruption(BaseException):
    """Synthetic process-death boundary; never triggers in-process cleanup."""


class StepState(str, Enum):
    BEFORE = "before"
    AFTER = "after"
    DRIFT = "drift"


@dataclass(frozen=True, slots=True)
class DisposableControllerAuthorization:
    """Synthetic-proof capability that can never authorize live execution.

    This is intentionally unrelated to
    ``CryptographicallyValidScopeNotExecution``.  Phase 8A contains no adapter
    that converts an externally verified scope into a live capability; that
    atomic nonce-claim boundary is deferred to a separately authorized phase.
    """

    operation: str
    binding_sha256: str
    proof_receipt_sha256: str
    execution_mode: str = EXECUTION_MODE

    def __post_init__(self) -> None:
        if self.operation not in {"install", "rollback"}:
            raise AuthorizationError("authorization_operation_invalid")
        if (
            HASH_RE.fullmatch(self.binding_sha256) is None
            or HASH_RE.fullmatch(self.proof_receipt_sha256) is None
        ):
            raise AuthorizationError("authorization_hash_invalid")
        if self.execution_mode != EXECUTION_MODE:
            raise AuthorizationError("authorization_execution_mode_invalid")


@dataclass(frozen=True, slots=True)
class RollbackFacts:
    pilot_ever_started: bool = False
    successor_user_memory_row_count: int = 0
    successor_projection_queue_row_count: int = 0
    qdrant_point_count: int = 0
    active_client_count: int = 0
    legacy_import_count: int = 0

    def refusal_codes(self) -> tuple[str, ...]:
        codes: list[str] = []
        if self.pilot_ever_started is not False:
            codes.append("pilot_ever_started")
        for field in (
            "successor_user_memory_row_count",
            "successor_projection_queue_row_count",
            "qdrant_point_count",
            "active_client_count",
            "legacy_import_count",
        ):
            value = getattr(self, field)
            if not isinstance(value, int) or isinstance(value, bool) or value != 0:
                codes.append(field + "_not_zero")
        return tuple(sorted(codes))


@runtime_checkable
class ControllerBackend(Protocol):
    controller_mode: str
    _phase8a_synthetic_seal: object

    def probe(self, operation: str, step_id: str) -> StepState:
        ...

    def execute(self, operation: str, step_id: str) -> None:
        ...

    def checkpoint(self, operation: str, step_id: str, event: str) -> None:
        ...

    def rollback_facts(self) -> RollbackFacts:
        ...


@dataclass(frozen=True, slots=True)
class ControllerReceipt:
    operation: str
    outcome: str
    binding_sha256: str
    disposable_authorization_sha256: str
    attempt_id: str
    completed_step_ids: tuple[str, ...]
    recovered_step_ids: tuple[str, ...]
    journal_sequence: int
    journal_head_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "governed-memory-installation-controller-receipt-v1",
            "operation": self.operation,
            "outcome": self.outcome,
            "binding_sha256": self.binding_sha256,
            "disposable_authorization_sha256": self.disposable_authorization_sha256,
            "attempt_id": self.attempt_id,
            "completed_step_ids": list(self.completed_step_ids),
            "recovered_step_ids": list(self.recovered_step_ids),
            "journal_sequence": self.journal_sequence,
            "journal_head_sha256": self.journal_head_sha256,
        }

    @property
    def receipt_sha256(self) -> str:
        encoded = json.dumps(
            self.as_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()


def _validate_attempt_id(attempt_id: str) -> None:
    if not isinstance(attempt_id, str) or ATTEMPT_RE.fullmatch(attempt_id) is None:
        raise OperationStateError("attempt_id_invalid")


class InactiveInstallationController:
    """Resumable executor restricted to a disposable synthetic backend."""

    def __init__(
        self,
        *,
        backend: ControllerBackend,
        journal: FileJournal,
        binding_sha256: str,
    ) -> None:
        if HASH_RE.fullmatch(binding_sha256) is None:
            raise OperationStateError("controller_binding_invalid")
        if journal.binding_sha256 != binding_sha256:
            raise OperationStateError("controller_journal_binding_mismatch")
        if not isinstance(backend, ControllerBackend):
            raise BackendNotAuthorizedError("controller_backend_protocol_invalid")
        if backend.controller_mode != EXECUTION_MODE:
            raise BackendNotAuthorizedError("live_backend_unavailable_in_phase8a")
        if backend._phase8a_synthetic_seal is not _SYNTHETIC_BACKEND_SEAL:
            raise BackendNotAuthorizedError("synthetic_backend_seal_invalid")
        # Protocol conformance is the future adapter seam, not Phase 8A
        # execution authority.  This phase accepts only the exact inert model;
        # subclasses could override execute() with host mutations.
        from .synthetic_backend import SyntheticBackend

        if type(backend) is not SyntheticBackend:
            raise BackendNotAuthorizedError("exact_synthetic_backend_required")
        self.backend = backend
        self.journal = journal
        self.binding_sha256 = binding_sha256

    def install(
        self,
        authorization: DisposableControllerAuthorization,
        *,
        attempt_id: str,
    ) -> ControllerReceipt:
        self._authorize(authorization, operation="install")
        _validate_attempt_id(attempt_id)
        self._assert_attempt("install", attempt_id)
        compensation_entries = self._operation_entries("compensation")
        if compensation_entries:
            self._assert_attempt("compensation", attempt_id)
            recorded_authorization_sha256 = compensation_entries[
                0
            ].disposable_authorization_sha256
            if (
                authorization.proof_receipt_sha256
                != recorded_authorization_sha256
            ):
                raise AuthorizationError(
                    "compensation_authorization_binding_mismatch"
                )
            install_entries = self._operation_entries("install")
            if not install_entries:
                raise OperationStateError(
                    "compensation_install_history_missing"
                )
            self._history_by_step(
                operation="install",
                steps=INSTALL_STEPS,
                attempt_id=attempt_id,
                authorization_receipt_sha256=(
                    recorded_authorization_sha256
                ),
            )
            operations = tuple(entry.operation for entry in self.journal.entries)
            first_compensation = operations.index("compensation")
            if (
                any(
                    operation != "install"
                    for operation in operations[:first_compensation]
                )
                or any(
                    operation != "compensation"
                    for operation in operations[first_compensation:]
                )
            ):
                raise OperationStateError(
                    "compensation_cross_operation_order_invalid"
                )
            receipt = self._run_rollback(
                operation="compensation",
                attempt_id=attempt_id,
                authorization_receipt_sha256=(
                    recorded_authorization_sha256
                ),
            )
            return ControllerReceipt(
                operation="install",
                outcome="same_attempt_compensated",
                binding_sha256=receipt.binding_sha256,
                disposable_authorization_sha256=(
                    authorization.proof_receipt_sha256
                ),
                attempt_id=attempt_id,
                completed_step_ids=receipt.completed_step_ids,
                recovered_step_ids=receipt.recovered_step_ids,
                journal_sequence=receipt.journal_sequence,
                journal_head_sha256=receipt.journal_head_sha256,
            )
        if self._operation_entries("rollback"):
            raise OperationStateError("installation_already_rolled_back")
        try:
            return self._run_steps(
                operation="install",
                steps=INSTALL_STEPS,
                authorization_receipt_sha256=(
                    authorization.proof_receipt_sha256
                ),
                attempt_id=attempt_id,
                outcome="inactive_installation_complete",
            )
        except (
            AuthorizationError,
            JournalIntegrityError,
            OperationStateError,
            RollbackGateError,
            StateDriftError,
        ):
            raise
        except Exception as cause:
            try:
                receipt = self._run_rollback(
                    operation="compensation",
                    attempt_id=attempt_id,
                    authorization_receipt_sha256=(
                        authorization.proof_receipt_sha256
                    ),
                )
            except Exception as compensation_error:
                raise CompensationFailedError(
                    cause, compensation_error
                ) from compensation_error
            raise InstallationCompensatedError(cause, receipt) from cause

    def rollback(
        self,
        authorization: DisposableControllerAuthorization,
        *,
        attempt_id: str,
    ) -> ControllerReceipt:
        self._authorize(authorization, operation="rollback")
        _validate_attempt_id(attempt_id)
        if self._operation_entries("compensation"):
            raise OperationStateError("same_attempt_compensation_already_recorded")
        self._validate_install_history_before_rollback()
        self._assert_attempt("rollback", attempt_id)
        return self._run_rollback(
            operation="rollback",
            attempt_id=attempt_id,
            authorization_receipt_sha256=authorization.proof_receipt_sha256,
        )

    def _validate_install_history_before_rollback(self) -> None:
        entries = self.journal.entries
        operations = tuple(entry.operation for entry in entries)
        if "rollback" in operations:
            first_rollback = operations.index("rollback")
            if (
                any(operation != "install" for operation in operations[:first_rollback])
                or any(
                    operation != "rollback"
                    for operation in operations[first_rollback:]
                )
            ):
                raise OperationStateError("rollback_cross_operation_order_invalid")
        elif any(operation != "install" for operation in operations):
            raise OperationStateError("rollback_cross_operation_order_invalid")
        install_entries = self._operation_entries("install")
        if not install_entries:
            return
        install_attempts = {entry.attempt_id for entry in install_entries}
        install_authorizations = {
            entry.disposable_authorization_sha256 for entry in install_entries
        }
        if len(install_attempts) != 1:
            raise OperationStateError("install_attempt_id_mismatch")
        if len(install_authorizations) != 1:
            raise AuthorizationError("install_authorization_binding_mismatch")
        self._history_by_step(
            operation="install",
            steps=INSTALL_STEPS,
            attempt_id=next(iter(install_attempts)),
            authorization_receipt_sha256=next(iter(install_authorizations)),
        )

    def _authorize(
        self,
        authorization: DisposableControllerAuthorization,
        *,
        operation: str,
    ) -> None:
        if not isinstance(authorization, DisposableControllerAuthorization):
            raise AuthorizationError("disposable_controller_authorization_required")
        if authorization.operation != operation:
            raise AuthorizationError("authorization_operation_mismatch")
        if authorization.binding_sha256 != self.binding_sha256:
            raise AuthorizationError("authorization_binding_mismatch")
        if authorization.execution_mode != EXECUTION_MODE:
            raise AuthorizationError("authorization_execution_mode_invalid")

    def _operation_entries(self, operation: str) -> tuple[JournalEntry, ...]:
        return tuple(
            entry for entry in self.journal.entries if entry.operation == operation
        )

    def _assert_attempt(self, operation: str, attempt_id: str) -> None:
        attempts = {entry.attempt_id for entry in self._operation_entries(operation)}
        if attempts and attempts != {attempt_id}:
            raise OperationStateError("operation_attempt_id_mismatch")

    def _rollback_steps(self, operation: str) -> tuple[str, ...]:
        install_events = {
            step_id: tuple(
                entry.event
                for entry in self._operation_entries("install")
                if entry.step_id == step_id
            )
            for step_id in INSTALL_STEPS
        }
        already_journaled = {
            entry.step_id for entry in self._operation_entries(operation)
        }
        selected: list[str] = []
        for step_id in ROLLBACK_STEPS:
            if step_id in {
                "R01_VERIFY_EMPTY_EXACT_OWNERSHIP",
                "R20_VERIFY_FINAL_ABSENCE",
            }:
                selected.append(step_id)
                continue
            prerequisites = ROLLBACK_PREREQUISITES[step_id]
            if step_id in already_journaled:
                selected.append(step_id)
                continue
            owned_effect_present = False
            for prerequisite in prerequisites:
                events = install_events[prerequisite]
                state = self.backend.probe("install", prerequisite)
                if state is StepState.DRIFT:
                    raise StateDriftError(
                        "install_state_drift_before_rollback:" + prerequisite
                    )
                if not events:
                    if state is not StepState.BEFORE:
                        raise StateDriftError(
                            "unowned_install_state_before_rollback:"
                            + prerequisite
                        )
                    continue
                if events in {
                    ("intent", "effect_complete"),
                    ("intent", "recovered_after_effect"),
                }:
                    if state is not StepState.AFTER:
                        raise StateDriftError(
                            "completed_install_state_not_after_before_rollback:"
                            + prerequisite
                        )
                    owned_effect_present = True
                    continue
                if events == ("intent",):
                    if state is StepState.AFTER:
                        owned_effect_present = True
                    elif state is not StepState.BEFORE:
                        raise StateDriftError(
                            "install_intent_state_invalid_before_rollback:"
                            + prerequisite
                        )
                    continue
                raise OperationStateError(
                    "install_history_invalid_before_rollback:" + prerequisite
                )
            if owned_effect_present:
                selected.append(step_id)
        return tuple(selected)

    def _run_rollback(
        self,
        *,
        operation: str,
        attempt_id: str,
        authorization_receipt_sha256: str,
    ) -> ControllerReceipt:
        facts = self.backend.rollback_facts()
        if not isinstance(facts, RollbackFacts):
            raise RollbackGateError(("rollback_facts_invalid",))
        refusal_codes = facts.refusal_codes()
        if refusal_codes:
            raise RollbackGateError(refusal_codes)
        steps = self._rollback_steps(operation)
        return self._run_steps(
            operation=operation,
            steps=steps,
            authorization_receipt_sha256=authorization_receipt_sha256,
            attempt_id=attempt_id,
            outcome=(
                "same_attempt_compensation_complete"
                if operation == "compensation"
                else "authorized_empty_only_rollback_complete"
            ),
        )

    def _history_by_step(
        self,
        *,
        operation: str,
        steps: tuple[str, ...],
        attempt_id: str,
        authorization_receipt_sha256: str,
    ) -> dict[str, tuple[JournalEntry, ...]]:
        entries = self._operation_entries(operation)
        if any(entry.attempt_id != attempt_id for entry in entries):
            raise OperationStateError("operation_attempt_id_mismatch")
        if any(
            entry.disposable_authorization_sha256
            != authorization_receipt_sha256
            for entry in entries
        ):
            raise AuthorizationError("journal_authorization_binding_mismatch")
        allowed = set(steps)
        if any(entry.step_id not in allowed for entry in entries):
            raise OperationStateError("operation_step_set_invalid")
        grouped: dict[str, list[JournalEntry]] = {step: [] for step in steps}
        seen_order: list[str] = []
        for entry in entries:
            grouped[entry.step_id].append(entry)
            if not seen_order or seen_order[-1] != entry.step_id:
                seen_order.append(entry.step_id)
        expected_order = [step for step in steps if grouped[step]]
        if seen_order != expected_order:
            raise OperationStateError("operation_step_order_invalid")
        terminal_seen = False
        for step in steps:
            events = tuple(entry.event for entry in grouped[step])
            if events not in {
                (),
                ("intent",),
                ("intent", "effect_complete"),
                ("intent", "recovered_after_effect"),
            }:
                raise OperationStateError("operation_event_order_invalid")
            if terminal_seen and events:
                raise OperationStateError("operation_history_has_gap")
            if events in {(), ("intent",)}:
                terminal_seen = True
        return {step: tuple(grouped[step]) for step in steps}

    def _run_steps(
        self,
        *,
        operation: str,
        steps: tuple[str, ...],
        authorization_receipt_sha256: str,
        attempt_id: str,
        outcome: str,
    ) -> ControllerReceipt:
        history = self._history_by_step(
            operation=operation,
            steps=steps,
            attempt_id=attempt_id,
            authorization_receipt_sha256=authorization_receipt_sha256,
        )
        recovered: list[str] = []
        for step_id in steps:
            events = tuple(entry.event for entry in history[step_id])
            state = self.backend.probe(operation, step_id)
            if state is StepState.DRIFT:
                raise StateDriftError("step_state_drift:" + step_id)
            if events in {
                ("intent", "effect_complete"),
                ("intent", "recovered_after_effect"),
            }:
                if state is not StepState.AFTER:
                    raise StateDriftError("completed_step_not_after:" + step_id)
                if events[-1] == "recovered_after_effect":
                    recovered.append(step_id)
                continue
            if not events:
                if state is not StepState.BEFORE:
                    raise StateDriftError("unowned_preexisting_effect:" + step_id)
                self.journal.append(
                    operation=operation,
                    attempt_id=attempt_id,
                    step_id=step_id,
                    event="intent",
                    disposable_authorization_sha256=authorization_receipt_sha256,
                )
                self.backend.checkpoint(operation, step_id, "after_intent")
                state = StepState.BEFORE
            if state is StepState.AFTER:
                self.journal.append(
                    operation=operation,
                    attempt_id=attempt_id,
                    step_id=step_id,
                    event="recovered_after_effect",
                    disposable_authorization_sha256=authorization_receipt_sha256,
                )
                self.backend.checkpoint(
                    operation, step_id, "after_journal_commit"
                )
                recovered.append(step_id)
                continue
            if state is not StepState.BEFORE:
                raise StateDriftError("step_resume_state_invalid:" + step_id)
            self.backend.execute(operation, step_id)
            self.backend.checkpoint(operation, step_id, "after_effect")
            if self.backend.probe(operation, step_id) is not StepState.AFTER:
                raise StateDriftError("step_effect_not_after:" + step_id)
            self.journal.append(
                operation=operation,
                attempt_id=attempt_id,
                step_id=step_id,
                event="effect_complete",
                disposable_authorization_sha256=authorization_receipt_sha256,
            )
            self.backend.checkpoint(operation, step_id, "after_journal_commit")
        completed = tuple(
            step_id
            for step_id in steps
            if any(
                entry.event in {"effect_complete", "recovered_after_effect"}
                for entry in self._operation_entries(operation)
                if entry.step_id == step_id
            )
        )
        if completed != steps:
            raise OperationStateError("operation_not_complete")
        return ControllerReceipt(
            operation=operation,
            outcome=outcome,
            binding_sha256=self.binding_sha256,
            disposable_authorization_sha256=authorization_receipt_sha256,
            attempt_id=attempt_id,
            completed_step_ids=completed,
            recovered_step_ids=tuple(recovered),
            journal_sequence=self.journal.sequence,
            journal_head_sha256=self.journal.head_sha256,
        )
