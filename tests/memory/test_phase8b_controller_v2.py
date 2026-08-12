from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest

from tools.governed_memory_install.controller_v2 import (
    CompletedStateError,
    CompensationFailedError,
    ControllerLockError,
    EXECUTION_MODE,
    InstallationCompensatedError,
    JournalEvent,
    JournalRecord,
    JournalValidationError,
    Phase8BStoresController,
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


ROOT = Path(__file__).resolve().parents[2]


class _HermeticBackend:
    execution_mode = EXECUTION_MODE

    def __init__(self) -> None:
        self.records: list[JournalRecord] = []
        self.states = {
            step.step_id: StepState.BEFORE for step in STORES_ONLY_PLAN
        }
        self.actions: list[tuple[str, str]] = []
        self.fail_before_step: str | None = None
        self.fail_after_step: str | None = None
        self.fail_compensation_once_step: str | None = None

    def journal_records(self) -> tuple[JournalRecord, ...]:
        return tuple(self.records)

    def append_journal(self, record: JournalRecord) -> None:
        self.records.append(record)

    def probe(self, step: PlanStep) -> StepState:
        return self.states[step.step_id]

    def apply(self, step: PlanStep) -> None:
        self.actions.append(("apply", step.step_id))
        if self.fail_before_step == step.step_id:
            self.fail_before_step = None
            raise RuntimeError("synthetic_pre_effect_failure")
        self.states[step.step_id] = StepState.AFTER
        if self.fail_after_step == step.step_id:
            self.fail_after_step = None
            raise RuntimeError("synthetic_post_effect_failure")

    def compensate(self, step: PlanStep) -> None:
        self.actions.append(("compensate", step.step_id))
        if self.fail_compensation_once_step == step.step_id:
            self.fail_compensation_once_step = None
            raise RuntimeError("synthetic_compensation_failure")
        self.states[step.step_id] = StepState.BEFORE


class Phase8BControllerV2Tests(unittest.TestCase):
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
    ) -> tuple[Phase8BStoresController, _HermeticBackend]:
        selected = backend or _HermeticBackend()
        return (
            Phase8BStoresController(
                plan=STORES_ONLY_PLAN,
                backend=selected,
                held_lock=self.execution_lock.held_capability(),
            ),
            selected,
        )

    def test_exact_20_step_stores_only_plan_and_no_live_executor(self) -> None:
        self.assertEqual(len(STORES_ONLY_PLAN), 20)
        self.assertEqual(
            tuple(step.step_id[:3] for step in STORES_ONLY_PLAN),
            tuple(f"I{position:02d}" for position in range(1, 21)),
        )
        self.assertTrue(STORES_ONLY_PLAN[-1].seals)
        self.assertFalse(STORES_ONLY_PLAN[-1].compensable)
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
        ):
            self.assertNotIn(forbidden, joined)

        plan_document = json.loads(
            (
                ROOT
                / "ops/governed_memory/installation/phase8b/controller_plan.json"
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
                "durable_journal_or_anchor_adapter_packaged"
            ],
            False,
        )
        self.assertIs(
            plan_document["execution_invariants"][
                "durable_crash_recovery_proven"
            ],
            False,
        )
        self.assertEqual(
            plan_document["live_execution"],
            {
                "live_install_entrypoint_packaged": False,
                "live_rollback_entrypoint_packaged": False,
                "live_activation_entrypoint_packaged": False,
                "stores_supervisor_cli_packaged": True,
                "stores_supervisor_cli_docker_surface": [
                    "container_inspect",
                    "container_start",
                    "container_stop",
                ],
                "stores_supervisor_is_installer": False,
                "stores_supervisor_requires_preexisting_exact_ledger_container_ids": True,
            },
        )

        source = (
            ROOT / "tools/governed_memory_install/controller_v2.py"
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

    def test_install_runs_exact_order_and_refuses_completed_state(self) -> None:
        controller, backend = self._controller()
        receipt = controller.run(attempt_id="phase8b-happy")
        expected = tuple(step.step_id for step in STORES_ONLY_PLAN)
        self.assertEqual(receipt.applied_step_ids, expected)
        self.assertEqual(receipt.compensated_step_ids, ())
        self.assertEqual(
            tuple(step_id for action, step_id in backend.actions if action == "apply"),
            expected,
        )
        self.assertEqual(receipt.journal_sequence, 40)
        with self.assertRaisesRegex(
            CompletedStateError, "inactive_postflight_already_sealed"
        ):
            controller.run(attempt_id="phase8b-happy")
        self.assertEqual(len(backend.actions), 20)

    def test_failure_compensates_owned_effects_in_exact_reverse_order(self) -> None:
        backend = _HermeticBackend()
        backend.fail_after_step = STORES_ONLY_PLAN[7].step_id
        controller, backend = self._controller(backend)
        with self.assertRaises(InstallationCompensatedError) as raised:
            controller.run(attempt_id="phase8b-compensate")
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
            {"phase8b-compensate"},
        )
        self.assertEqual(
            backend.records[-1].event,
            JournalEvent.COMPENSATION_COMPLETE.value,
        )

    def test_compensation_resumes_same_attempt_after_interruption(self) -> None:
        backend = _HermeticBackend()
        backend.fail_before_step = STORES_ONLY_PLAN[5].step_id
        backend.fail_compensation_once_step = STORES_ONLY_PLAN[3].step_id
        controller, backend = self._controller(backend)
        with self.assertRaises(CompensationFailedError):
            controller.run(attempt_id="phase8b-resume-compensation")

        receipt = controller.run(attempt_id="phase8b-resume-compensation")
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
            controller.run(attempt_id="phase8b-resume-compensation")

    def test_drift_and_unowned_state_refuse_before_any_journal_write(self) -> None:
        for state in (StepState.AFTER, StepState.DRIFT):
            with self.subTest(state=state):
                backend = _HermeticBackend()
                backend.states[STORES_ONLY_PLAN[3].step_id] = state
                controller, backend = self._controller(backend)
                with self.assertRaises(StateDriftError):
                    controller.run(attempt_id="phase8b-drift")
                self.assertEqual(backend.records, [])
                self.assertEqual(backend.actions, [])

    def test_seal_effect_after_failure_blocks_compensation(self) -> None:
        backend = _HermeticBackend()
        backend.fail_after_step = STORES_ONLY_PLAN[-1].step_id
        controller, backend = self._controller(backend)
        with self.assertRaisesRegex(
            CompletedStateError, "seal_effect_present_after_failure"
        ):
            controller.run(attempt_id="phase8b-seal-failure")
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
            Phase8BStoresController(
                plan=invalid_plan,
                backend=_HermeticBackend(),
                held_lock=self.execution_lock.held_capability(),
            )

        reordered = list(STORES_ONLY_PLAN)
        reordered[1], reordered[2] = reordered[2], reordered[1]
        with self.assertRaises(PlanValidationError):
            validate_plan(tuple(reordered))

        controller, backend = self._controller()
        controller.run(attempt_id="phase8b-tamper")
        backend.records[0] = replace(
            backend.records[0], event=JournalEvent.APPLIED.value
        )
        actions = tuple(backend.actions)
        with self.assertRaises(JournalValidationError):
            controller.run(attempt_id="phase8b-tamper")
        self.assertEqual(tuple(backend.actions), actions)

    def test_closed_lock_capability_refuses_before_journal_access(self) -> None:
        controller, backend = self._controller()
        self.execution_lock.close()
        with self.assertRaisesRegex(ControllerLockError, "global_lock_not_held"):
            controller.run(attempt_id="phase8b-lock-closed")
        self.assertEqual(backend.records, [])
        self.assertEqual(backend.actions, [])


if __name__ == "__main__":
    unittest.main()
