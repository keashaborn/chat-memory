from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from tools.governed_memory_install.controller import (
    AuthorizationError,
    BackendNotAuthorizedError,
    ControllerInterruption,
    DisposableControllerAuthorization,
    EXECUTION_MODE,
    INSTALL_STEPS,
    InstallationCompensatedError,
    InactiveInstallationController,
    OperationStateError,
    RETAINED_INSTALL_EFFECTS,
    ROLLBACK_PREREQUISITES,
    ROLLBACK_STEPS,
    RollbackFacts,
    RollbackGateError,
    StateDriftError,
    StepState,
)
from tools.governed_memory_install.controller_linux import (
    LinuxControllerBackend,
    LiveControllerUnavailableError,
    build_linux_controller_backend,
)
from tools.governed_memory_install.journal import (
    FileJournal,
    JournalBusyError,
    JournalIntegrityError,
    JournalSecurityError,
    ZERO_HEAD,
    parse_journal_bytes,
)
from tools.governed_memory_install.synthetic_backend import SyntheticBackend


ROOT = Path(__file__).resolve().parents[2]
BINDING = hashlib.sha256(b"phase8a-controller-binding").hexdigest()
INSTALL_PROOF = hashlib.sha256(b"phase8a-install-proof").hexdigest()
ROLLBACK_PROOF = hashlib.sha256(b"phase8a-rollback-proof").hexdigest()


def _authorization(operation: str) -> DisposableControllerAuthorization:
    return DisposableControllerAuthorization(
        operation=operation,
        binding_sha256=BINDING,
        proof_receipt_sha256=(
            INSTALL_PROOF if operation == "install" else ROLLBACK_PROOF
        ),
    )


class _Harness:
    def __init__(self, backend: SyntheticBackend | None = None) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "controller"
        self.directory.mkdir(mode=0o700)
        os.chmod(self.directory, 0o700)
        self.path = self.directory / "journal.jsonl"
        self.backend = backend or SyntheticBackend()
        self.journal = FileJournal(self.path, binding_sha256=BINDING)
        self.controller = InactiveInstallationController(
            backend=self.backend,
            journal=self.journal,
            binding_sha256=BINDING,
        )

    def close(self) -> None:
        self.journal.close()
        self.temp.cleanup()

    def __enter__(self) -> _Harness:
        return self

    def __exit__(self, *unused: object) -> None:
        self.close()


