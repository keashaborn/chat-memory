from __future__ import annotations

"""Deterministic disposable backend for Phase 8A controller proof."""

from collections import Counter
from dataclasses import replace

from .controller import (
    ControllerInterruption,
    EXECUTION_MODE,
    INSTALL_STEPS,
    RETAINED_INSTALL_EFFECTS,
    ROLLBACK_PREREQUISITES,
    ROLLBACK_STEPS,
    RollbackFacts,
    StepState,
    _SYNTHETIC_BACKEND_SEAL,
)


class SyntheticEffectFailure(RuntimeError):
    pass


class SyntheticBackend:
    """In-memory state model; it cannot address a host or provider."""

    controller_mode = EXECUTION_MODE
    _phase8a_synthetic_seal = _SYNTHETIC_BACKEND_SEAL

    def __init__(
        self,
        *,
        crash_after_intent_step: str | None = None,
        crash_after_effect_step: str | None = None,
        crash_after_journal_commit_step: str | None = None,
        fail_effect_step: str | None = None,
        fail_after_effect_step: str | None = None,
        rollback_facts: RollbackFacts | None = None,
    ) -> None:
        self.crash_after_intent_step = crash_after_intent_step
        self.crash_after_effect_step = crash_after_effect_step
        self.crash_after_journal_commit_step = crash_after_journal_commit_step
        self.fail_effect_step = fail_effect_step
        self.fail_after_effect_step = fail_after_effect_step
        self._rollback_facts = rollback_facts or RollbackFacts()
        self._states: dict[tuple[str, str], StepState] = {}
        self._probe_overrides: dict[tuple[str, str], StepState] = {}
        self._consumed_crashes: set[tuple[str, str, str]] = set()
        self.effects: list[tuple[str, str]] = []
        self.checkpoints: list[tuple[str, str, str]] = []
        self.execution_counts: Counter[tuple[str, str]] = Counter()
        self._applied_install_effects: set[str] = set()
        self._root_resources: set[str] = set()
        self.provider_calls = 0
        self.source_postgresql_actions = 0
        self.runtime_application_starts = 0
        self.network_calls = 0

    # Verification-only effects are discharged when their underlying exact
    # resources are removed.  The final postflight receipt, exact named roots,
    # legacy-secret quarantine, and service identity are deliberately retained.
    _ROLLBACK_CLEARS = {
        **ROLLBACK_PREREQUISITES,
        "R03_REMOVE_HTTP_UNIT": (
            *ROLLBACK_PREREQUISITES["R03_REMOVE_HTTP_UNIT"],
            "I25_DAEMON_RELOAD_VERIFY_APP_UNITS_DORMANT",
        ),
        "R12_STOP_REMOVE_POSTGRES_CONTAINER": (
            *ROLLBACK_PREREQUISITES["R12_STOP_REMOVE_POSTGRES_CONTAINER"],
            "I14_START_VERIFY_STORES",
            "I27_COLD_RESTART_SAME_VOLUMES_PROOF",
        ),
    }
    _RETAINED_AFTER_ROLLBACK = frozenset(
        (*RETAINED_INSTALL_EFFECTS, "I29_SEAL_INACTIVE_POSTFLIGHT")
    )
    _ROOTS_CREATED_BY_I06 = frozenset(
        {
            "install_root",
            "environment_root",
            "runtime_environment_root",
            "state_root",
            "backup_root",
        }
    )
    _ROOTS_RETAINED_AFTER_ROLLBACK = _ROOTS_CREATED_BY_I06

    def clone(self) -> SyntheticBackend:
        clone = SyntheticBackend(rollback_facts=self._rollback_facts)
        clone.crash_after_intent_step = self.crash_after_intent_step
        clone.crash_after_effect_step = self.crash_after_effect_step
        clone.crash_after_journal_commit_step = (
            self.crash_after_journal_commit_step
        )
        clone.fail_effect_step = self.fail_effect_step
        clone.fail_after_effect_step = self.fail_after_effect_step
        clone._states = dict(self._states)
        clone._probe_overrides = dict(self._probe_overrides)
        clone._consumed_crashes = set(self._consumed_crashes)
        clone.effects = list(self.effects)
        clone.checkpoints = list(self.checkpoints)
        clone.execution_counts = Counter(self.execution_counts)
        clone._applied_install_effects = set(self._applied_install_effects)
        clone._root_resources = set(self._root_resources)
        return clone

    def clear_faults(self) -> None:
        self.crash_after_intent_step = None
        self.crash_after_effect_step = None
        self.crash_after_journal_commit_step = None
        self.fail_effect_step = None
        self.fail_after_effect_step = None

    def probe(self, operation: str, step_id: str) -> StepState:
        self._validate_step(operation, step_id)
        override = self._probe_overrides.get((operation, step_id))
        if override is not None:
            return override
        return self._states.get((operation, step_id), StepState.BEFORE)

    def execute(self, operation: str, step_id: str) -> None:
        self._validate_step(operation, step_id)
        key = (operation, step_id)
        if self.probe(operation, step_id) is not StepState.BEFORE:
            raise SyntheticEffectFailure("synthetic_effect_not_before")
        if self.fail_effect_step == step_id:
            self.fail_effect_step = None
            raise SyntheticEffectFailure("synthetic_effect_failure:" + step_id)
        if operation == "install":
            self._applied_install_effects.add(step_id)
            if step_id == "I06_CREATE_OWNED_ROOTS":
                self._root_resources.update(self._ROOTS_CREATED_BY_I06)
        elif step_id == "R20_VERIFY_FINAL_ABSENCE":
            remaining = self.resources_remaining
            if remaining:
                raise SyntheticEffectFailure(
                    "synthetic_candidate_resources_remain:"
                    + ",".join(remaining)
                )
        else:
            for install_step in self._ROLLBACK_CLEARS.get(step_id, ()):
                self._applied_install_effects.discard(install_step)
                self._states[("install", install_step)] = StepState.BEFORE
        self._states[key] = StepState.AFTER
        self.execution_counts[key] += 1
        self.effects.append(key)

    def checkpoint(self, operation: str, step_id: str, event: str) -> None:
        self._validate_step(operation, step_id)
        if event not in {
            "after_intent",
            "after_effect",
            "after_journal_commit",
        }:
            raise SyntheticEffectFailure("synthetic_checkpoint_invalid")
        self.checkpoints.append((operation, step_id, event))
        if event == "after_effect" and self.fail_after_effect_step == step_id:
            self.fail_after_effect_step = None
            raise SyntheticEffectFailure(
                "synthetic_post_effect_failure:" + step_id
            )
        configured = {
            "after_intent": self.crash_after_intent_step,
            "after_effect": self.crash_after_effect_step,
            "after_journal_commit": self.crash_after_journal_commit_step,
        }[event]
        crash_key = (operation, step_id, event)
        if configured == step_id and crash_key not in self._consumed_crashes:
            self._consumed_crashes.add(crash_key)
            raise ControllerInterruption(
                "synthetic_process_death:" + operation + ":" + step_id + ":" + event
            )

    def rollback_facts(self) -> RollbackFacts:
        return self._rollback_facts

    def set_rollback_facts(self, **changes: object) -> None:
        allowed = set(RollbackFacts.__dataclass_fields__)
        if set(changes) - allowed:
            raise ValueError("synthetic_rollback_fact_invalid")
        self._rollback_facts = replace(self._rollback_facts, **changes)

    def set_probe_override(
        self,
        operation: str,
        step_id: str,
        state: StepState | str,
    ) -> None:
        self._validate_step(operation, step_id)
        try:
            normalized = state if isinstance(state, StepState) else StepState(state)
        except ValueError as error:
            raise ValueError("synthetic_probe_state_invalid") from error
        self._probe_overrides[(operation, step_id)] = normalized

    def clear_probe_override(self, operation: str, step_id: str) -> None:
        self._probe_overrides.pop((operation, step_id), None)

    def seed_unowned_effect(self, operation: str, step_id: str) -> None:
        self._validate_step(operation, step_id)
        self._states[(operation, step_id)] = StepState.AFTER

    def effect_count(self, operation: str, step_id: str) -> int:
        return self.execution_counts[(operation, step_id)]

    @property
    def resources_remaining(self) -> tuple[str, ...]:
        effects = {
            "install_effect:" + step_id
            for step_id in (
                self._applied_install_effects
                - self._RETAINED_AFTER_ROLLBACK
            )
        }
        roots = {
            "root:" + root
            for root in (
                self._root_resources
                - self._ROOTS_RETAINED_AFTER_ROLLBACK
            )
        }
        return tuple(sorted(effects | roots))

    @property
    def retained_resources(self) -> tuple[str, ...]:
        resources = {
            "root:" + root
            for root in (
                self._root_resources
                & self._ROOTS_RETAINED_AFTER_ROLLBACK
            )
        }
        if "I04_QUARANTINE_LEGACY_SECRET" in self._applied_install_effects:
            resources.add("legacy_secret_quarantine")
        if "I05_CREATE_SERVICE_IDENTITY" in self._applied_install_effects:
            resources.add("service_identity")
        if "I29_SEAL_INACTIVE_POSTFLIGHT" in self._applied_install_effects:
            resources.add("inactive_postflight_receipt")
        return tuple(sorted(resources))

    @property
    def retained_effects(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                self._applied_install_effects
                & self._RETAINED_AFTER_ROLLBACK
            )
        )

    @staticmethod
    def _validate_step(operation: str, step_id: str) -> None:
        if operation == "install":
            allowed = INSTALL_STEPS
        elif operation in {"rollback", "compensation"}:
            allowed = ROLLBACK_STEPS
        else:
            raise SyntheticEffectFailure("synthetic_operation_invalid")
        if step_id not in allowed:
            raise SyntheticEffectFailure("synthetic_step_invalid")
