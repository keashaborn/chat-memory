from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools.governed_memory_validation import (
    durable_live_proof_receipt as durable,
)
from tools.governed_memory_validation import (
    execute_phase9_disposable_live_proof_controller as subject,
)
from tools.governed_memory_validation import phase9_permitted_candidate


COMMIT = "a" * 40
TREE = "b" * 40
PACKAGE = "c" * 64
RUNTIME = "d" * 64
CONTRACT = "e" * 64
SUCCESSOR = "f" * 64
PERMIT = "1" * 64
DISPOSITION_TOMBSTONE = "2" * 64
CAPSULE = "4" * 64
SCHEMA = b"exact-live-proof-schema"
ENVIRONMENT = {
    "CHAT_MEMORY_LEASE_ID": "lease-1",
    "CODEX_TASK_ID": "task-1",
    "CODEX_THREAD_ID": "thread-1",
}


def candidate() -> phase9_permitted_candidate.PermittedCandidateAuthority:
    return phase9_permitted_candidate.PermittedCandidateAuthority(
        candidate_git_commit=COMMIT,
        candidate_git_tree=TREE,
        package_manifest_sha256=PACKAGE,
        controller_runtime_receipt_sha256=RUNTIME,
        contract_sha256=CONTRACT,
        successor_attempt_identity_sha256=SUCCESSOR,
        source_blobs=(("fixed.py", "5" * 40),),
    )


def full_disposition() -> dict[str, object]:
    return {
        "result": subject.staged_prefix_disposition.RESULT,
        "contract_sha256": CONTRACT,
        "corrected_generation": (
            subject.disposition_entrypoint.CORRECTED_GENERATION
        ),
        "corrected_package_manifest_sha256": PACKAGE,
        "corrected_controller_runtime_receipt_sha256": RUNTIME,
        "corrected_attempt_identity_sha256": SUCCESSOR,
        "failed_evidence_preserved_in_place": True,
        "no_store_or_service_effects_proven": True,
        "deletion_performed": False,
        "provider_calls": 0,
        "production_data_read": False,
        "activation_performed": False,
        "tombstone_sha256": DISPOSITION_TOMBSTONE,
    }


def full_live_receipt() -> dict[str, object]:
    hashes = {
        key: "6" * 64
        for key in durable._RECEIPT_KEYS
        if key.endswith("sha256") or key.endswith("execution_id")
    }
    receipt: dict[str, object] = {
        **hashes,
        "schema_version": durable.LIVE_PROOF_RECEIPT_SCHEMA,
        "result": "disposable_install_crash_resume_and_empty_rollback_proved",
        "observation_scope": (
            "one_bounded_disposable_linux_execution_terminal_snapshot"
        ),
        "candidate_git_commit": COMMIT,
        "candidate_git_tree": TREE,
        "package_manifest_sha256": PACKAGE,
        "controller_runtime_receipt_sha256": RUNTIME,
        "live_proof_receipt_schema_sha256": hashlib.sha256(SCHEMA).hexdigest(),
        "recovery_capsule_sha256": CAPSULE,
        "authorization_text_sha256": durable.EXPECTED_AUTHORIZATION_TEXT_SHA256,
        "start_authority_pair_claimed_atomically": True,
        "recovery_capsule_published_before_first_install_effect": True,
        "recovery_reservation_claimed_before_first_install_effect": True,
        "ephemeral_private_signer_retained_at_execution_start": False,
        "recovery_capsule_retained_at_terminal_observation": True,
        "install_process_death_observed": True,
        "install_exact_resume_completed": True,
        "cold_controller_process_restart_terminal_postflight_verified": True,
        "cold_restart_scope": "controller_worker_process_only",
        "host_reboot_proven": False,
        "persistent_store_restart_supervision_and_boot_recovery_proven": False,
        "rollback_process_death_observed": True,
        "rollback_exact_resume_completed": True,
        "public_install_entrypoint_used": True,
        "public_empty_rollback_entrypoint_used": True,
        "fresh_r06_semantic_empty_recheck_required": True,
        "completed_public_rollback_replayed": True,
        "exact_rollback_resources_absent_count": 15,
        "exact_resources_absent_at_terminal_observation": True,
        "terminal_absence_is_continuous_guarantee": False,
        "stores_installed_at_terminal_observation": False,
        "stores_supervisor_installed_at_terminal_observation": False,
        "controller_runtime_capability_reverified": True,
        "host_clock_synchronization_preflight_passed": True,
        "source_postgres_read_count": 0,
        "source_postgres_write_count": 0,
        "provider_calls": 0,
        "production_data_read": False,
        "application_services_installed": False,
        "activation_performed": False,
        "issuer_or_host_death_durable_cleanup_proven": False,
    }
    receipt["receipt_sha256"] = subject.runner._document_sha(
        {
            key: value
            for key, value in receipt.items()
            if key != "receipt_sha256"
        }
    )
    if set(receipt) != durable._RECEIPT_KEYS:
        raise AssertionError("synthetic live receipt drifted from the v4 schema")
    return receipt


