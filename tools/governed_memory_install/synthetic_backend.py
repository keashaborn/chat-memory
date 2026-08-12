from __future__ import annotations

"""Sealed, in-memory backend for the dormant-store installation disposable proof.

This backend has no host adapter, command runner, socket, environment, secret,
installation, or activation surface.  It models one deterministic fault at a
time so the dormant-store installation controller algorithm can be exercised without claiming a
process-crash, durable-recovery, composite-fault, or live-system proof.
"""

from collections import Counter
from dataclasses import dataclass
from typing import Final

from .controller import (
    EXECUTION_MODE,
    JournalEvent,
    JournalRecord,
    PlanStep,
    STORES_ONLY_PLAN,
    StepState,
)


SYNTHETIC_BACKEND_SCHEMA_VERSION: Final = (
    "governed-memory-dormant_store_install-sealed-synthetic-backend-v1"
)

FAULT_NONE: Final = "none"
FAULT_FAIL_BEFORE_EFFECT: Final = "fail_before_effect"
FAULT_FAIL_AFTER_EFFECT: Final = "fail_after_effect"
FAULT_INTERRUPT_BEFORE_EFFECT: Final = "interrupt_before_effect"
FAULT_INTERRUPT_AFTER_EFFECT: Final = "interrupt_after_effect"
FAULT_INTERRUPT_AFTER_APPLIED_JOURNAL: Final = (
    "interrupt_after_applied_journal"
)
FAULT_INTERRUPT_BEFORE_COMPENSATION: Final = (
    "interrupt_before_compensation"
)
FAULT_INTERRUPT_AFTER_COMPENSATION: Final = (
    "interrupt_after_compensation"
)

ALLOWED_FAULT_POINTS: Final = frozenset(
    {
        FAULT_NONE,
        FAULT_FAIL_BEFORE_EFFECT,
        FAULT_FAIL_AFTER_EFFECT,
        FAULT_INTERRUPT_BEFORE_EFFECT,
        FAULT_INTERRUPT_AFTER_EFFECT,
        FAULT_INTERRUPT_AFTER_APPLIED_JOURNAL,
        FAULT_INTERRUPT_BEFORE_COMPENSATION,
        FAULT_INTERRUPT_AFTER_COMPENSATION,
    }
)

_COMPENSATION_FAULTS: Final = frozenset(
    {
        FAULT_INTERRUPT_BEFORE_COMPENSATION,
        FAULT_INTERRUPT_AFTER_COMPENSATION,
    }
)
_EFFECT_FAULTS: Final = ALLOWED_FAULT_POINTS - _COMPENSATION_FAULTS
_PLAN_STEP_IDS: Final = tuple(step.step_id for step in STORES_ONLY_PLAN)
_COMPENSABLE_STEP_IDS: Final = tuple(
    step.step_id for step in STORES_ONLY_PLAN if step.compensable
)
_CONSTRUCTION_SEAL = object()


class SyntheticBackendError(RuntimeError):
    """Content-free refusal raised by the sealed synthetic backend."""


class SyntheticEffectFailure(RuntimeError):
    """Ordinary modeled effect failure caught by the controller."""


class SyntheticInterruption(BaseException):
    """In-process interruption marker deliberately not caught as Exception.

    This is not an operating-system process crash and never claims to be one.
    """


@dataclass(frozen=True, slots=True)
class SyntheticScenario:
    scenario_id: str
    family: str
    fault_point: str
    target_step_id: str | None
    trigger_failure_step_id: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.scenario_id, str)
            or not self.scenario_id
            or len(self.scenario_id) > 160
            or not self.scenario_id.isascii()
            or not isinstance(self.family, str)
            or not self.family
            or len(self.family) > 80
            or not self.family.isascii()
            or self.fault_point not in ALLOWED_FAULT_POINTS
        ):
            raise SyntheticBackendError("synthetic_scenario_invalid")
        if self.fault_point == FAULT_NONE:
            if self.target_step_id is not None or self.trigger_failure_step_id is not None:
                raise SyntheticBackendError("synthetic_scenario_invalid")
            return
        if self.target_step_id not in _PLAN_STEP_IDS:
            raise SyntheticBackendError("synthetic_target_step_invalid")
        if self.fault_point in _COMPENSATION_FAULTS:
            if (
                self.target_step_id not in _COMPENSABLE_STEP_IDS
                or self.trigger_failure_step_id not in _PLAN_STEP_IDS
            ):
                raise SyntheticBackendError(
                    "synthetic_compensation_scenario_invalid"
                )
            target_position = _PLAN_STEP_IDS.index(self.target_step_id)
            trigger_position = _PLAN_STEP_IDS.index(
                self.trigger_failure_step_id
            )
            if target_position >= trigger_position:
                raise SyntheticBackendError(
                    "synthetic_compensation_scenario_invalid"
                )
        elif (
            self.fault_point not in _EFFECT_FAULTS
            or self.trigger_failure_step_id is not None
        ):
            raise SyntheticBackendError("synthetic_scenario_invalid")

    def projection(self) -> dict[str, str | None]:
        return {
            "scenario_id": self.scenario_id,
            "family": self.family,
            "fault_point": self.fault_point,
            "target_step_id": self.target_step_id,
            "trigger_failure_step_id": self.trigger_failure_step_id,
        }


