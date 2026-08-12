from __future__ import annotations

"""Hermetic Phase 8B stores-only controller state machine.

This module contains no host executor. It models an
exact twenty-step plan against an injected hermetic backend so ordering,
journal interpretation, drift refusal, and compensation algorithms can be
tested without touching a host. A separate packaged backend may supply the
durable journal adapter. This module is not end-to-end crash-recovery proof.
"""

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re
from typing import Final, Protocol

from .execution_lock import (
    ExecutionLockError,
    HeldExecutionLockCapability,
    validate_held_execution_lock,
)


EXECUTION_MODE: Final = "phase8b_hermetic_contract_only"
JOURNAL_SCHEMA_VERSION: Final = "governed-memory-phase8b-journal-v1"
ATTEMPT_STEP_ID: Final = "I00_ATTEMPT"
ZERO_HEAD: Final = "0" * 64
HASH_RE: Final = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
ATTEMPT_RE: Final = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z", re.ASCII
)
ACTION_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,95}\Z", re.ASCII)


class ControllerError(RuntimeError):
    """Base class for fail-closed Phase 8B controller errors."""


class PlanValidationError(ControllerError):
    pass


class BackendProtocolError(ControllerError):
    pass


class JournalValidationError(ControllerError):
    pass


class StateDriftError(ControllerError):
    pass


class CompletedStateError(ControllerError):
    pass


class CompensationBlockedError(ControllerError):
    pass


class ControllerLockError(ControllerError):
    pass


class CompensationFailedError(ControllerError):
    def __init__(self, cause: Exception, compensation_error: Exception) -> None:
        self.cause = cause
        self.compensation_error = compensation_error
        super().__init__("phase8b_same_attempt_compensation_failed")


class InstallationCompensatedError(ControllerError):
    def __init__(self, cause: Exception, receipt: ControllerReceipt) -> None:
        self.cause = cause
        self.receipt = receipt
        super().__init__("phase8b_install_failed_and_same_attempt_compensated")


class StepState(str, Enum):
    BEFORE = "before"
    AFTER = "after"
    RECOVERABLE = "recoverable"
    DRIFT = "drift"


class JournalEvent(str, Enum):
    INTENT = "intent"
    APPLIED = "applied"
    COMPENSATION_STARTED = "compensation_started"
    COMPENSATION_INTENT = "compensation_intent"
    COMPENSATED = "compensated"
    COMPENSATION_COMPLETE = "compensation_complete"


@dataclass(frozen=True, slots=True)
class PlanStep:
    step_id: str
    effect: str
    rollback: str

    @property
    def seals(self) -> bool:
        return self.rollback == "requires_separate_signed_rollback_after_seal"

    @property
    def compensable(self) -> bool:
        return self.rollback not in {
            "none",
            "retain_single_use_nonce_claim",
            "retain_attempt_resource_identity_ledger",
            "requires_separate_signed_rollback_after_seal",
        }