class InactiveControllerTests(unittest.TestCase):
    def test_exact_closed_plans_exclude_source_and_runtime_activation(self) -> None:
        self.assertEqual(len(INSTALL_STEPS), 26)
        self.assertEqual(len(set(INSTALL_STEPS)), 26)
        self.assertEqual(INSTALL_STEPS[0], "I04_QUARANTINE_LEGACY_SECRET")
        self.assertEqual(INSTALL_STEPS[-1], "I29_SEAL_INACTIVE_POSTFLIGHT")
        self.assertEqual(len(ROLLBACK_STEPS), 20)
        self.assertEqual(len(set(ROLLBACK_STEPS)), 20)
        self.assertEqual(
            ROLLBACK_STEPS[0], "R01_VERIFY_EMPTY_EXACT_OWNERSHIP"
        )
        self.assertEqual(ROLLBACK_STEPS[-1], "R20_VERIFY_FINAL_ABSENCE")
        joined = "\n".join(INSTALL_STEPS + ROLLBACK_STEPS)
        for forbidden in (
            "SOURCE_CLUSTER",
            "CONVERSATION_BRIDGE",
            "SUPABASE",
            "OPENAI",
            "START_HTTP",
            "START_WORKER",
            "ROUTE",
        ):
            self.assertNotIn(forbidden, joined)
        prerequisites = {
            item for values in ROLLBACK_PREREQUISITES.values() for item in values
        }
        self.assertTrue(set(RETAINED_INSTALL_EFFECTS).isdisjoint(prerequisites))

    def test_happy_install_is_idempotent_then_separately_authorized_rollback(
        self,
    ) -> None:
        with _Harness() as harness:
            installed = harness.controller.install(
                _authorization("install"), attempt_id="install-0001"
            )
            self.assertEqual(installed.completed_step_ids, INSTALL_STEPS)
            self.assertEqual(installed.recovered_step_ids, ())
            self.assertEqual(len(harness.backend.effects), len(INSTALL_STEPS))
            first_sequence = installed.journal_sequence
            first_head = installed.journal_head_sha256

            repeated = harness.controller.install(
                _authorization("install"), attempt_id="install-0001"
            )
            self.assertEqual(repeated.journal_sequence, first_sequence)
            self.assertEqual(repeated.journal_head_sha256, first_head)
            self.assertEqual(len(harness.backend.effects), len(INSTALL_STEPS))

            with self.assertRaises(AuthorizationError):
                harness.controller.rollback(
                    _authorization("install"), attempt_id="rollback-0001"
                )
            rolled_back = harness.controller.rollback(
                _authorization("rollback"), attempt_id="rollback-0001"
            )
            self.assertEqual(rolled_back.completed_step_ids, ROLLBACK_STEPS)
            self.assertEqual(
                len(harness.backend.effects), len(INSTALL_STEPS) + len(ROLLBACK_STEPS)
            )
            self.assertEqual(harness.backend.resources_remaining, ())
            self.assertEqual(
                harness.backend.retained_effects,
                tuple(
                    sorted(
                        (*RETAINED_INSTALL_EFFECTS, "I29_SEAL_INACTIVE_POSTFLIGHT")
                    )
                ),
            )
            self.assertEqual(
                harness.backend.retained_resources,
                (
                    "inactive_postflight_receipt",
                    "legacy_secret_quarantine",
                    "root:backup_root",
                    "root:environment_root",
                    "root:install_root",
                    "root:runtime_environment_root",
                    "root:state_root",
                    "service_identity",
                ),
            )
            for retained in RETAINED_INSTALL_EFFECTS:
                self.assertEqual(harness.backend.effect_count("install", retained), 1)
            with self.assertRaises(OperationStateError):
                harness.controller.install(
                    _authorization("install"), attempt_id="install-0001"
                )

    def test_synthetic_receipt_and_journal_are_content_free(self) -> None:
        with _Harness() as harness:
            receipt = harness.controller.install(
                _authorization("install"), attempt_id="content-free-0001"
            )
            rendered = harness.path.read_text(encoding="ascii") + json.dumps(
                receipt.as_dict(), sort_keys=True
            )
            for forbidden in (
                "password",
                "api_key",
                "bearer",
                "postgresql://",
                "conversation",
                "user_id",
                "memory text",
            ):
                self.assertNotIn(forbidden, rendered.lower())

    def test_no_source_provider_network_or_runtime_actions(self) -> None:
        with _Harness() as harness:
            harness.controller.install(
                _authorization("install"), attempt_id="zero-external-0001"
            )
            harness.controller.rollback(
                _authorization("rollback"), attempt_id="zero-external-rollback"
            )
            self.assertEqual(harness.backend.source_postgresql_actions, 0)
            self.assertEqual(harness.backend.provider_calls, 0)
            self.assertEqual(harness.backend.network_calls, 0)
            self.assertEqual(harness.backend.runtime_application_starts, 0)

    def test_external_scope_and_arbitrary_backend_are_not_execution_capabilities(
        self,
    ) -> None:
        class CryptographicallyValidScopeNotExecution:
            result_type = "cryptographically_valid_scope_not_execution"
            operation = "dormant_install"
            binding_sha256 = BINDING
            authorization_sha256 = INSTALL_PROOF

        with _Harness() as harness:
            with self.assertRaises(AuthorizationError):
                harness.controller.install(  # type: ignore[arg-type]
                    CryptographicallyValidScopeNotExecution(),
                    attempt_id="scope-is-not-execution",
                )

        class SpoofedBackend(SyntheticBackend):
            _phase8a_synthetic_seal = object()

        temp = tempfile.TemporaryDirectory()
        try:
            directory = Path(temp.name) / "journal"
            directory.mkdir(mode=0o700)
            journal = FileJournal(directory / "journal.jsonl", binding_sha256=BINDING)
            try:
                with self.assertRaises(BackendNotAuthorizedError):
                    InactiveInstallationController(
                        backend=SpoofedBackend(),
                        journal=journal,
                        binding_sha256=BINDING,
                    )
            finally:
                journal.close()
        finally:
            temp.cleanup()

    def test_live_linux_adapter_has_no_constructible_or_callable_path(self) -> None:
        with self.assertRaisesRegex(
            LiveControllerUnavailableError,
            "phase8a_has_no_live_install_or_rollback_adapter",
        ):
            LinuxControllerBackend()
        with self.assertRaises(LiveControllerUnavailableError):
            build_linux_controller_backend()
        source = (
            ROOT / "tools/governed_memory_install/controller_linux.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "subprocess",
            "socket",
            "requests",
            "urllib",
            "psycopg",
            "docker",
            "systemctl",
        ):
            self.assertNotIn(forbidden, source)

    def test_every_install_step_recovers_after_intent_without_double_effect(
        self,
    ) -> None:
        for step_id in INSTALL_STEPS:
            with self.subTest(step_id=step_id), _Harness(
                SyntheticBackend(crash_after_intent_step=step_id)
            ) as harness:
                with self.assertRaises(ControllerInterruption):
                    harness.controller.install(
                        _authorization("install"), attempt_id="intent-crash"
                    )
                receipt = harness.controller.install(
                    _authorization("install"), attempt_id="intent-crash"
                )
                self.assertEqual(receipt.completed_step_ids, INSTALL_STEPS)
                self.assertEqual(harness.backend.effect_count("install", step_id), 1)
                self.assertNotIn(step_id, receipt.recovered_step_ids)

    def test_every_install_step_recovers_after_effect_without_double_effect(
        self,
    ) -> None:
        for step_id in INSTALL_STEPS:
            with self.subTest(step_id=step_id), _Harness(
                SyntheticBackend(crash_after_effect_step=step_id)
            ) as harness:
                with self.assertRaises(ControllerInterruption):
                    harness.controller.install(
                        _authorization("install"), attempt_id="effect-crash"
                    )
                receipt = harness.controller.install(
                    _authorization("install"), attempt_id="effect-crash"
                )
                self.assertEqual(receipt.completed_step_ids, INSTALL_STEPS)
                self.assertEqual(harness.backend.effect_count("install", step_id), 1)
                self.assertIn(step_id, receipt.recovered_step_ids)

    def test_every_install_step_resumes_after_journal_commit_without_double_effect(
        self,
    ) -> None:
        for step_id in INSTALL_STEPS:
            with self.subTest(step_id=step_id), _Harness(
                SyntheticBackend(crash_after_journal_commit_step=step_id)
            ) as harness:
                with self.assertRaises(ControllerInterruption):
                    harness.controller.install(
                        _authorization("install"), attempt_id="journal-crash"
                    )
                receipt = harness.controller.install(
                    _authorization("install"), attempt_id="journal-crash"
                )
                self.assertEqual(receipt.completed_step_ids, INSTALL_STEPS)
                self.assertEqual(harness.backend.effect_count("install", step_id), 1)

    def test_every_rollback_step_recovers_after_intent_without_double_effect(
        self,
    ) -> None:
        for step_id in ROLLBACK_STEPS:
            with self.subTest(step_id=step_id), _Harness() as harness:
                harness.controller.install(
                    _authorization("install"), attempt_id="install-before-rollback"
                )
                harness.backend.crash_after_intent_step = step_id
                with self.assertRaises(ControllerInterruption):
                    harness.controller.rollback(
                        _authorization("rollback"), attempt_id="rollback-intent-crash"
                    )
                receipt = harness.controller.rollback(
                    _authorization("rollback"), attempt_id="rollback-intent-crash"
                )
                self.assertEqual(receipt.completed_step_ids, ROLLBACK_STEPS)
                self.assertEqual(harness.backend.effect_count("rollback", step_id), 1)
                self.assertNotIn(step_id, receipt.recovered_step_ids)

    def test_every_rollback_step_recovers_after_effect_without_double_effect(
        self,
    ) -> None:
        for step_id in ROLLBACK_STEPS:
            with self.subTest(step_id=step_id), _Harness() as harness:
                harness.controller.install(
                    _authorization("install"), attempt_id="install-before-rollback"
                )
                harness.backend.crash_after_effect_step = step_id
                with self.assertRaises(ControllerInterruption):
                    harness.controller.rollback(
                        _authorization("rollback"), attempt_id="rollback-effect-crash"
                    )
                receipt = harness.controller.rollback(
                    _authorization("rollback"), attempt_id="rollback-effect-crash"
                )
                self.assertEqual(receipt.completed_step_ids, ROLLBACK_STEPS)
                self.assertEqual(harness.backend.effect_count("rollback", step_id), 1)
                self.assertIn(step_id, receipt.recovered_step_ids)

    def test_every_rollback_step_resumes_after_journal_commit_without_double_effect(
        self,
    ) -> None:
        for step_id in ROLLBACK_STEPS:
            with self.subTest(step_id=step_id), _Harness() as harness:
                harness.controller.install(
                    _authorization("install"), attempt_id="install-before-rollback"
                )
                harness.backend.crash_after_journal_commit_step = step_id
                with self.assertRaises(ControllerInterruption):
                    harness.controller.rollback(
                        _authorization("rollback"), attempt_id="rollback-journal-crash"
                    )
                receipt = harness.controller.rollback(
                    _authorization("rollback"), attempt_id="rollback-journal-crash"
                )
                self.assertEqual(receipt.completed_step_ids, ROLLBACK_STEPS)
                self.assertEqual(harness.backend.effect_count("rollback", step_id), 1)

    def test_ordinary_pre_effect_failure_compensates_only_same_attempt(self) -> None:
        backend = SyntheticBackend(fail_effect_step="I12_CREATE_POSTGRES_CONTAINER")
        with _Harness(backend) as harness:
            with self.assertRaises(InstallationCompensatedError) as raised:
                harness.controller.install(
                    _authorization("install"), attempt_id="compensate-pre-effect"
                )
            receipt = raised.exception.receipt
            self.assertEqual(receipt.operation, "compensation")
            self.assertNotIn(
                "R12_STOP_REMOVE_POSTGRES_CONTAINER", receipt.completed_step_ids
            )
            self.assertIn(
                "R14_REMOVE_POSTGRES_VOLUME_EXACT_EMPTY",
                receipt.completed_step_ids,
            )
            self.assertEqual(harness.backend.resources_remaining, ())
            resumed = harness.controller.install(
                _authorization("install"), attempt_id="compensate-pre-effect"
            )
            self.assertEqual(resumed.outcome, "same_attempt_compensated")
            with self.assertRaises(OperationStateError):
                harness.controller.install(
                    _authorization("install"), attempt_id="different-attempt"
                )

    def test_ordinary_post_effect_failure_compensates_uncertain_effect(self) -> None:
        backend = SyntheticBackend(
            fail_after_effect_step="I12_CREATE_POSTGRES_CONTAINER"
        )
        with _Harness(backend) as harness:
            with self.assertRaises(InstallationCompensatedError) as raised:
                harness.controller.install(
                    _authorization("install"), attempt_id="compensate-post-effect"
                )
            self.assertIn(
                "R12_STOP_REMOVE_POSTGRES_CONTAINER",
                raised.exception.receipt.completed_step_ids,
            )
            self.assertEqual(
                harness.backend.effect_count(
                    "install", "I12_CREATE_POSTGRES_CONTAINER"
                ),
                1,
            )
            self.assertEqual(harness.backend.resources_remaining, ())

    def test_compensation_resume_rejects_a_different_proof_binding(self) -> None:
        backend = SyntheticBackend(
            fail_effect_step="I12_CREATE_POSTGRES_CONTAINER"
        )
        with _Harness(backend) as harness:
            with self.assertRaises(InstallationCompensatedError):
                harness.controller.install(
                    _authorization("install"),
                    attempt_id="compensation-binding",
                )
            sequence = harness.journal.sequence
            head = harness.journal.head_sha256
            effects = tuple(harness.backend.effects)
            different = DisposableControllerAuthorization(
                operation="install",
                binding_sha256=BINDING,
                proof_receipt_sha256=hashlib.sha256(
                    b"different-phase8a-install-proof"
                ).hexdigest(),
            )
            with self.assertRaisesRegex(
                AuthorizationError,
                "compensation_authorization_binding_mismatch",
            ):
                harness.controller.install(
                    different,
                    attempt_id="compensation-binding",
                )
            self.assertEqual(harness.journal.sequence, sequence)
            self.assertEqual(harness.journal.head_sha256, head)
            self.assertEqual(tuple(harness.backend.effects), effects)

    def test_all_empty_only_rollback_gates_refuse_before_intent(self) -> None:
        cases = {
            "pilot_ever_started": True,
            "successor_user_memory_row_count": 1,
            "successor_projection_queue_row_count": 1,
            "qdrant_point_count": 1,
            "active_client_count": 1,
            "legacy_import_count": 1,
        }
        for field, value in cases.items():
            with self.subTest(field=field), _Harness() as harness:
                harness.controller.install(
                    _authorization("install"), attempt_id="gate-install"
                )
                harness.backend.set_rollback_facts(**{field: value})
                before_sequence = harness.journal.sequence
                before_effects = tuple(harness.backend.effects)
                with self.assertRaises(RollbackGateError) as raised:
                    harness.controller.rollback(
                        _authorization("rollback"), attempt_id="gate-rollback"
                    )
                self.assertIn(
                    field
                    if field == "pilot_ever_started"
                    else field + "_not_zero",
                    raised.exception.refusal_codes,
                )
                self.assertEqual(harness.journal.sequence, before_sequence)
                self.assertEqual(tuple(harness.backend.effects), before_effects)

    def test_unowned_after_state_and_drift_refuse_without_adoption(self) -> None:
        with _Harness() as harness:
            harness.backend.seed_unowned_effect("install", INSTALL_STEPS[0])
            with self.assertRaisesRegex(StateDriftError, "unowned_preexisting_effect"):
                harness.controller.install(
                    _authorization("install"), attempt_id="unowned-effect"
                )
            self.assertEqual(harness.journal.sequence, 0)

        with _Harness() as harness:
            harness.backend.set_probe_override(
                "install", INSTALL_STEPS[0], StepState.DRIFT
            )
            with self.assertRaisesRegex(StateDriftError, "step_state_drift"):
                harness.controller.install(
                    _authorization("install"), attempt_id="drift-effect"
                )
            self.assertEqual(harness.journal.sequence, 0)

    def test_rollback_refuses_unowned_install_after_state_without_intent(
        self,
    ) -> None:
        unowned_step = "I10_CREATE_POSTGRES_VOLUME"
        with _Harness() as harness:
            harness.backend.seed_unowned_effect("install", unowned_step)
            before_effects = tuple(harness.backend.effects)
            with self.assertRaisesRegex(
                StateDriftError,
                "unowned_install_state_before_rollback:" + unowned_step,
            ):
                harness.controller.rollback(
                    _authorization("rollback"),
                    attempt_id="empty-journal-unowned-rollback",
                )
            self.assertEqual(harness.journal.sequence, 0)
            self.assertEqual(tuple(harness.backend.effects), before_effects)

        with _Harness(
            SyntheticBackend(
                crash_after_journal_commit_step=INSTALL_STEPS[0]
            )
        ) as harness:
            with self.assertRaises(ControllerInterruption):
                harness.controller.install(
                    _authorization("install"),
                    attempt_id="partial-journal-unowned-install",
                )
            harness.backend.clear_faults()
            harness.backend.seed_unowned_effect("install", unowned_step)
            before_sequence = harness.journal.sequence
            before_head = harness.journal.head_sha256
            before_effects = tuple(harness.backend.effects)
            with self.assertRaisesRegex(
                StateDriftError,
                "unowned_install_state_before_rollback:" + unowned_step,
            ):
                harness.controller.rollback(
                    _authorization("rollback"),
                    attempt_id="partial-journal-unowned-rollback",
                )
            self.assertEqual(harness.journal.sequence, before_sequence)
            self.assertEqual(harness.journal.head_sha256, before_head)
            self.assertEqual(tuple(harness.backend.effects), before_effects)

        with _Harness(
            SyntheticBackend(
                crash_after_journal_commit_step=(
                    "I21_INSTALL_STORE_SUPERVISOR"
                )
            )
        ) as harness:
            with self.assertRaises(ControllerInterruption):
                harness.controller.install(
                    _authorization("install"),
                    attempt_id="multi-prerequisite-owned-install",
                )
            harness.backend.clear_faults()
            unowned_second_prerequisite = (
                "I22_ENABLE_START_STORE_SUPERVISOR"
            )
            harness.backend.seed_unowned_effect(
                "install", unowned_second_prerequisite
            )
            before_sequence = harness.journal.sequence
            before_head = harness.journal.head_sha256
            before_effects = tuple(harness.backend.effects)
            with self.assertRaisesRegex(
                StateDriftError,
                "unowned_install_state_before_rollback:"
                + unowned_second_prerequisite,
            ):
                harness.controller.rollback(
                    _authorization("rollback"),
                    attempt_id="multi-prerequisite-unowned-rollback",
                )
            self.assertEqual(harness.journal.sequence, before_sequence)
            self.assertEqual(harness.journal.head_sha256, before_head)
            self.assertEqual(tuple(harness.backend.effects), before_effects)

    def test_completed_state_regression_refuses(self) -> None:
        with _Harness() as harness:
            harness.controller.install(
                _authorization("install"), attempt_id="completed-regression"
            )
            effects = tuple(harness.backend.effects)
            harness.backend.set_probe_override(
                "install", INSTALL_STEPS[0], StepState.BEFORE
            )
            with self.assertRaisesRegex(StateDriftError, "completed_step_not_after"):
                harness.controller.install(
                    _authorization("install"), attempt_id="completed-regression"
                )
            self.assertEqual(tuple(harness.backend.effects), effects)

    def test_rollback_rejects_cross_operation_order_and_mixed_install_authority(
        self,
    ) -> None:
        with _Harness() as harness:
            harness.journal.append(
                operation="rollback",
                attempt_id="forged-rollback",
                step_id=ROLLBACK_STEPS[0],
                event="intent",
                disposable_authorization_sha256=ROLLBACK_PROOF,
            )
            harness.journal.append(
                operation="install",
                attempt_id="late-install",
                step_id=INSTALL_STEPS[0],
                event="intent",
                disposable_authorization_sha256=INSTALL_PROOF,
            )
            with self.assertRaisesRegex(
                OperationStateError,
                "rollback_cross_operation_order_invalid",
            ):
                harness.controller.rollback(
                    _authorization("rollback"), attempt_id="forged-rollback"
                )

        with _Harness() as harness:
            harness.journal.append(
                operation="install",
                attempt_id="mixed-authorization",
                step_id=INSTALL_STEPS[0],
                event="intent",
                disposable_authorization_sha256=INSTALL_PROOF,
            )
            harness.journal.append(
                operation="install",
                attempt_id="mixed-authorization",
                step_id=INSTALL_STEPS[0],
                event="effect_complete",
                disposable_authorization_sha256="f" * 64,
            )
            with self.assertRaisesRegex(
                AuthorizationError,
                "install_authorization_binding_mismatch",
            ):
                harness.controller.rollback(
                    _authorization("rollback"), attempt_id="mixed-rollback"
                )