class SyntheticBackend:
    """Exact hermetic backend; construction is restricted to this module."""

    execution_mode: Final = EXECUTION_MODE
    schema_version: Final = SYNTHETIC_BACKEND_SCHEMA_VERSION

    def __init__(
        self,
        scenario: SyntheticScenario,
        *,
        _seal: object | None = None,
    ) -> None:
        if _seal is not _CONSTRUCTION_SEAL or type(scenario) is not SyntheticScenario:
            raise SyntheticBackendError(
                "synthetic_backend_construction_not_authorized"
            )
        self._scenario = scenario
        self._records: list[JournalRecord] = []
        self._states = {
            step.step_id: StepState.BEFORE for step in STORES_ONLY_PLAN
        }
        self._counts: Counter[str] = Counter()
        self._fault_fired = False
        self._trigger_failure_fired = False

    @property
    def scenario(self) -> SyntheticScenario:
        return self._scenario

    @property
    def fault_fired(self) -> bool:
        return self._fault_fired

    @property
    def operation_counts(self) -> dict[str, int]:
        return {
            key: self._counts[key]
            for key in (
                "journal_append",
                "probe",
                "apply",
                "compensate",
                "ordinary_failure",
                "interruption",
            )
        }

    @property
    def state_projection(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (step_id, self._states[step_id].value)
            for step_id in _PLAN_STEP_IDS
        )

    def journal_records(self) -> tuple[JournalRecord, ...]:
        return tuple(self._records)

    def append_journal(self, record: JournalRecord) -> None:
        if type(record) is not JournalRecord:
            raise SyntheticBackendError("synthetic_journal_record_invalid")
        self._counts["journal_append"] += 1
        self._records.append(record)
        if (
            not self._fault_fired
            and self._scenario.fault_point
            == FAULT_INTERRUPT_AFTER_APPLIED_JOURNAL
            and record.event == JournalEvent.APPLIED.value
            and record.step_id == self._scenario.target_step_id
        ):
            self._fault_fired = True
            self._counts["interruption"] += 1
            raise SyntheticInterruption(
                "synthetic_in_process_interruption_after_applied_journal"
            )

    def probe(self, step: PlanStep) -> StepState:
        self._validate_step(step)
        self._counts["probe"] += 1
        return self._states[step.step_id]

    def apply(self, step: PlanStep) -> None:
        self._validate_step(step)
        self._counts["apply"] += 1
        if (
            not self._trigger_failure_fired
            and self._scenario.trigger_failure_step_id == step.step_id
        ):
            self._trigger_failure_fired = True
            self._counts["ordinary_failure"] += 1
            raise SyntheticEffectFailure(
                "synthetic_compensation_trigger_failure"
            )
        if not self._fault_fired and self._scenario.target_step_id == step.step_id:
            if self._scenario.fault_point == FAULT_FAIL_BEFORE_EFFECT:
                self._fire_failure("synthetic_failure_before_effect")
            if self._scenario.fault_point == FAULT_INTERRUPT_BEFORE_EFFECT:
                self._fire_interruption(
                    "synthetic_in_process_interruption_before_effect"
                )
        self._states[step.step_id] = StepState.AFTER
        if not self._fault_fired and self._scenario.target_step_id == step.step_id:
            if self._scenario.fault_point == FAULT_FAIL_AFTER_EFFECT:
                self._fire_failure("synthetic_failure_after_effect")
            if self._scenario.fault_point == FAULT_INTERRUPT_AFTER_EFFECT:
                self._fire_interruption(
                    "synthetic_in_process_interruption_after_effect"
                )

    def compensate(self, step: PlanStep) -> None:
        self._validate_step(step)
        if not step.compensable:
            raise SyntheticBackendError(
                "synthetic_noncompensable_step_refused"
            )
        self._counts["compensate"] += 1
        if not self._fault_fired and self._scenario.target_step_id == step.step_id:
            if (
                self._scenario.fault_point
                == FAULT_INTERRUPT_BEFORE_COMPENSATION
            ):
                self._fire_interruption(
                    "synthetic_in_process_interruption_before_compensation"
                )
        self._states[step.step_id] = StepState.BEFORE
        if not self._fault_fired and self._scenario.target_step_id == step.step_id:
            if (
                self._scenario.fault_point
                == FAULT_INTERRUPT_AFTER_COMPENSATION
            ):
                self._fire_interruption(
                    "synthetic_in_process_interruption_after_compensation"
                )

    def _fire_failure(self, code: str) -> None:
        self._fault_fired = True
        self._counts["ordinary_failure"] += 1
        raise SyntheticEffectFailure(code)

    def _fire_interruption(self, code: str) -> None:
        self._fault_fired = True
        self._counts["interruption"] += 1
        raise SyntheticInterruption(code)

    @staticmethod
    def _validate_step(step: PlanStep) -> None:
        if type(step) is not PlanStep or step.step_id not in _PLAN_STEP_IDS:
            raise SyntheticBackendError("synthetic_step_invalid")


def _construct_exact_synthetic_backend(
    scenario: SyntheticScenario,
) -> SyntheticBackend:
    """Construct the sole backend accepted by the disposable harness."""

    return SyntheticBackend(scenario, _seal=_CONSTRUCTION_SEAL)