STORES_ONLY_PLAN: Final = (
    PlanStep(
        "I01_REVERIFY_PRECLAIMED_EXECUTION_LOCK",
        "reverify_preclaimed_execution_lock",
        "none",
    ),
    PlanStep(
        "I02_VERIFY_CLAIMED_EXECUTION_BINDING",
        "verify_claimed_execution_binding",
        "retain_single_use_nonce_claim",
    ),
    PlanStep(
        "I03_VERIFY_LIVE_PREFLIGHT",
        "verify_live_preflight",
        "none",
    ),
    PlanStep(
        "I04_STAGE_IMMUTABLE_CONTROLLER_RELEASE",
        "stage_immutable_controller_release",
        "remove_immutable_controller_release",
    ),
    PlanStep(
        "I05_GENERATE_FRESH_STORE_SECRETS",
        "generate_fresh_store_secrets",
        "remove_fresh_store_secrets",
    ),
    PlanStep(
        "I06_CREATE_EXACT_NETWORK",
        "create_exact_network",
        "remove_exact_unused_network",
    ),
    PlanStep(
        "I07_CREATE_EXACT_POSTGRES_VOLUME",
        "create_exact_postgres_volume",
        "remove_exact_empty_postgres_volume",
    ),
    PlanStep(
        "I08_CREATE_EXACT_QDRANT_VOLUME",
        "create_exact_qdrant_volume",
        "remove_exact_empty_qdrant_volume",
    ),
    PlanStep(
        "I09_CREATE_EXACT_POSTGRES_CONTAINER",
        "create_exact_postgres_container",
        "remove_exact_postgres_container",
    ),
    PlanStep(
        "I10_CREATE_EXACT_QDRANT_CONTAINER",
        "create_exact_qdrant_container",
        "remove_exact_qdrant_container",
    ),
    PlanStep(
        "I11_START_AND_VERIFY_EMPTY_STORES",
        "start_and_verify_empty_stores",
        "stop_exact_stores",
    ),
    PlanStep(
        "I12_BOOTSTRAP_CANONICAL_DATABASE",
        "bootstrap_canonical_database",
        "drop_empty_canonical_database_and_roles",
    ),
    PlanStep(
        "I13_APPLY_FOUNDATION_0001",
        "apply_foundation_0001",
        "rollback_empty_foundation_0001",
    ),
    PlanStep(
        "I14_APPLY_OWNER_CLAIM_DETAIL_0003",
        "apply_owner_claim_detail_0003",
        "rollback_owner_claim_detail_0003",
    ),
    PlanStep(
        "I15_APPLY_PILOT_MARKER_0004",
        "apply_pilot_marker_0004_without_marker",
        "rollback_empty_pilot_marker_0004",
    ),
    PlanStep(
        "I16_CREATE_EMPTY_QDRANT_COLLECTION",
        "create_empty_qdrant_collection",
        "remove_exact_empty_qdrant_collection",
    ),
    PlanStep(
        "I17_CREATE_QDRANT_ALIAS",
        "create_qdrant_alias",
        "remove_exact_qdrant_alias",
    ),
    PlanStep(
        "I18_SEAL_RESOURCE_IDENTITY_LEDGER",
        "seal_resource_identity_ledger",
        "retain_attempt_resource_identity_ledger",
    ),
    PlanStep(
        "I19_INSTALL_AND_ENABLE_STORES_SUPERVISOR",
        "install_and_enable_stores_supervisor",
        "disable_and_remove_stores_supervisor",
    ),
    PlanStep(
        "I20_COLD_RESTART_AND_SEAL_INACTIVE_POSTFLIGHT",
        "cold_restart_and_seal_inactive_postflight",
        "requires_separate_signed_rollback_after_seal",
    ),
)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def validate_plan(plan: tuple[PlanStep, ...]) -> str:
    if not isinstance(plan, tuple) or len(plan) != 20:
        raise PlanValidationError("phase8b_plan_requires_exactly_20_steps")
    for position, step in enumerate(plan, start=1):
        if type(step) is not PlanStep:
            raise PlanValidationError("phase8b_plan_step_type_invalid")
        if not step.step_id.startswith(f"I{position:02d}_"):
            raise PlanValidationError("phase8b_plan_step_order_invalid")
        if ACTION_RE.fullmatch(step.effect) is None:
            raise PlanValidationError("phase8b_plan_action_invalid")
        if ACTION_RE.fullmatch(step.rollback) is None:
            raise PlanValidationError("phase8b_plan_compensation_invalid")
        if position < 20 and step.seals:
            raise PlanValidationError("phase8b_plan_compensation_invalid")
        if position == 20 and not step.seals:
            raise PlanValidationError("phase8b_plan_seal_invalid")
    expected_noncompensable = {
        "I01_REVERIFY_PRECLAIMED_EXECUTION_LOCK": "none",
        "I02_VERIFY_CLAIMED_EXECUTION_BINDING": (
            "retain_single_use_nonce_claim"
        ),
        "I03_VERIFY_LIVE_PREFLIGHT": "none",
        "I18_SEAL_RESOURCE_IDENTITY_LEDGER": (
            "retain_attempt_resource_identity_ledger"
        ),
        "I20_COLD_RESTART_AND_SEAL_INACTIVE_POSTFLIGHT": (
            "requires_separate_signed_rollback_after_seal"
        ),
    }
    for step in plan:
        if step.step_id in expected_noncompensable:
            if step.rollback != expected_noncompensable[step.step_id]:
                raise PlanValidationError("phase8b_plan_compensation_invalid")
        elif not step.compensable:
            raise PlanValidationError("phase8b_plan_compensation_invalid")
    if len({step.step_id for step in plan}) != 20:
        raise PlanValidationError("phase8b_plan_step_id_duplicate")
    document = plan_install_steps_projection(plan)
    return _sha256(_canonical_bytes(document))


def plan_install_steps_projection(
    plan: tuple[PlanStep, ...],
) -> list[dict[str, str]]:
    return [
        {"id": step.step_id, "effect": step.effect, "rollback": step.rollback}
        for step in plan
    ]