class JournalTests(unittest.TestCase):
    def _new_directory(self, temp: tempfile.TemporaryDirectory) -> Path:
        directory = Path(temp.name) / "journal"
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)
        return directory

    def _one_entry(self, path: Path) -> tuple[int, str]:
        with FileJournal(path, binding_sha256=BINDING) as journal:
            journal.append(
                operation="install",
                attempt_id="journal-0001",
                step_id=INSTALL_STEPS[0],
                event="intent",
                disposable_authorization_sha256=INSTALL_PROOF,
            )
            return journal.sequence, journal.head_sha256

    def test_filesystem_modes_link_count_and_lock_are_strict(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            bad_directory = temp / "bad"
            bad_directory.mkdir(mode=0o755)
            os.chmod(bad_directory, 0o755)
            with self.assertRaises(JournalSecurityError):
                FileJournal(bad_directory / "journal", binding_sha256=BINDING)

            directory = temp / "good"
            directory.mkdir(mode=0o700)
            path = directory / "journal"
            with FileJournal(path, binding_sha256=BINDING) as journal:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                with self.assertRaises(JournalBusyError):
                    FileJournal(path, binding_sha256=BINDING)

            os.chmod(path, 0o644)
            with self.assertRaises(JournalSecurityError):
                FileJournal(path, binding_sha256=BINDING)
            os.chmod(path, 0o600)
            hardlink = directory / "hardlink"
            os.link(path, hardlink)
            with self.assertRaises(JournalSecurityError):
                FileJournal(path, binding_sha256=BINDING)
            hardlink.unlink()
            path.unlink()
            target = directory / "target"
            target.write_bytes(b"")
            os.chmod(target, 0o600)
            path.symlink_to(target)
            with self.assertRaises(JournalSecurityError):
                FileJournal(path, binding_sha256=BINDING)

    def test_tamper_reorder_partial_truncate_and_binding_fail_closed(self) -> None:
        mutation_cases = ("tamper", "reorder", "partial_truncate")
        for case in mutation_cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw:
                directory = self._new_directory_object(Path(raw))
                path = directory / "journal"
                with FileJournal(path, binding_sha256=BINDING) as journal:
                    for step in INSTALL_STEPS[:2]:
                        for event in ("intent", "effect_complete"):
                            journal.append(
                                operation="install",
                                attempt_id="journal-mutation",
                                step_id=step,
                                event=event,
                                disposable_authorization_sha256=INSTALL_PROOF,
                            )
                lines = path.read_bytes().splitlines(keepends=True)
                if case == "tamper":
                    document = json.loads(lines[1])
                    document["attempt_id"] = "journal-mutated"
                    lines[1] = json.dumps(
                        document, sort_keys=True, separators=(",", ":")
                    ).encode("ascii") + b"\n"
                    path.write_bytes(b"".join(lines))
                elif case == "reorder":
                    lines[1], lines[2] = lines[2], lines[1]
                    path.write_bytes(b"".join(lines))
                else:
                    path.write_bytes(b"".join(lines)[:-7])
                with self.assertRaises(JournalIntegrityError):
                    FileJournal(path, binding_sha256=BINDING)

        with tempfile.TemporaryDirectory() as raw:
            directory = self._new_directory_object(Path(raw))
            path = directory / "journal"
            self._one_entry(path)
            wrong = hashlib.sha256(b"wrong-binding").hexdigest()
            with self.assertRaises(JournalIntegrityError):
                FileJournal(path, binding_sha256=wrong)

    def test_clean_suffix_truncation_requires_and_honors_sealed_head(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = self._new_directory_object(Path(raw))
            path = directory / "journal"
            with FileJournal(path, binding_sha256=BINDING) as journal:
                for step in INSTALL_STEPS[:2]:
                    journal.append(
                        operation="install",
                        attempt_id="sealed-head",
                        step_id=step,
                        event="intent",
                        disposable_authorization_sha256=INSTALL_PROOF,
                    )
                sequence = journal.sequence
                head = journal.head_sha256
            lines = path.read_bytes().splitlines(keepends=True)
            path.write_bytes(b"".join(lines[:-1]))
            with self.assertRaisesRegex(
                JournalIntegrityError, "journal_sealed_head_mismatch"
            ):
                FileJournal(
                    path,
                    binding_sha256=BINDING,
                    expected_sequence=sequence,
                    expected_head_sha256=head,
                )

    def test_duplicate_and_extra_keys_are_rejected(self) -> None:
        duplicate = (
            b'{"schema_version":"governed-memory-installation-journal-entry-v1",'
            b'"schema_version":"governed-memory-installation-journal-entry-v1"}\n'
        )
        with self.assertRaisesRegex(JournalIntegrityError, "journal_duplicate_key"):
            parse_journal_bytes(duplicate, binding_sha256=BINDING)

        document = {
            "schema_version": "governed-memory-installation-journal-entry-v1",
            "binding_sha256": BINDING,
            "sequence": 1,
            "operation": "install",
            "attempt_id": "extra-key",
            "step_id": INSTALL_STEPS[0],
            "event": "intent",
            "disposable_authorization_sha256": INSTALL_PROOF,
            "prior_entry_sha256": ZERO_HEAD,
            "unexpected": False,
        }
        document["entry_sha256"] = hashlib.sha256(
            json.dumps(
                document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("ascii")
        ).hexdigest()
        encoded = json.dumps(
            document, sort_keys=True, separators=(",", ":")
        ).encode("ascii") + b"\n"
        with self.assertRaisesRegex(
            JournalIntegrityError, "journal_entry_shape_invalid"
        ):
            parse_journal_bytes(encoded, binding_sha256=BINDING)

    def test_noncanonical_journal_bytes_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = self._new_directory_object(Path(raw))
            path = directory / "journal"
            with FileJournal(path, binding_sha256=BINDING) as journal:
                journal.append(
                    operation="install",
                    attempt_id="canonical-line",
                    step_id=INSTALL_STEPS[0],
                    event="intent",
                    disposable_authorization_sha256=INSTALL_PROOF,
                )
            canonical = path.read_bytes().rstrip(b"\n")
            with self.assertRaisesRegex(
                JournalIntegrityError,
                "journal_json_not_canonical",
            ):
                parse_journal_bytes(
                    b" " + canonical + b"\n",
                    binding_sha256=BINDING,
                )

            document = json.loads(canonical.decode("ascii"))
            reordered = dict(reversed(tuple(document.items())))
            reordered_bytes = json.dumps(
                reordered,
                sort_keys=False,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")
            self.assertNotEqual(reordered_bytes, canonical)
            with self.assertRaisesRegex(
                JournalIntegrityError,
                "journal_json_not_canonical",
            ):
                parse_journal_bytes(
                    reordered_bytes + b"\n",
                    binding_sha256=BINDING,
                )

    def test_schema_matches_exact_journal_shape(self) -> None:
        schema = json.loads(
            (
                ROOT / "ops/governed_memory/installation/journal.schema.json"
            ).read_text(encoding="utf-8")
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertEqual(
            schema["properties"]["operation"]["enum"],
            ["install", "rollback", "compensation"],
        )

    @staticmethod
    def _new_directory_object(temp: Path) -> Path:
        directory = temp / "journal"
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)
        return directory


if __name__ == "__main__":
    unittest.main()
