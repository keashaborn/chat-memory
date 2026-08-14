from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest

from tools.governed_memory_install.controller import (
    ATTEMPT_STEP_ID,
    CompletedStateError,
    CompensationFailedError,
    ControllerLockError,
    EXECUTION_MODE,
    InstallationCompensatedError,
    JournalEvent,
    JournalRecord,
    JournalValidationError,
    DormantStoreInstallController,
    PlanStep,
    PlanValidationError,
    StateDriftError,
    StepState,
    STORES_ONLY_PLAN,
    plan_compensation_order,
    plan_install_steps_projection,
    validate_plan,
)
from tools.governed_memory_install.execution_lock import GlobalExecutionLock
from tools.governed_memory_install.package import EXPECTED_LIVE_EXECUTION


ROOT = Path(__file__).resolve().parents[2]


class _HermeticBackend:
    execution_mode = EXECUTION_MODE

    def __init__(self) -> None:
        self.records: list[JournalRecord] = []
        self.states = {
            step.step_id: (
                StepState.AFTER if step.invariant_only else StepState.BEFORE
            )
            for step in STORES_ONLY_PLAN
        }
        self.actions: list[tuple[str, str]] = []
        self.fail_before_step: str | None = None
        self.fail_after_step: str | None = None
        self.fail_compensation_once_step: str | None = None
        self.fail_after_compensation_once_step: str | None = None
        self.fail_after_appending_event: JournalEvent | None = None
        self.inject_after_apply: tuple[str, str] | None = None
        self.inject_after_compensate: tuple[str, str] | None = None
        self.drift_after_apply: tuple[str, str] | None = None
        self.probe_calls: list[str] = []

    def journal_records(self) -> tuple[JournalRecord, ...]:
        return tuple(self.records)

    def append_journal(self, record: JournalRecord) -> None:
        self.records.append(record)
        if record.event == (
            self.fail_after_appending_event.value
            if self.fail_after_appending_event is not None
            else None
        ):
            self.fail_after_appending_event = None
            raise RuntimeError("synthetic_post_journal_append_failure")

    def probe(self, step: PlanStep) -> StepState:
        self.probe_calls.append(step.step_id)
        if any(
            self.states[required] is not StepState.AFTER
            for required in step.observation_requires
        ):
            raise AssertionError(
                "synthetic_dependency_unavailable:" + step.step_id
            )
        if step.proof_lifetime == "pre_effect_barrier" and any(
            self.states[candidate.step_id] is not StepState.BEFORE
            for candidate in STORES_ONLY_PLAN
            if candidate.proof_lifetime == "mutation"
        ):
            return StepState.DRIFT
        if step.proof_lifetime == "until_compensation" and any(
            self.states[candidate.step_id] is not StepState.AFTER
            for candidate in STORES_ONLY_PLAN[3:16]
        ):
            return StepState.DRIFT
        return self.states[step.step_id]

    def apply(self, step: PlanStep) -> None:
        self.actions.append(("apply", step.step_id))
        if self.fail_before_step == step.step_id:
            self.fail_before_step = None
            raise RuntimeError("synthetic_pre_effect_failure")
        expected = (
            StepState.AFTER if step.invariant_only else StepState.BEFORE
        )
        if self.states[step.step_id] is not expected:
            raise RuntimeError("synthetic_effect_already_present")
        if not step.invariant_only:
            self.states[step.step_id] = StepState.AFTER
        if self.inject_after_apply is not None and (
            self.inject_after_apply[0] == step.step_id
        ):
            self.states[self.inject_after_apply[1]] = StepState.AFTER
            self.inject_after_apply = None
        if self.drift_after_apply is not None and (
            self.drift_after_apply[0] == step.step_id
        ):
            self.states[self.drift_after_apply[1]] = StepState.BEFORE
            self.drift_after_apply = None
        if self.fail_after_step == step.step_id:
            self.fail_after_step = None
            raise RuntimeError("synthetic_post_effect_failure")

    def compensate(self, step: PlanStep) -> None:
        self.actions.append(("compensate", step.step_id))
        if self.fail_compensation_once_step == step.step_id:
            self.fail_compensation_once_step = None
            raise RuntimeError("synthetic_compensation_failure")
        self.states[step.step_id] = StepState.BEFORE
        if self.fail_after_compensation_once_step == step.step_id:
            self.fail_after_compensation_once_step = None
            raise RuntimeError("synthetic_post_compensation_failure")
        if self.inject_after_compensate is not None and (
            self.inject_after_compensate[0] == step.step_id
        ):
            self.states[self.inject_after_compensate[1]] = StepState.AFTER
            self.inject_after_compensate = None


class DormantStoreInstallControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        lock_directory = Path(self._temporary.name) / "controller-lock"
        lock_directory.mkdir(mode=0o700)
        os.chmod(lock_directory, 0o700)
        self.execution_lock = GlobalExecutionLock(
            lock_directory / "execution.lock"
        )

    def tearDown(self) -> None:
        self.execution_lock.close()
        self._temporary.cleanup()

    def _controller(
        self, backend: _HermeticBackend | None = None
    ) -> tuple[DormantStoreInstallController, _HermeticBackend]:
        selected = backend or _HermeticBackend()
        return (
            DormantStoreInstallController(
                plan=STORES_ONLY_PLAN,
                backend=selected,
                held_lock=self.execution_lock.held_capability(),
            ),
            selected,
        )

    def test_exact_19_step_stores_only_plan_and_no_live_executor(self) -> None:
        self.assertEqual(len(STORES_ONLY_PLAN), 19)
        self.assertEqual(
            tuple(step.step_id[:3] for step in STORES_ONLY_PLAN),
            tuple(f"I{position:02d}" for position in range(1, 20)),
        )
        self.assertTrue(STORES_ONLY_PLAN[-1].terminal_postflight)
        self.assertFalse(STORES_ONLY_PLAN[-1].compensable)
        self.assertEqual(
            STORES_ONLY_PLAN[3],
            PlanStep(
                "I04_WRITE_RESOLVED_STORE_SPEC_AND_GENERATE_FRESH_STORE_SECRETS",
                "write_resolved_store_spec_and_generate_fresh_store_secrets",
                "remove_resolved_store_spec_and_fresh_store_secrets",
            ),
        )
        self.assertEqual(
            STORES_ONLY_PLAN[-1].step_id,
            "I19_COLD_RESTART_AND_VERIFY_TERMINAL_POSTFLIGHT",
        )
        joined = "\n".join(step.effect for step in STORES_ONLY_PLAN)
        for forbidden in (
            "legacy",
            "source_cluster",
            "conversation",
            "memory_runtime",
            "http_service",
            "worker_service",
            "provider",
            "route",
            "supabase",
            "controller_release",
            "stage_immutable_controller_release",
        ):
            self.assertNotIn(forbidden, joined)

        plan_document = json.loads(
            (
                ROOT
                / "ops/governed_memory/installation/current/controller_plan.json"
            ).read_text(encoding="ascii")
        )
        self.assertEqual(
            plan_document["install_steps"],
            plan_install_steps_projection(STORES_ONLY_PLAN),
        )
        self.assertEqual(
            plan_document["same_attempt_compensation_order"],
            plan_compensation_order(STORES_ONLY_PLAN),
        )
        self.assertIs(
            plan_document["execution_invariants"][
                "durable_journal_and_anchor_adapter_packaged"
            ],
            True,
        )
        self.assertIs(
            plan_document["execution_invariants"][
                "durable_process_crash_recovery_proven"
            ],
            False,
        )
        self.assertEqual(
            plan_document["live_execution"],
            EXPECTED_LIVE_EXECUTION,
        )

        source = (
            ROOT / "tools/governed_memory_install/controller.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "import subprocess",
            "import socket",
            "import requests",
            "import urllib",
            "import psycopg",
            "import docker",
            "systemctl",
            "docker run",
        ):
            self.assertNotIn(forbidden, source)

    def test_install_runs_exact_order_and_exact_completed_resume_is_idempotent(self) -> None:
        controller, backend = self._controller()
        receipt = controller.run(attempt_id="dormant_store_install-happy")
        expected = tuple(step.step_id for step in STORES_ONLY_PLAN)
        self.assertEqual(receipt.applied_step_ids, expected)
        self.assertEqual(receipt.compensated_step_ids, ())
        self.assertEqual(
            tuple(step_id for action, step_id in backend.actions if action == "apply"),
            expected,
        )
        self.assertEqual(receipt.journal_sequence, 38)
        resumed = controller.run(attempt_id="dormant_store_install-happy")
        self.assertEqual(resumed, receipt)
        self.assertEqual(len(backend.actions), 19)

    def test_failure_compensates_owned_effects_in_exact_reverse_order(self) -> None:
        backend = _HermeticBackend()
        backend.fail_after_step = STORES_ONLY_PLAN[7].step_id
        controller, backend = self._controller(backend)
        with self.assertRaises(InstallationCompensatedError) as raised:
            controller.run(attempt_id="dormant_store_install-compensate")
        expected_applied = tuple(step.step_id for step in STORES_ONLY_PLAN[:8])
        expected_reverse = tuple(
            step.step_id
            for step in reversed(STORES_ONLY_PLAN[:8])
            if step.compensable
        )
        self.assertEqual(raised.exception.receipt.applied_step_ids, expected_applied)
        self.assertEqual(
            raised.exception.receipt.compensated_step_ids, expected_reverse
        )
        self.assertEqual(
            tuple(
                step_id
                for action, step_id in backend.actions
                if action == "compensate"
            ),
            expected_reverse,
        )
        for step in STORES_ONLY_PLAN[:8]:
            expected_state = (
                StepState.BEFORE if step.compensable else StepState.AFTER
            )
            self.assertIs(backend.states[step.step_id], expected_state)
        self.assertEqual(
            {record.attempt_id for record in backend.records},
            {"dormant_store_install-compensate"},
        )
        self.assertEqual(
            backend.records[-1].event,
            JournalEvent.COMPENSATION_COMPLETE.value,
        )

    def test_failure_after_applied_append_does_not_duplicate_applied(self) -> None:
        backend = _HermeticBackend()
        backend.fail_after_appending_event = JournalEvent.APPLIED
        controller, backend = self._controller(backend)
        with self.assertRaises(InstallationCompensatedError) as caught:
            controller.run(attempt_id="post-append-failure")
        first_step = STORES_ONLY_PLAN[0].step_id
        applied = [
            record
            for record in backend.records
            if record.step_id == first_step
            and record.event == JournalEvent.APPLIED.value
        ]
        self.assertEqual(len(applied), 1)
        self.assertEqual(
            caught.exception.receipt.outcome,
            "same_attempt_compensation_complete",
        )

    def test_compensation_resumes_same_attempt_after_interruption(self) -> None:
        backend = _HermeticBackend()
        backend.fail_before_step = STORES_ONLY_PLAN[5].step_id
        backend.fail_compensation_once_step = STORES_ONLY_PLAN[3].step_id
        controller, backend = self._controller(backend)
        with self.assertRaises(CompensationFailedError):
            controller.run(attempt_id="dormant_store_install-resume-compensation")

        receipt = controller.run(attempt_id="dormant_store_install-resume-compensation")
        expected_applied = tuple(step.step_id for step in STORES_ONLY_PLAN[:5])
        self.assertEqual(receipt.outcome, "same_attempt_compensation_complete")
        self.assertEqual(receipt.applied_step_ids, expected_applied)
        self.assertEqual(
            receipt.compensated_step_ids,
            tuple(
                step.step_id
                for step in reversed(STORES_ONLY_PLAN[:5])
                if step.compensable
            ),
        )
        with self.assertRaisesRegex(
            CompletedStateError, "attempt_already_compensated"
        ):
            controller.run(attempt_id="dormant_store_install-resume-compensation")

    def test_drift_and_unowned_state_refuse_before_any_journal_write(self) -> None:
        for state in (StepState.AFTER, StepState.DRIFT):
            with self.subTest(state=state):
                backend = _HermeticBackend()
                backend.states[STORES_ONLY_PLAN[3].step_id] = state
                controller, backend = self._controller(backend)
                with self.assertRaises(StateDriftError):
                    controller.run(attempt_id="dormant_store_install-drift")
                self.assertEqual(backend.records, [])
                self.assertEqual(backend.actions, [])

    def test_unowned_effect_appearing_after_initial_scan_is_not_claimed(self) -> None:
        backend = _HermeticBackend()
        first = STORES_ONLY_PLAN[0].step_id
        intervening = STORES_ONLY_PLAN[1].step_id
        later = STORES_ONLY_PLAN[17].step_id
        backend.inject_after_apply = (first, later)
        controller, backend = self._controller(backend)
        with self.assertRaisesRegex(
            StateDriftError,
            "step_state_drift:I03_VERIFY_LIVE_PREFLIGHT",
        ):
            controller.run(attempt_id="late-unowned-effect")
        self.assertIs(backend.states[later], StepState.AFTER)
        self.assertNotIn(("apply", intervening), backend.actions)
        self.assertNotIn(("apply", later), backend.actions)
        self.assertNotIn(("compensate", later), backend.actions)
        self.assertEqual(
            [record.event for record in backend.records],
            [JournalEvent.INTENT.value, JournalEvent.APPLIED.value],
        )

    def test_first_mutation_intent_before_effect_revalidates_preflight(self) -> None:
        controller, backend = self._controller()
        attempt_id = "first-mutation-intent-preflight-revalidation"
        for step in STORES_ONLY_PLAN[:3]:
            controller._append(attempt_id, step.step_id, JournalEvent.INTENT)
            controller._append(attempt_id, step.step_id, JournalEvent.APPLIED)
        first_mutation = STORES_ONLY_PLAN[3]
        controller._append(
            attempt_id, first_mutation.step_id, JournalEvent.INTENT
        )
        backend.states[STORES_ONLY_PLAN[2].step_id] = StepState.DRIFT
        backend.probe_calls.clear()

        with self.assertRaisesRegex(
            StateDriftError,
            "step_state_drift:I03_VERIFY_LIVE_PREFLIGHT",
        ):
            controller.run(attempt_id=attempt_id)

        self.assertIn(STORES_ONLY_PLAN[2].step_id, backend.probe_calls)
        self.assertNotIn(("apply", first_mutation.step_id), backend.actions)
        self.assertEqual(
            backend.records[-1].event,
            JournalEvent.INTENT.value,
        )

    def test_unowned_effect_during_compensation_prevents_terminal_marker(self) -> None:
        backend = _HermeticBackend()
        failed = STORES_ONLY_PLAN[5].step_id
        first_compensation = STORES_ONLY_PLAN[4].step_id
        unowned = STORES_ONLY_PLAN[17].step_id
        backend.fail_before_step = failed
        backend.inject_after_compensate = (first_compensation, unowned)
        controller, backend = self._controller(backend)
        with self.assertRaises(CompensationFailedError):
            controller.run(attempt_id="mid-compensation-unowned-effect")
        self.assertIs(backend.states[unowned], StepState.AFTER)
        self.assertFalse(
            any(
                record.event == JournalEvent.COMPENSATION_COMPLETE.value
                for record in backend.records
            )
        )

    def test_dependency_visible_unowned_logical_effect_is_never_claimed(self) -> None:
        backend = _HermeticBackend()
        stores_started = STORES_ONLY_PLAN[9].step_id
        canonical_database = STORES_ONLY_PLAN[10].step_id
        backend.inject_after_apply = (stores_started, canonical_database)
        controller, backend = self._controller(backend)
        with self.assertRaisesRegex(
            StateDriftError,
            "unowned_preexisting_effect:" + canonical_database,
        ):
            controller.run(attempt_id="frontier-unowned-effect")
        self.assertNotIn(("apply", canonical_database), backend.actions)
        self.assertNotIn(("compensate", canonical_database), backend.actions)
        self.assertEqual(
            backend.records[-1].step_id,
            stores_started,
        )
        self.assertEqual(
            backend.records[-1].event,
            JournalEvent.APPLIED.value,
        )

    def test_reverse_compensation_respects_observation_dependencies(self) -> None:
        backend = _HermeticBackend()
        backend.fail_before_step = STORES_ONLY_PLAN[17].step_id
        controller, backend = self._controller(backend)
        with self.assertRaises(InstallationCompensatedError) as raised:
            controller.run(attempt_id="dependency-aware-compensation")
        self.assertEqual(
            raised.exception.receipt.outcome,
            "same_attempt_compensation_complete",
        )
        self.assertEqual(
            raised.exception.receipt.compensated_step_ids,
            tuple(
                step.step_id
                for step in reversed(STORES_ONLY_PLAN[:17])
                if step.compensable
            ),
        )

    def test_failed_dependency_bound_intent_is_safe_after_parent_compensation(
        self,
    ) -> None:
        backend = _HermeticBackend()
        failed = STORES_ONLY_PLAN[10]
        backend.fail_before_step = failed.step_id
        controller, backend = self._controller(backend)

        with self.assertRaises(InstallationCompensatedError) as raised:
            controller.run(attempt_id="dependency-bound-intent-compensation")

        self.assertEqual(
            raised.exception.receipt.outcome,
            "same_attempt_compensation_complete",
        )
        self.assertNotIn(("compensate", failed.step_id), backend.actions)
        self.assertEqual(
            backend.records[-1].event,
            JournalEvent.COMPENSATION_COMPLETE.value,
        )

    def test_compensation_intent_before_parent_effect_rechecks_failed_child(
        self,
    ) -> None:
        backend = _HermeticBackend()
        failed = STORES_ONLY_PLAN[10]
        parent = STORES_ONLY_PLAN[9]
        backend.fail_before_step = failed.step_id
        backend.fail_compensation_once_step = parent.step_id
        controller, backend = self._controller(backend)
        with self.assertRaises(CompensationFailedError):
            controller.run(attempt_id="parent-compensation-intent-child-drift")
        self.assertIs(backend.states[parent.step_id], StepState.AFTER)
        backend.states[failed.step_id] = StepState.AFTER
        backend.actions.clear()

        with self.assertRaisesRegex(
            StateDriftError,
            "failed_unapplied_step_not_before:" + failed.step_id,
        ):
            controller.run(
                attempt_id="parent-compensation-intent-child-drift"
            )

        self.assertEqual(backend.actions, [])
        self.assertIs(backend.states[failed.step_id], StepState.AFTER)
        self.assertEqual(
            backend.records[-1].event,
            JournalEvent.COMPENSATION_INTENT.value,
        )
        self.assertFalse(
            any(
                record.event == JournalEvent.COMPENSATION_COMPLETE.value
                for record in backend.records
            )
        )

    def test_compensation_intent_before_parent_effect_rechecks_compensated_child(
        self,
    ) -> None:
        backend = _HermeticBackend()
        failed = STORES_ONLY_PLAN[11]
        child = STORES_ONLY_PLAN[10]
        parent = STORES_ONLY_PLAN[9]
        backend.fail_before_step = failed.step_id
        backend.fail_compensation_once_step = parent.step_id
        controller, backend = self._controller(backend)
        with self.assertRaises(CompensationFailedError):
            controller.run(
                attempt_id="parent-compensation-intent-compensated-child-drift"
            )
        self.assertIs(backend.states[parent.step_id], StepState.AFTER)
        self.assertIs(backend.states[child.step_id], StepState.BEFORE)
        backend.states[child.step_id] = StepState.AFTER
        backend.actions.clear()

        with self.assertRaisesRegex(
            StateDriftError,
            "compensated_step_not_before:" + child.step_id,
        ):
            controller.run(
                attempt_id="parent-compensation-intent-compensated-child-drift"
            )

        self.assertEqual(backend.actions, [])
        self.assertEqual(
            backend.records[-1].event,
            JournalEvent.COMPENSATION_INTENT.value,
        )
        self.assertFalse(
            any(
                record.event == JournalEvent.COMPENSATION_COMPLETE.value
                for record in backend.records
            )
        )

    def test_compensation_intent_resume_skips_newly_unobservable_children(
        self,
    ) -> None:
        backend = _HermeticBackend()
        backend.fail_before_step = STORES_ONLY_PLAN[17].step_id
        backend.fail_after_compensation_once_step = STORES_ONLY_PLAN[9].step_id
        controller, backend = self._controller(backend)
        with self.assertRaises(CompensationFailedError):
            controller.run(attempt_id="dependency-compensation-intent-resume")
        receipt = controller.run(
            attempt_id="dependency-compensation-intent-resume"
        )
        self.assertEqual(receipt.outcome, "same_attempt_compensation_complete")

    def test_compensation_marker_resume_refuses_active_child_drift(self) -> None:
        controller, backend = self._controller()
        attempt_id = "compensation-marker-active-child-drift"
        for step in STORES_ONLY_PLAN[:17]:
            controller._append(attempt_id, step.step_id, JournalEvent.INTENT)
            if not step.invariant_only:
                backend.states[step.step_id] = StepState.AFTER
            controller._append(attempt_id, step.step_id, JournalEvent.APPLIED)
        controller._append(
            attempt_id,
            ATTEMPT_STEP_ID,
            JournalEvent.COMPENSATION_STARTED,
        )
        drifted = STORES_ONLY_PLAN[14]
        backend.states[drifted.step_id] = StepState.DRIFT
        backend.actions.clear()

        with self.assertRaisesRegex(
            StateDriftError,
            "step_state_drift:" + drifted.step_id,
        ):
            controller.run(attempt_id=attempt_id)

        self.assertEqual(backend.actions, [])
        self.assertEqual(
            backend.records[-1].event,
            JournalEvent.COMPENSATION_STARTED.value,
        )
        self.assertFalse(
            any(
                record.event == JournalEvent.COMPENSATION_COMPLETE.value
                for record in backend.records
            )
        )

    def test_prior_applied_step_drift_prevents_success_receipt(self) -> None:
        backend = _HermeticBackend()
        penultimate = STORES_ONLY_PLAN[-2].step_id
        first = STORES_ONLY_PLAN[0].step_id
        backend.drift_after_apply = (penultimate, first)
        controller, backend = self._controller(backend)
        with self.assertRaisesRegex(
            StateDriftError,
            "applied_step_not_after:" + first,
        ):
            controller.run(attempt_id="late-prior-step-drift")
        terminal = STORES_ONLY_PLAN[-1].step_id
        self.assertNotIn(("apply", terminal), backend.actions)
        self.assertFalse(any(record.step_id == terminal for record in backend.records))

    def test_terminal_postflight_after_failure_blocks_compensation(self) -> None:
        backend = _HermeticBackend()
        backend.fail_after_step = STORES_ONLY_PLAN[-1].step_id
        controller, backend = self._controller(backend)
        with self.assertRaisesRegex(
            CompletedStateError, "terminal_postflight_present_after_failure"
        ):
            controller.run(attempt_id="dormant_store_install-terminal-failure")
        self.assertFalse(
            any(action == "compensate" for action, unused in backend.actions)
        )
        self.assertNotIn(
            JournalEvent.COMPENSATION_STARTED.value,
            tuple(record.event for record in backend.records),
        )

    def test_plan_or_journal_tampering_fails_closed(self) -> None:
        invalid_plan = STORES_ONLY_PLAN[:-1]
        with self.assertRaises(PlanValidationError):
            DormantStoreInstallController(
                plan=invalid_plan,
                backend=_HermeticBackend(),
                held_lock=self.execution_lock.held_capability(),
            )

        reordered = list(STORES_ONLY_PLAN)
        reordered[1], reordered[2] = reordered[2], reordered[1]
        with self.assertRaises(PlanValidationError):
            validate_plan(tuple(reordered))

        wrong_requirements = list(STORES_ONLY_PLAN)
        wrong_requirements[9] = replace(
            wrong_requirements[9], observation_requires=()
        )
        with self.assertRaisesRegex(
            PlanValidationError, "observation_requirements_invalid"
        ):
            validate_plan(tuple(wrong_requirements))

        wrong_lifetime = list(STORES_ONLY_PLAN)
        wrong_lifetime[2] = replace(
            wrong_lifetime[2], proof_lifetime="continuous"
        )
        with self.assertRaisesRegex(
            PlanValidationError, "proof_lifetime_invalid"
        ):
            validate_plan(tuple(wrong_lifetime))

        controller, backend = self._controller()
        controller.run(attempt_id="dormant_store_install-tamper")
        backend.records[0] = replace(
            backend.records[0], event=JournalEvent.APPLIED.value
        )
        actions = tuple(backend.actions)
        with self.assertRaises(JournalValidationError):
            controller.run(attempt_id="dormant_store_install-tamper")
        self.assertEqual(tuple(backend.actions), actions)

    def test_closed_lock_capability_refuses_before_journal_access(self) -> None:
        controller, backend = self._controller()
        self.execution_lock.close()
        with self.assertRaisesRegex(ControllerLockError, "global_lock_not_held"):
            controller.run(attempt_id="dormant_store_install-lock-closed")
        self.assertEqual(backend.records, [])
        self.assertEqual(backend.actions, [])


if __name__ == "__main__":
    unittest.main()