def plan_compensation_order(plan: tuple[PlanStep, ...]) -> list[str]:
    return [step.step_id for step in reversed(plan) if step.compensable]


@dataclass(frozen=True, slots=True)
class JournalRecord:
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
        event: JournalEvent,
        prior_record_sha256: str,
    ) -> JournalRecord:
        document = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "plan_sha256": plan_sha256,
            "attempt_id": attempt_id,
            "sequence": sequence,
            "step_id": step_id,
            "event": event.value,
            "prior_record_sha256": prior_record_sha256,
        }
        return cls(record_sha256=_sha256(_canonical_bytes(document)), **{
            key: value for key, value in document.items() if key != "schema_version"
        })

    def hash_input(self) -> dict[str, object]:
        return {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "plan_sha256": self.plan_sha256,
            "attempt_id": self.attempt_id,
            "sequence": self.sequence,
            "step_id": self.step_id,
            "event": self.event,
            "prior_record_sha256": self.prior_record_sha256,
        }


class HermeticControllerBackend(Protocol):
    execution_mode: str

    def journal_records(self) -> tuple[JournalRecord, ...]: ...

    def append_journal(self, record: JournalRecord) -> None: ...

    def probe(self, step: PlanStep) -> StepState: ...

    def apply(self, step: PlanStep) -> None: ...

    def compensate(self, step: PlanStep) -> None: ...


@dataclass(frozen=True, slots=True)
class ControllerReceipt:
    outcome: str
    plan_sha256: str
    attempt_id: str
    applied_step_ids: tuple[str, ...]
    compensated_step_ids: tuple[str, ...]
    journal_sequence: int
    journal_head_sha256: str


@dataclass(frozen=True, slots=True)
class _History:
    applied_step_ids: tuple[str, ...]
    intent_only_step_id: str | None
    compensation_started: bool
    compensation_intent_step_id: str | None
    compensated_step_ids: tuple[str, ...]
    compensation_complete: bool