def successful_guard() -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        args=("guard",), returncode=0, stdout=b"", stderr=b""
    )


class FakeProcess:
    def __init__(
        self,
        outcomes: list[object],
        *,
        returncode: int = 0,
        events: list[tuple[str, object]] | None = None,
    ) -> None:
        self.pid = 424242
        self.returncode: int | None = None
        self._terminal_returncode = returncode
        self._outcomes = list(outcomes)
        self.timeouts: list[float] = []
        self.events = events

    def send_signal(self, observed_signal: int) -> None:
        if self.events is not None:
            self.events.append(("signal", observed_signal))

    def communicate(self, *, timeout: float) -> tuple[bytes, bytes]:
        self.timeouts.append(timeout)
        if self.events is not None:
            self.events.append(("communicate", timeout))
        if not self._outcomes:
            raise AssertionError("unexpected communicate call")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        self.returncode = self._terminal_returncode
        assert isinstance(outcome, tuple)
        return outcome


class FakeCommandRunner:
    def __init__(self, process: FakeProcess) -> None:
        self.process = process
        self.run = mock.Mock(return_value=successful_guard())
        self.Popen = mock.Mock(return_value=process)


class Phase9DisposableLiveProofControllerTests(unittest.TestCase):
    def runtime(self):
        return (
            mock.patch.multiple(
                subject,
                _ISOLATED_RUNTIME_AT_START=True,
                _DONT_WRITE_BYTECODE_AT_START=True,
            ),
            mock.patch.object(subject.sys, "platform", "linux"),
            mock.patch.object(
                subject.sys, "executable", subject.CONTROLLER_PYTHON
            ),
            mock.patch.object(subject.os, "geteuid", return_value=0),
            mock.patch.object(subject.os, "getegid", return_value=0),
        )

    def test_strict_document_rejects_duplicate_noncanonical_or_multiline_output(self) -> None:
        good = subject._canonical({"a": 1}) + b"\n"
        self.assertEqual(subject._strict_document(good, "bad"), {"a": 1})
        for raw in (
            b'{"a":1,"a":1}\n',
            b'{"a": 1}\n',
            b'{"a":1}\n{}\n',
            b'{"a":1}',
        ):
            with self.subTest(raw=raw), self.assertRaisesRegex(
                subject.Phase9DisposableLiveProofControllerError,
                "bad",
            ):
                subject._strict_document(raw, "bad")

    def test_lease_guard_uses_system_python_and_exact_worktree(self) -> None:
        runner = mock.Mock()
        runner.run.return_value = successful_guard()
        subject._require_production_write_lease(
            ENVIRONMENT,
            command_runner=runner,
        )
        call = runner.run.call_args
        self.assertEqual(call.args[0][0], subject.LEASE_GUARD_PYTHON)
        self.assertNotEqual(subject.LEASE_GUARD_PYTHON, subject.CONTROLLER_PYTHON)
        self.assertIn("production-write", call.args[0])
        self.assertIn(str(subject._REPOSITORY_ROOT), call.args[0])
        self.assertEqual(call.kwargs["user"], 1000)
        self.assertEqual(call.kwargs["group"], 1000)
        self.assertEqual(call.kwargs["extra_groups"], ())

    def test_step_refuses_before_spawn_when_initial_lease_is_denied(self) -> None:
        process = FakeProcess([])
        runner = FakeCommandRunner(process)
        runner.run.return_value = subprocess.CompletedProcess(
            args=("guard",), returncode=2, stdout=b"", stderr=b"denied"
        )
        with self.assertRaisesRegex(
            subject.Phase9DisposableLiveProofControllerError,
            "phase9_proof_controller_lease_denied",
        ):
            subject._run_step(
                subject.PUBLISHER_RELATIVE,
                (),
                timeout=10,
                environment=ENVIRONMENT,
                command_runner=runner,
            )
        runner.Popen.assert_not_called()

    def test_step_uses_exact_phase9j_runtime_and_rechecks_lease_while_running(self) -> None:
        receipt = subject._canonical({"result": "ok"}) + b"\n"
        process = FakeProcess(
            [
                subprocess.TimeoutExpired(("step",), 30),
                (receipt, b""),
            ]
        )
        command_runner = FakeCommandRunner(process)
        with (
            mock.patch.object(
                subject, "_open_issuer_control_pipe", return_value=199
            ),
            mock.patch.object(subject, "_close_descriptor") as close,
        ):
            observed = subject._run_step(
                subject.ISSUER_RELATIVE,
                (),
                timeout=120,
                environment=ENVIRONMENT,
                command_runner=command_runner,
            )
        self.assertEqual(observed, {"result": "ok"})
        self.assertEqual(command_runner.run.call_count, 3)
        call = command_runner.Popen.call_args
        self.assertEqual(
            call.args[0],
            (
                subject.CONTROLLER_PYTHON,
                "-I",
                "-B",
                str(subject._REPOSITORY_ROOT / subject.ISSUER_RELATIVE),
            ),
        )
        self.assertTrue(call.kwargs["start_new_session"])
        self.assertTrue(call.kwargs["close_fds"])
        self.assertEqual(call.kwargs["pass_fds"], (198,))
        self.assertEqual(
            call.kwargs["env"][subject.MANAGER_PID_ENVIRONMENT_KEY],
            str(subject.os.getpid()),
        )
        for key, value in ENVIRONMENT.items():
            self.assertEqual(call.kwargs["env"][key], value)
        self.assertEqual(close.call_args_list, [mock.call(198), mock.call(199)])

    def test_nonzero_step_preserves_only_closed_child_error_evidence(self) -> None:
        error_code = "phase9_staged_prefix_disposition_contract_invalid"
        stderr = (error_code + "\n").encode("ascii")
        process = FakeProcess([(b"", stderr)], returncode=1)
        command_runner = FakeCommandRunner(process)

        with self.assertRaises(subject._SubordinateStepFailure) as observed:
            subject._run_step(
                subject.DISPOSITION_RELATIVE,
                (),
                timeout=120,
                environment=ENVIRONMENT,
                command_runner=command_runner,
            )

        failure = observed.exception
        self.assertEqual(failure.error_code, error_code)
        self.assertEqual(failure.exit_status, 1)
        self.assertEqual(failure.stdout_size_bytes, 0)
        self.assertEqual(failure.stderr_size_bytes, len(stderr))
        self.assertEqual(failure.stderr_sha256, hashlib.sha256(stderr).hexdigest())
        self.assertFalse(hasattr(failure, "stdout"))
        self.assertFalse(hasattr(failure, "stderr"))

    def test_zero_exit_malformed_receipt_persists_generic_bound_evidence(self) -> None:
        stdout = b'{"result": "noncanonical"}\n'
        process = FakeProcess([(stdout, b"")], returncode=0)
        command_runner = FakeCommandRunner(process)
        with (
            mock.patch.object(
                subject, "_read_disposition_if_present", return_value=None
            ),
            mock.patch.object(subject, "_persist_failure_evidence") as persist,
            self.assertRaisesRegex(
                subject.Phase9DisposableLiveProofControllerError,
                "^phase9_proof_controller_step_failed$",
            ),
        ):
            subject._run_evidence_bound_step(
                subject.DISPOSITION_RELATIVE,
                timeout=120,
                environment=ENVIRONMENT,
                command_runner=command_runner,
                candidate=candidate(),
                permit={"permit_sha256": PERMIT},
                disposition=None,
            )

        evidence = persist.call_args.args[0]
        self.assertEqual(
            evidence["sanitized_error_code"],
            subject._GENERIC_SUBORDINATE_ERROR,
        )
        self.assertEqual(evidence["subordinate_exit_status"], 0)
        self.assertEqual(evidence["stdout_size_bytes"], len(stdout))
        self.assertEqual(
            evidence["stdout_sha256"], hashlib.sha256(stdout).hexdigest()
        )
        self.assertIsNone(evidence["staged_prefix_tombstone_sha256"])

    def test_untrusted_child_output_is_hashed_but_never_retained(self) -> None:
        stdout = b"private stdout"
        stderr = b"/etc/private/secret-value\nextra\n"
        failure = subject._sanitized_step_failure(
            relative=subject.ISSUER_RELATIVE,
            exit_status=2,
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(
            failure.error_code,
            subject._GENERIC_SUBORDINATE_ERROR,
        )
        self.assertEqual(failure.stdout_sha256, hashlib.sha256(stdout).hexdigest())
        self.assertEqual(failure.stderr_sha256, hashlib.sha256(stderr).hexdigest())
        self.assertFalse(hasattr(failure, "stdout"))
        self.assertFalse(hasattr(failure, "stderr"))

        document = subject._failure_evidence_document(
            failure=failure,
            candidate=candidate(),
            permit={"permit_sha256": PERMIT},
            disposition=full_disposition(),
        )
        encoded = subject._canonical(document)
        self.assertNotIn(stdout, encoded)
        self.assertNotIn(stderr, encoded)
        self.assertNotIn(b"secret-value", encoded)
        self.assertFalse(document["raw_subordinate_output_persisted"])
        self.assertEqual(
            subject._verify_failure_evidence_document(document)[0],
            document,
        )

    def test_bound_failure_evidence_is_create_once_and_replay_exact(self) -> None:
        code = "phase9_live_proof_install_execution_failed"
        stderr = (code + "\n").encode("ascii")
        failure = subject._sanitized_step_failure(
            relative=subject.ISSUER_RELATIVE,
            exit_status=1,
            stdout=b"",
            stderr=stderr,
        )
        document = subject._failure_evidence_document(
            failure=failure,
            candidate=candidate(),
            permit={"permit_sha256": PERMIT},
            disposition=full_disposition(),
        )

        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            state_root.mkdir(mode=0o700)
            state_root.chmod(0o700)
            first = subject._persist_failure_evidence_at(
                state_root,
                document,
                expected_uid=os.getuid(),
                expected_gid=os.getgid(),
            )
            before = first.stat()
            self.assertEqual(stat.S_IMODE(before.st_mode), 0o400)
            self.assertEqual(before.st_nlink, 1)
            self.assertEqual(json.loads(first.read_bytes()), document)

            replay = subject._persist_failure_evidence_at(
                state_root,
                document,
                expected_uid=os.getuid(),
                expected_gid=os.getgid(),
            )
            self.assertEqual(replay, first)
            self.assertEqual(replay.stat().st_ino, before.st_ino)

            first.chmod(0o600)
            first.write_bytes(b"tampered")
            first.chmod(0o400)
            with self.assertRaisesRegex(
                subject.Phase9DisposableLiveProofControllerError,
                "phase9_proof_controller_failure_evidence_persistence_failed",
            ):
                subject._persist_failure_evidence_at(
                    state_root,
                    document,
                    expected_uid=os.getuid(),
                    expected_gid=os.getgid(),
                )

    def test_bound_disposition_and_issuer_failures_persist_before_refusal(self) -> None:
        cases = (
            (
                subject.DISPOSITION_RELATIVE,
                None,
                None,
                "phase9_staged_prefix_disposition_contract_invalid",
            ),
            (
                subject.DISPOSITION_RELATIVE,
                None,
                full_disposition(),
                "phase9_staged_prefix_disposition_evidence_changed",
            ),
            (
                subject.ISSUER_RELATIVE,
                full_disposition(),
                None,
                "phase9_live_proof_install_execution_failed",
            ),
        )
        for relative, disposition, observed_disposition, code in cases:
            with self.subTest(relative=relative):
                failure = subject._sanitized_step_failure(
                    relative=relative,
                    exit_status=1,
                    stdout=b"",
                    stderr=(code + "\n").encode("ascii"),
                )
                with (
                    mock.patch.object(
                        subject, "_run_step", side_effect=failure
                    ),
                    mock.patch.object(
                        subject, "_persist_failure_evidence"
                    ) as persist,
                    mock.patch.object(
                        subject,
                        "_read_disposition_if_present",
                        return_value=observed_disposition,
                    ),
                    self.assertRaisesRegex(
                        subject.Phase9DisposableLiveProofControllerError,
                        "^phase9_proof_controller_step_failed$",
                    ),
                ):
                    subject._run_evidence_bound_step(
                        relative,
                        timeout=120,
                        environment=ENVIRONMENT,
                        command_runner=mock.sentinel.command_runner,
                        candidate=candidate(),
                        permit={"permit_sha256": PERMIT},
                        disposition=disposition,
                    )
                evidence = persist.call_args.args[0]
                self.assertEqual(evidence["step_relative"], relative)
                self.assertEqual(evidence["sanitized_error_code"], code)
                self.assertEqual(
                    evidence["staged_prefix_tombstone_sha256"],
                    None
                    if (
                        disposition is None
                        and observed_disposition is None
                    )
                    else DISPOSITION_TOMBSTONE,
                )

    def test_successful_bound_step_never_persists_failure_evidence(self) -> None:
        with (
            mock.patch.object(subject, "_run_step", return_value={"ok": True}),
            mock.patch.object(subject, "_persist_failure_evidence") as persist,
        ):
            observed = subject._run_evidence_bound_step(
                subject.ISSUER_RELATIVE,
                timeout=120,
                environment=ENVIRONMENT,
                command_runner=mock.sentinel.command_runner,
                candidate=candidate(),
                permit={"permit_sha256": PERMIT},
                disposition=full_disposition(),
            )
        self.assertEqual(observed, {"ok": True})
        persist.assert_not_called()

    def test_periodic_lease_denial_withdraws_control_before_recovery_wait(self) -> None:
        events: list[tuple[str, object]] = []
        timeout = subprocess.TimeoutExpired(("step",), 30)
        process = FakeProcess(
            [timeout, (b"", b"")], returncode=1, events=events
        )
        command_runner = FakeCommandRunner(process)
        command_runner.run.side_effect = [
            successful_guard(),
            subprocess.CompletedProcess(
                args=("guard",), returncode=2, stdout=b"", stderr=b"denied"
            ),
        ]
        with (
            mock.patch.object(
                subject, "_open_issuer_control_pipe", return_value=199
            ),
            mock.patch.object(
                subject,
                "_close_descriptor",
                side_effect=lambda descriptor: events.append(
                    ("close", descriptor)
                ),
            ),
            mock.patch.object(subject.os, "killpg") as killpg,
            self.assertRaisesRegex(
                subject.Phase9DisposableLiveProofControllerError,
                "phase9_proof_controller_lease_denied",
            ),
        ):
            subject._run_step(
                subject.ISSUER_RELATIVE,
                (),
                timeout=120,
                environment=ENVIRONMENT,
                command_runner=command_runner,
            )
        killpg.assert_not_called()
        self.assertEqual(process.returncode, 1)
        self.assertEqual(
            events,
            [
                ("close", 198),
                ("communicate", 30.0),
                ("close", 199),
                ("signal", signal.SIGCONT),
                (
                    "communicate",
                    subject.ISSUER_CONTROLLED_RECOVERY_SECONDS,
                ),
            ],
        )

    def test_outer_timeout_withdraws_control_and_allows_issuer_recovery(self) -> None:
        events: list[tuple[str, object]] = []
        process = FakeProcess([(b"", b"")], returncode=1, events=events)
        command_runner = FakeCommandRunner(process)
        with (
            mock.patch.object(subject.time, "monotonic", side_effect=(0.0, 11.0)),
            mock.patch.object(
                subject, "_open_issuer_control_pipe", return_value=199
            ),
            mock.patch.object(
                subject,
                "_close_descriptor",
                side_effect=lambda descriptor: events.append(
                    ("close", descriptor)
                ),
            ),
            mock.patch.object(subject.os, "killpg") as killpg,
            self.assertRaisesRegex(
                subject.Phase9DisposableLiveProofControllerError,
                "phase9_proof_controller_step_timeout",
            ),
        ):
            subject._run_step(
                subject.ISSUER_RELATIVE,
                (),
                timeout=10,
                environment=ENVIRONMENT,
                command_runner=command_runner,
            )
        killpg.assert_not_called()
        self.assertEqual(process.returncode, 1)
        self.assertEqual(
            events,
            [
                ("close", 198),
                ("close", 199),
                ("signal", signal.SIGCONT),
                (
                    "communicate",
                    subject.ISSUER_CONTROLLED_RECOVERY_SECONDS,
                ),
            ],
        )

    def test_base_exception_withdraws_control_before_recovery_and_propagates(self) -> None:
        events: list[tuple[str, object]] = []
        process = FakeProcess(
            [KeyboardInterrupt(), (b"", b"")],
            returncode=1,
            events=events,
        )
        command_runner = FakeCommandRunner(process)
        with (
            mock.patch.object(
                subject, "_open_issuer_control_pipe", return_value=199
            ),
            mock.patch.object(
                subject,
                "_close_descriptor",
                side_effect=lambda descriptor: events.append(
                    ("close", descriptor)
                ),
            ),
            mock.patch.object(subject.os, "killpg") as killpg,
            self.assertRaises(KeyboardInterrupt),
        ):
            subject._run_step(
                subject.ISSUER_RELATIVE,
                (),
                timeout=120,
                environment=ENVIRONMENT,
                command_runner=command_runner,
            )
        killpg.assert_not_called()
        self.assertEqual(
            events,
            [
                ("close", 198),
                ("communicate", 30.0),
                ("close", 199),
                ("signal", signal.SIGCONT),
                (
                    "communicate",
                    subject.ISSUER_CONTROLLED_RECOVERY_SECONDS,
                ),
            ],
        )

    def test_controlled_recovery_timeout_never_kills_live_issuer(self) -> None:
        events: list[tuple[str, object]] = []
        process = FakeProcess(
            [
                KeyboardInterrupt(),
                subprocess.TimeoutExpired(("issuer-recovery",), 1800),
            ],
            events=events,
        )
        command_runner = FakeCommandRunner(process)

        with (
            mock.patch.object(
                subject, "_open_issuer_control_pipe", return_value=199
            ),
            mock.patch.object(
                subject,
                "_close_descriptor",
                side_effect=lambda descriptor: events.append(
                    ("close", descriptor)
                ),
            ),
            mock.patch.object(subject.os, "killpg") as killpg,
            self.assertRaises(KeyboardInterrupt),
        ):
            subject._run_step(
                subject.ISSUER_RELATIVE,
                (),
                timeout=120,
                environment=ENVIRONMENT,
                command_runner=command_runner,
            )
        killpg.assert_not_called()
        self.assertIsNone(process.returncode)
        recovery_wait = (
            "communicate",
            subject.ISSUER_CONTROLLED_RECOVERY_SECONDS,
        )
        self.assertIn(("signal", signal.SIGCONT), events)
        self.assertIn(recovery_wait, events)
        self.assertEqual(events[-1], recovery_wait)

    def test_controlled_recovery_live_returncode_never_kills_issuer(self) -> None:
        process = mock.Mock(pid=424242, returncode=None)
        process.communicate.return_value = (b"", b"")
        with (
            mock.patch.object(subject, "_close_descriptor") as close,
            mock.patch.object(subject.os, "killpg") as killpg,
            self.assertRaisesRegex(
                subject.Phase9DisposableLiveProofControllerError,
                "phase9_proof_controller_step_cleanup_failed",
            ),
        ):
            subject._wait_for_controlled_issuer_recovery(process, 199)
        close.assert_called_once_with(199)
        process.send_signal.assert_called_once_with(signal.SIGCONT)
        process.communicate.assert_called_once_with(
            timeout=subject.ISSUER_CONTROLLED_RECOVERY_SECONDS
        )
        killpg.assert_not_called()

    def test_short_atomic_child_timeout_still_kills_and_reaps(self) -> None:
        process = FakeProcess(
            [
                subprocess.TimeoutExpired(("publisher",), 30),
                (b"", b""),
            ],
            returncode=-signal.SIGKILL,
        )
        command_runner = FakeCommandRunner(process)
        command_runner.run.side_effect = [successful_guard(), successful_guard()]
        with (
            mock.patch.object(
                subject.time, "monotonic", side_effect=(0.0, 0.0, 11.0)
            ),
            mock.patch.object(subject.os, "killpg") as killpg,
            self.assertRaisesRegex(
                subject.Phase9DisposableLiveProofControllerError,
                "phase9_proof_controller_step_timeout",
            ),
        ):
            subject._run_step(
                subject.PUBLISHER_RELATIVE,
                (),
                timeout=10,
                environment=ENVIRONMENT,
                command_runner=command_runner,
            )
        killpg.assert_called_once_with(process.pid, signal.SIGKILL)

    def test_direct_execution_gate_precedes_all_repository_imports(self) -> None:
        source = Path(subject.__file__).read_text(encoding="utf-8")
        first_repository_import = source.index(
            "from tools.governed_memory_validation import"
        )
        for marker in (
            "_ISOLATED_RUNTIME_AT_START",
            "_DONT_WRITE_BYTECODE_AT_START",
            'and sys.platform == "linux"',
            "and sys.executable == _PREIMPORT_CONTROLLER_PYTHON",
            "_PREIMPORT_EXPECTED_CANDIDATE_REF",
            '"status",\n                "--porcelain=v1"',
            "*_PREIMPORT_SOURCE_PATHS",
            "not _preimport_exact_candidate_valid()",
            "cooperative Phase-9-only boundary",
            "raise SystemExit(1)",
        ):
            with self.subTest(marker=marker):
                self.assertLess(source.index(marker), first_repository_import)
        self.assertFalse(hasattr(subject, "_exclusive_controller_lock"))
        self.assertFalse(hasattr(subject, "CONTROLLER_LOCK_PATH"))

    def test_preimport_candidate_gate_requires_fixed_tag_head_and_source_blobs(self) -> None:
        repository_root = str(subject._REPOSITORY_ROOT)

        def git(root: str, *arguments: str) -> bytes:
            self.assertEqual(root, repository_root)
            if arguments == ("rev-parse", "--show-toplevel"):
                return (repository_root + "\n").encode("utf-8")
            if arguments == (
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ):
                return b""
            if arguments[:4] == (
                "ls-files",
                "--error-unmatch",
                "--stage",
                "--",
            ):
                return b"tracked\n"
            raise AssertionError(arguments)

        def git_object(root: str, *arguments: str) -> str:
            self.assertEqual(root, repository_root)
            if arguments[-1].endswith("^{commit}"):
                return "a" * 40
            if arguments[-1].endswith("^{tree}"):
                return "b" * 40
            for index, relative in enumerate(subject._PREIMPORT_SOURCE_PATHS):
                value = f"{index + 1:x}" * 40
                if arguments == ("rev-parse", "HEAD:" + relative):
                    return value
                if arguments == (
                    "hash-object",
                    str(subject._REPOSITORY_ROOT / relative),
                ):
                    return value
            raise AssertionError(arguments)

        with (
            mock.patch.object(subject, "_preimport_git", side_effect=git),
            mock.patch.object(
                subject, "_preimport_git_object", side_effect=git_object
            ),
        ):
            self.assertTrue(subject._preimport_exact_candidate_valid())

        def drifted_git_object(root: str, *arguments: str) -> str:
            value = git_object(root, *arguments)
            if arguments == (
                "hash-object",
                str(
                    subject._REPOSITORY_ROOT
                    / subject._PREIMPORT_CONTROLLER_RELATIVE
                ),
            ):
                return "f" * 40
            return value

        with (
            mock.patch.object(subject, "_preimport_git", side_effect=git),
            mock.patch.object(
                subject,
                "_preimport_git_object",
                side_effect=drifted_git_object,
            ),
        ):
            self.assertFalse(subject._preimport_exact_candidate_valid())

    def test_direct_execution_wrong_runtime_refuses_in_preimport_preamble(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            wrong_python = Path(temporary) / "wrong-python"
            wrong_python.symlink_to(Path(sys.executable).resolve(strict=True))
            completed = subprocess.run(
                (
                    str(wrong_python),
                    "-I",
                    "-B",
                    str(Path(subject.__file__).resolve()),
                ),
                cwd="/",
                env={
                    "PATH": "/usr/bin:/bin",
                    "LANG": "C",
                    "LC_ALL": "C",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(
            completed.stderr,
            b"phase9_proof_controller_runtime_isolation_required\n",
        )

    def test_require_disposition_reverifies_exact_staged_prefix_tombstone(self) -> None:
        selected = candidate()
        expected_paths = object()
        expected_expectation = object()
        disposition = full_disposition()
        with (
            mock.patch.object(
                subject.staged_prefix_disposition,
                "production_disposition_paths",
                return_value=expected_paths,
            ) as paths,
            mock.patch.object(
                subject.disposition_entrypoint,
                "_expectation",
                return_value=expected_expectation,
            ) as expectation,
            mock.patch.object(
                subject.staged_prefix_disposition,
                "verify_staged_prefix_tombstone",
                return_value=disposition,
            ) as verify,
        ):
            observed = subject._require_disposition(selected)
        self.assertEqual(observed, disposition)
        paths.assert_called_once_with()
        expectation.assert_called_once_with(subject.staged_prefix_disposition)
        verify.assert_called_once_with(expected_paths, expected_expectation)

    def test_require_disposition_refuses_missing_or_unpreserved_tombstone(self) -> None:
        selected = candidate()
        invalid = full_disposition()
        invalid["failed_evidence_preserved_in_place"] = False
        for receipt in (None, invalid):
            with (
                self.subTest(receipt=receipt),
                mock.patch.object(
                    subject.disposition_entrypoint,
                    "_expectation",
                    return_value=object(),
                ),
                mock.patch.object(
                    subject.staged_prefix_disposition,
                    "production_disposition_paths",
                    return_value=object(),
                ),
                mock.patch.object(
                    subject.staged_prefix_disposition,
                    "verify_staged_prefix_tombstone",
                    return_value=receipt,
                ),
                self.assertRaisesRegex(
                    subject.Phase9DisposableLiveProofControllerError,
                    "phase9_proof_controller_disposition_invalid",
                ),
            ):
                subject._require_disposition(selected)

    def test_exact_chain_has_no_standalone_bootstrap_and_uses_real_v4_verifiers(self) -> None:
        selected = candidate()
        permit = {
            "permit_sha256": PERMIT,
            "candidate_git_commit": COMMIT,
            "candidate_git_tree": TREE,
            "package_manifest_sha256": PACKAGE,
            "controller_runtime_receipt_sha256": RUNTIME,
        }
        disposition = full_disposition()
        disposition_step = {
            key: disposition[key]
            for key in (
                "contract_sha256",
                "corrected_controller_runtime_receipt_sha256",
                "corrected_generation",
                "corrected_package_manifest_sha256",
                "result",
                "tombstone_sha256",
            )
        }
        live = full_live_receipt()
        artifacts = {durable.LIVE_PROOF_RECEIPT_SCHEMA_RELATIVE: SCHEMA}
        verified_durable = durable.verify_promotable_live_proof_receipt(
            inputs=subject.runner.ProofInputs(
                candidate_git_commit=COMMIT,
                candidate_git_tree=TREE,
                package_manifest_sha256=PACKAGE,
                controller_runtime_receipt_sha256=RUNTIME,
            ),
            artifacts=artifacts,
            capsule_sha256=CAPSULE,
            receipt=live,
        )
        calls: list[tuple[str, tuple[str, ...], int]] = []

        def run_step(
            relative: str,
            arguments: tuple[str, ...],
            *,
            timeout: int,
            **unused: object,
        ) -> dict[str, object]:
            calls.append((relative, arguments, timeout))
            return {
                subject.PUBLISHER_RELATIVE: permit,
                subject.DISPOSITION_RELATIVE: disposition_step,
                subject.ISSUER_RELATIVE: live,
            }[relative]

        isolation, platform, executable, geteuid, getegid = self.runtime()
        with (
            isolation,
            platform,
            executable,
            geteuid,
            getegid,
            mock.patch.object(subject, "_run_step", side_effect=run_step),
            mock.patch.object(
                subject.phase9_permitted_candidate,
                "require_exact_permitted_candidate",
                return_value=selected,
            ),
            mock.patch.object(subject, "_require_disposition", return_value=disposition),
            mock.patch.object(
                subject.runner, "_load_release", return_value=(b"manifest", artifacts)
            ),
            mock.patch.object(
                subject.durable_live_proof_receipt,
                "read_verified_promotable_live_receipt_if_present",
                return_value=verified_durable,
            ),
            mock.patch.object(subject, "_require_production_write_lease") as guard,
        ):
            result = subject.execute_phase9_disposable_live_proof_controller(
                environment=ENVIRONMENT
            )
        self.assertEqual(
            [relative for relative, _, _ in calls],
            [
                subject.PUBLISHER_RELATIVE,
                subject.DISPOSITION_RELATIVE,
                subject.ISSUER_RELATIVE,
            ],
        )
        self.assertFalse(hasattr(subject, "BOOTSTRAP_RELATIVE"))
        self.assertEqual(calls[-1][2], subject.ISSUER_TIMEOUT_SECONDS)
        self.assertEqual(calls[-1][1], ())
        self.assertEqual(guard.call_count, 2)
        self.assertEqual(result["result"], "exact_disposable_live_proof_completed")
        self.assertEqual(
            result["staged_prefix_tombstone_sha256"], DISPOSITION_TOMBSTONE
        )
        self.assertTrue(result["exact_resources_absent"])
        self.assertFalse(result["activation_performed"])

    def test_bad_disposition_stops_before_issuer(self) -> None:
        selected = candidate()
        calls: list[str] = []

        def run_step(
            relative: str,
            arguments: tuple[str, ...],
            **unused: object,
        ) -> dict[str, object]:
            del arguments
            calls.append(relative)
            if relative == subject.PUBLISHER_RELATIVE:
                return {
                    "permit_sha256": PERMIT,
                    "candidate_git_commit": COMMIT,
                    "candidate_git_tree": TREE,
                    "package_manifest_sha256": PACKAGE,
                    "controller_runtime_receipt_sha256": RUNTIME,
                }
            return {
                "contract_sha256": CONTRACT,
                "tombstone_sha256": DISPOSITION_TOMBSTONE,
            }

        isolation, platform, executable, geteuid, getegid = self.runtime()
        with (
            isolation,
            platform,
            executable,
            geteuid,
            getegid,
            mock.patch.object(subject, "_run_step", side_effect=run_step),
            mock.patch.object(
                subject.phase9_permitted_candidate,
                "require_exact_permitted_candidate",
                return_value=selected,
            ),
            mock.patch.object(
                subject,
                "_require_disposition",
                side_effect=subject.Phase9DisposableLiveProofControllerError(
                    "phase9_proof_controller_disposition_invalid"
                ),
            ),
            mock.patch.object(subject, "_require_production_write_lease"),
            self.assertRaisesRegex(
                subject.Phase9DisposableLiveProofControllerError,
                "phase9_proof_controller_disposition_invalid",
            ),
        ):
            subject.execute_phase9_disposable_live_proof_controller(
                environment=ENVIRONMENT
            )
        self.assertEqual(
            calls,
            [subject.PUBLISHER_RELATIVE, subject.DISPOSITION_RELATIVE],
        )

    def test_runtime_and_issuer_bound_fit_inside_four_hour_lease(self) -> None:
        self.assertEqual(
            subject.CONTROLLER_RUNTIME_RECEIPT_SHA256,
            "ed0b3518484eec292f35f8b996bccefd01e13038105634016b4253a8c2a732a7",
        )
        self.assertIn(subject.CONTROLLER_RUNTIME_RECEIPT_SHA256, subject.CONTROLLER_PYTHON)
        self.assertGreaterEqual(subject.ISSUER_TIMEOUT_SECONDS, 2 * 60 * 60)
        self.assertLess(subject.ISSUER_TIMEOUT_SECONDS, 4 * 60 * 60)

    def test_wrong_interpreter_refuses_before_lease(self) -> None:
        isolation, platform, executable, geteuid, getegid = self.runtime()
        with (
            isolation,
            platform,
            executable,
            geteuid,
            getegid,
            mock.patch.object(subject.sys, "executable", "/usr/bin/python3.12"),
            mock.patch.object(subject, "_require_production_write_lease") as guard,
            self.assertRaisesRegex(
                subject.Phase9DisposableLiveProofControllerError,
                "phase9_proof_controller_runtime_isolation_required",
            ),
        ):
            subject.execute_phase9_disposable_live_proof_controller(
                environment=ENVIRONMENT
            )
        guard.assert_not_called()

    def test_controller_accepts_no_operational_arguments(self) -> None:
        with self.assertRaisesRegex(
            subject.Phase9DisposableLiveProofControllerError,
            "phase9_proof_controller_arguments_refused",
        ):
            subject.main(("--permit", "/tmp/foreign"))


if __name__ == "__main__":
    unittest.main()