class Phase8BStoresController:
    """Exact-order stores-only controller with no concrete executor."""

    def __init__(
        self,
        *,
        plan: tuple[PlanStep, ...],
        backend: HermeticControllerBackend,
        held_lock: HeldExecutionLockCapability,
    ) -> None:
        self.plan = plan
        self.plan_sha256 = validate_plan(plan)
        self.held_lock = held_lock
        self._require_lock()
        if getattr(backend, "execution_mode", None) != EXECUTION_MODE:
            raise BackendProtocolError("phase8b_hermetic_backend_required")
        for method in (
            "journal_records",
            "append_journal",
            "probe",
            "apply",
            "compensate",
        ):
            if not callable(getattr(backend, method, None)):
                raise BackendProtocolError("phase8b_backend_protocol_invalid")
        self.backend = backend

    def _require_lock(self) -> None:
        try:
            validate_held_execution_lock(self.held_lock)
        except (AttributeError, ExecutionLockError) as error:
            raise ControllerLockError("phase8b_global_lock_not_held") from error

    def run(self, *, attempt_id: str) -> ControllerReceipt:
        self._require_lock()
        if not isinstance(attempt_id, str) or ATTEMPT_RE.fullmatch(attempt_id) is None:
            raise JournalValidationError("phase8b_attempt_id_invalid")
        history = self._load_history(attempt_id)
        self._verify_known_states(history)
        if history.compensation_complete:
            raise CompletedStateError("phase8b_attempt_already_compensated")
        if history.compensation_started:
            return self._compensate(attempt_id, history)
        seal_id = self.plan[-1].step_id
        if seal_id in history.applied_step_ids:
            raise CompletedStateError("phase8b_inactive_postflight_already_sealed")
        history = self._recover_intent_after_effect(attempt_id, history)
        self._verify_untouched_states(history)

        current: PlanStep | None = None
        try:
            for step in self.plan:
                if step.step_id in history.applied_step_ids:
                    continue
                self._verify_known_states(history)
                self._verify_untouched_states(history)
                current = step
                if history.intent_only_step_id != step.step_id:
                    fresh_state = self._probe(step)
                    if fresh_state is not StepState.BEFORE:
                        if step.seals and fresh_state is StepState.AFTER:
                            raise CompletedStateError(
                                "phase8b_unowned_inactive_postflight_seal_present"
                            )
                        raise StateDriftError(
                            "phase8b_fresh_step_not_before:" + step.step_id
                        )
                    self._append(attempt_id, step.step_id, JournalEvent.INTENT)
                self._require_lock()
                self.backend.apply(step)
                self._require_lock()
                state = self._probe(step)
                if state is not StepState.AFTER:
                    raise StateDriftError(
                        "phase8b_step_effect_not_after:" + step.step_id
                    )
                self._append(attempt_id, step.step_id, JournalEvent.APPLIED)
                history = self._load_history(attempt_id)
            history = self._load_history(attempt_id)
            self._verify_known_states(history)
            return self._receipt(attempt_id, "inactive_stores_installation_complete")
        except ControllerError:
            raise
        except Exception as cause:
            if current is None:
                raise
            state = self._probe(current)
            if state is StepState.DRIFT:
                raise StateDriftError(
                    "phase8b_failed_step_state_drift:" + current.step_id
                ) from cause
            if state is StepState.RECOVERABLE:
                raise StateDriftError(
                    "phase8b_failed_step_recoverable_requires_exact_resume:"
                    + current.step_id
                ) from cause
            if current.seals and state is StepState.AFTER:
                raise CompletedStateError(
                    "phase8b_seal_effect_present_after_failure"
                ) from cause
            if state is StepState.AFTER:
                refreshed = self._load_history(attempt_id)
                if current.step_id not in refreshed.applied_step_ids:
                    self._append(
                        attempt_id,
                        current.step_id,
                        JournalEvent.APPLIED,
                    )
            try:
                self._append(
                    attempt_id,
                    ATTEMPT_STEP_ID,
                    JournalEvent.COMPENSATION_STARTED,
                )
                receipt = self._compensate(
                    attempt_id, self._load_history(attempt_id)
                )
            except ControllerError as compensation_error:
                raise CompensationFailedError(
                    cause, compensation_error
                ) from compensation_error
            except Exception as compensation_error:
                raise CompensationFailedError(
                    cause, compensation_error
                ) from compensation_error
            raise InstallationCompensatedError(cause, receipt) from cause

    def _probe(self, step: PlanStep) -> StepState:
        self._require_lock()
        state = self.backend.probe(step)
        self._require_lock()
        if type(state) is not StepState:
            raise BackendProtocolError(
                "phase8b_backend_probe_state_invalid:" + step.step_id
            )
        return state

    def _records(self) -> tuple[JournalRecord, ...]:
        self._require_lock()
        records = self.backend.journal_records()
        self._require_lock()
        if not isinstance(records, tuple) or any(
            type(record) is not JournalRecord for record in records
        ):
            raise JournalValidationError("phase8b_journal_record_type_invalid")
        return records

    def _append(
        self, attempt_id: str, step_id: str, event: JournalEvent
    ) -> None:
        before = self._records()
        prior = before[-1].record_sha256 if before else ZERO_HEAD
        record = JournalRecord.create(
            plan_sha256=self.plan_sha256,
            attempt_id=attempt_id,
            sequence=len(before) + 1,
            step_id=step_id,
            event=event,
            prior_record_sha256=prior,
        )
        self._require_lock()
        self.backend.append_journal(record)
        self._require_lock()
        after = self._records()
        if after != before + (record,):
            raise JournalValidationError("phase8b_journal_append_not_atomic")

    def _load_history(self, attempt_id: str) -> _History:
        records = self._records()
        prior = ZERO_HEAD
        valid_step_ids = {step.step_id for step in self.plan}
        for sequence, record in enumerate(records, start=1):
            if record.plan_sha256 != self.plan_sha256:
                raise JournalValidationError("phase8b_journal_plan_mismatch")
            if record.attempt_id != attempt_id:
                raise JournalValidationError("phase8b_journal_attempt_mismatch")
            if record.sequence != sequence:
                raise JournalValidationError("phase8b_journal_sequence_invalid")
            if record.prior_record_sha256 != prior:
                raise JournalValidationError("phase8b_journal_chain_invalid")
            if _sha256(_canonical_bytes(record.hash_input())) != record.record_sha256:
                raise JournalValidationError("phase8b_journal_record_hash_invalid")
            if HASH_RE.fullmatch(record.record_sha256) is None:
                raise JournalValidationError("phase8b_journal_record_hash_invalid")
            if record.step_id not in valid_step_ids | {ATTEMPT_STEP_ID}:
                raise JournalValidationError("phase8b_journal_step_invalid")
            try:
                JournalEvent(record.event)
            except ValueError as error:
                raise JournalValidationError(
                    "phase8b_journal_event_invalid"
                ) from error
            prior = record.record_sha256

        index = 0
        applied: list[str] = []
        intent_only: str | None = None
        for step in self.plan:
            if index >= len(records) or records[index].event != JournalEvent.INTENT.value:
                break
            if records[index].step_id != step.step_id:
                raise JournalValidationError("phase8b_install_step_order_invalid")
            index += 1
            if (
                index < len(records)
                and records[index].event == JournalEvent.APPLIED.value
            ):
                if records[index].step_id != step.step_id:
                    raise JournalValidationError(
                        "phase8b_install_event_step_mismatch"
                    )
                applied.append(step.step_id)
                index += 1
            else:
                intent_only = step.step_id
                break

        compensation_started = False
        if index < len(records):
            marker = records[index]
            if (
                marker.step_id != ATTEMPT_STEP_ID
                or marker.event != JournalEvent.COMPENSATION_STARTED.value
            ):
                raise JournalValidationError("phase8b_journal_phase_order_invalid")
            if self.plan[-1].step_id in applied:
                raise JournalValidationError("phase8b_compensation_after_seal")
            compensation_started = True
            index += 1

        compensated: list[str] = []
        compensation_intent: str | None = None
        if compensation_started:
            compensable_applied = [
                step.step_id
                for step in reversed(self.plan)
                if step.step_id in applied and step.compensable
            ]
            for step_id in compensable_applied:
                if index >= len(records):
                    break
                record = records[index]
                if record.event != JournalEvent.COMPENSATION_INTENT.value:
                    break
                if record.step_id != step_id:
                    raise JournalValidationError(
                        "phase8b_compensation_step_order_invalid"
                    )
                index += 1
                if (
                    index < len(records)
                    and records[index].event == JournalEvent.COMPENSATED.value
                ):
                    if records[index].step_id != step_id:
                        raise JournalValidationError(
                            "phase8b_compensation_event_step_mismatch"
                        )
                    compensated.append(step_id)
                    index += 1
                else:
                    compensation_intent = step_id
                    break

        compensation_complete = False
        if index < len(records):
            marker = records[index]
            if (
                marker.step_id != ATTEMPT_STEP_ID
                or marker.event != JournalEvent.COMPENSATION_COMPLETE.value
                or tuple(compensated)
                != tuple(
                    step.step_id
                    for step in reversed(self.plan)
                    if step.step_id in applied and step.compensable
                )
            ):
                raise JournalValidationError("phase8b_compensation_terminal_invalid")
            compensation_complete = True
            index += 1
        if index != len(records):
            raise JournalValidationError("phase8b_journal_trailing_records")
        return _History(
            applied_step_ids=tuple(applied),
            intent_only_step_id=intent_only,
            compensation_started=compensation_started,
            compensation_intent_step_id=compensation_intent,
            compensated_step_ids=tuple(compensated),
            compensation_complete=compensation_complete,
        )

    def _verify_known_states(self, history: _History) -> None:
        compensated = set(history.compensated_step_ids)
        for step in self.plan:
            state = self._probe(step)
            if state is StepState.DRIFT:
                raise StateDriftError("phase8b_step_state_drift:" + step.step_id)
            if state is StepState.RECOVERABLE:
                exact_install_intent = (
                    not history.compensation_started
                    and step.step_id == history.intent_only_step_id
                )
                exact_compensation_intent = (
                    history.compensation_started
                    and step.step_id == history.compensation_intent_step_id
                )
                if exact_install_intent or exact_compensation_intent:
                    continue
                raise StateDriftError(
                    "phase8b_recoverable_without_exact_current_intent:"
                    + step.step_id
                )
            if step.step_id in compensated:
                if state is not StepState.BEFORE:
                    raise StateDriftError(
                        "phase8b_compensated_step_not_before:" + step.step_id
                    )
            elif (
                history.compensation_started
                and step.step_id == history.compensation_intent_step_id
            ):
                # AFTER means compensation has not run; BEFORE means it ran
                # and the crash occurred before COMPENSATED was appended.
                if state not in {StepState.AFTER, StepState.BEFORE}:
                    raise StateDriftError(
                        "phase8b_compensation_intent_state_invalid:"
                        + step.step_id
                    )
            elif step.step_id in history.applied_step_ids:
                if state is not StepState.AFTER:
                    raise StateDriftError(
                        "phase8b_applied_step_not_after:" + step.step_id
                    )
            elif (
                history.compensation_started
                and step.step_id == history.intent_only_step_id
                and state is not StepState.BEFORE
            ):
                raise StateDriftError(
                    "phase8b_failed_unapplied_step_not_before:" + step.step_id
                )
            elif (
                history.compensation_started
                and step.step_id not in history.applied_step_ids
                and state is not StepState.BEFORE
            ):
                raise StateDriftError(
                    "phase8b_unowned_effect_during_compensation:" + step.step_id
                )

    def _recover_intent_after_effect(
        self, attempt_id: str, history: _History
    ) -> _History:
        if history.intent_only_step_id is None:
            return history
        step = next(
            item for item in self.plan if item.step_id == history.intent_only_step_id
        )
        state = self._probe(step)
        if state is StepState.AFTER:
            if step.seals:
                raise CompletedStateError("phase8b_unjournaled_seal_effect_present")
            self._append(attempt_id, step.step_id, JournalEvent.APPLIED)
            return self._load_history(attempt_id)
        if state is StepState.RECOVERABLE:
            # The exact durable intent permits the backend to re-enter only
            # this step.  No APPLIED record is synthesized for a composite
            # or partially observed effect.
            return history
        if state is not StepState.BEFORE:
            raise StateDriftError("phase8b_intent_state_invalid:" + step.step_id)
        return history

    def _verify_untouched_states(self, history: _History) -> None:
        touched = set(history.applied_step_ids)
        if history.intent_only_step_id is not None:
            touched.add(history.intent_only_step_id)
        for step in self.plan:
            if step.step_id in touched:
                continue
            state = self._probe(step)
            if state is StepState.DRIFT:
                raise StateDriftError("phase8b_step_state_drift:" + step.step_id)
            if state is not StepState.BEFORE:
                if step.seals:
                    raise CompletedStateError(
                        "phase8b_unowned_inactive_postflight_seal_present"
                    )
                raise StateDriftError(
                    "phase8b_unowned_preexisting_effect:" + step.step_id
                )

    def _compensate(
        self, attempt_id: str, history: _History
    ) -> ControllerReceipt:
        if not history.compensation_started:
            raise CompensationBlockedError("phase8b_compensation_not_started")
        history = self._load_history(attempt_id)
        self._verify_known_states(history)
        seal_state = self._probe(self.plan[-1])
        if seal_state is not StepState.BEFORE:
            raise CompensationBlockedError(
                "phase8b_compensation_blocked_by_seal_or_drift"
            )
        compensated = set(history.compensated_step_ids)
        step_by_id = {step.step_id: step for step in self.plan}
        for step_id in reversed(history.applied_step_ids):
            self._verify_known_states(history)
            step = step_by_id[step_id]
            if step.seals:
                raise CompensationBlockedError(
                    "phase8b_compensation_after_seal_refused"
                )
            if not step.compensable:
                continue
            if step_id in compensated:
                continue
            state = self._probe(step)
            if state is StepState.DRIFT:
                raise CompensationBlockedError(
                    "phase8b_compensation_state_drift:" + step_id
                )
            if history.compensation_intent_step_id != step_id:
                self._append(
                    attempt_id, step_id, JournalEvent.COMPENSATION_INTENT
                )
            if state in {StepState.AFTER, StepState.RECOVERABLE}:
                self._require_lock()
                self.backend.compensate(step)
                self._require_lock()
                state = self._probe(step)
            if state is not StepState.BEFORE:
                raise CompensationBlockedError(
                    "phase8b_compensation_effect_not_before:" + step_id
                )
            self._append(attempt_id, step_id, JournalEvent.COMPENSATED)
            history = self._load_history(attempt_id)
            self._verify_known_states(history)
            compensated.add(step_id)
        history = self._load_history(attempt_id)
        self._verify_known_states(history)
        self._append(
            attempt_id, ATTEMPT_STEP_ID, JournalEvent.COMPENSATION_COMPLETE
        )
        return self._receipt(attempt_id, "same_attempt_compensation_complete")

    def _receipt(self, attempt_id: str, outcome: str) -> ControllerReceipt:
        history = self._load_history(attempt_id)
        records = self._records()
        return ControllerReceipt(
            outcome=outcome,
            plan_sha256=self.plan_sha256,
            attempt_id=attempt_id,
            applied_step_ids=history.applied_step_ids,
            compensated_step_ids=history.compensated_step_ids,
            journal_sequence=len(records),
            journal_head_sha256=(
                records[-1].record_sha256 if records else ZERO_HEAD
            ),
        )
