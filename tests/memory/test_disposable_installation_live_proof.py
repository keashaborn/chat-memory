from __future__ import annotations

import ast
import contextlib
from datetime import datetime, timezone
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tools.governed_memory_validation import (
    issue_disposable_installation_live_proof as issuer,
)
from tools.governed_memory_validation import (
    run_disposable_installation_live_proof as proof,
)
from tools.governed_memory_install.execution_lock import (
    ExecutionLockBusyError,
    ExecutionLockSecurityError,
    GlobalExecutionLock,
)


COMMIT = "1" * 40
TREE = "2" * 40
PACKAGE = "3" * 64
RUNTIME = "4" * 64
INSTALL_EXECUTION = "5" * 64
ROLLBACK_EXECUTION = "6" * 64
DISPOSITION_CONTRACT = "a" * 64
PREDECESSOR_ATTEMPT = "b" * 64
REAL_RUNNER_AUTHORITY_CHECK = proof._require_runner_process_authority


def inputs() -> proof.ProofInputs:
    return proof.ProofInputs(
        candidate_git_commit=COMMIT,
        candidate_git_tree=TREE,
        package_manifest_sha256=PACKAGE,
        controller_runtime_receipt_sha256=RUNTIME,
    )


def disposition_receipt() -> dict[str, object]:
    successor = (
        issuer.pre_effect_disposition.production_successor_attempt_identity_sha256(
            package_manifest_sha256=PACKAGE,
            controller_runtime_receipt_sha256=RUNTIME,
        )
    )
    return {
        "schema_version": issuer.pre_effect_disposition.RECEIPT_SCHEMA,
        "result": issuer.pre_effect_disposition.RESULT,
        "contract_sha256": DISPOSITION_CONTRACT,
        "predecessor_attempt_identity_sha256": PREDECESSOR_ATTEMPT,
        "successor_attempt_identity_sha256": successor,
    }


def artifacts() -> dict[str, bytes]:
    contract = {"exact_targets": {"fixed": "/one"}}
    return {
        "ops/governed_memory/installation/current/contract.json": (
            json.dumps(contract, indent=2).encode("ascii") + b"\n"
        ),
        "ops/governed_memory/installation/current/controller_plan.json": (
            json.dumps({"plan": "fixed"}, indent=2).encode("ascii") + b"\n"
        ),
    }


def context() -> proof.ProofContext:
    recovery_capsule = proof.VerifiedRecoveryCapsule(
        raw=b"{}",
        document={},
        install_documents=proof.InstallDocuments(b"{}", b"{}", b"{}", "7" * 64),
        rollback_delegation={},
        key_id="7" * 64,
        rollback_nonce="8" * 64,
    )
    return proof.ProofContext(
        inputs=inputs(),
        package_manifest=b"{}",
        artifacts={},
        runtime_receipt=b"{}",
        install_documents=recovery_capsule.install_documents,
        verified_scope_capability=object(),
        verified_package_capability=object(),
        verified_runtime_capability=object(),
        verified_recovery_delegation_capability=object(),
        recovery_capsule=recovery_capsule,
    )


class DisposableInstallationLiveProofTests(unittest.TestCase):
    def setUp(self) -> None:
        self._runner_authority_patcher = mock.patch.object(
            proof,
            "_require_runner_process_authority",
        )
        self._runner_authority_patcher.start()
        self.addCleanup(self._runner_authority_patcher.stop)

    def _issuer_guard_mocks(
        self,
    ) -> tuple[
        mock._patch,
        mock._patch,
        mock._patch,
        mock._patch,
        mock._patch,
        mock._patch,
        mock._patch,
        mock._patch,
        mock._patch,
        mock._patch,
    ]:
        guard = mock.Mock()
        guard.__enter__ = mock.Mock(return_value=guard)
        guard.__exit__ = mock.Mock(return_value=None)
        guard.descriptor = 9
        capsule = proof.VerifiedRecoveryCapsule(
            raw=b"retained-capsule",
            document={},
            install_documents=proof.InstallDocuments(
                b"{}", b"{}", b"{}", "7" * 64
            ),
            rollback_delegation={},
            key_id="7" * 64,
            rollback_nonce="8" * 64,
        )
        return (
            mock.patch.object(issuer, "_require_closed_issuer_runtime"),
            mock.patch.object(issuer, "_require_active_manager_authority"),
            mock.patch.object(issuer, "_ensure_live_proof_guard_parent"),
            mock.patch.object(issuer, "GlobalExecutionLock", return_value=guard),
            mock.patch.object(
                issuer,
                "reconcile_capsule_publication",
                return_value=issuer.CapsulePublicationObservation(
                    state="published",
                    capsule=capsule,
                    capsule_sha256="a" * 64,
                    inode=(1, 2),
                ),
            ),
            mock.patch.object(
                issuer,
                "_require_production_pre_effect_disposition",
                return_value=disposition_receipt(),
            ),
            mock.patch.object(
                issuer.substrate_bootstrap,
                "bootstrap_phase9_disposable_store_substrate",
            ),
            mock.patch.object(issuer.runner, "_prepare_fixed_substrate"),
            mock.patch.object(
                issuer,
                "_run_start_authority_pair_mode",
                return_value={
                    "result": "exact_execution_resumed",
                    "recovery_capsule_sha256": "a" * 64,
                    "recovery_capsule_device": 1,
                    "recovery_capsule_inode": 2,
                    "recovery_reservation_claim_sha256": "b" * 64,
                    "install_authority_claim_sha256": "c" * 64,
                    "start_authority_pair_claimed_atomically": True,
                },
            ),
            mock.patch.object(
                issuer.durable_live_proof_receipt,
                "read_verified_promotable_live_receipt_if_present",
                return_value=None,
            ),
        )

    def test_worker_modes_are_closed_and_exact(self) -> None:
        self.assertEqual(
            tuple(item.value for item in proof.WorkerMode),
            (
                "worker-start-or-recover",
                "worker-recover-only",
                "install",
                "resume-install",
                "rollback",
                "resume-rollback",
                "verify-absence",
            ),
        )

    def test_cooperative_boundary_stop_is_fixed_and_aborts_before_next_effect(
        self,
    ) -> None:
        cases = (
            (
                proof.WorkerMode.RESUME_INSTALL,
                proof.process_death_arm_receipt.INSTALL_BOUNDARY_KIND,
                proof.DurableJournal,
                "append_journal",
                proof.INSTALL_KILL_STEP,
            ),
            (
                proof.WorkerMode.ROLLBACK,
                proof.process_death_arm_receipt.EMPTY_ROLLBACK_BOUNDARY_KIND,
                proof.DurableRollbackJournal,
                "append",
                proof.ROLLBACK_KILL_STEP,
            ),
        )
        for mode, kind, journal_type, method_name, step_id in cases:
            events: list[str] = []
            record = mock.Mock(step_id=step_id, event=proof.JOURNAL_APPLIED_EVENT)

            def original_append(
                unused_instance: object,
                observed_record: object,
            ) -> None:
                self.assertIs(observed_record, record)
                events.append("durable-append-returned")

            def dispatch(
                unused_mode: proof.WorkerMode,
                unused_context: object,
            ) -> dict[str, object]:
                getattr(journal_type, method_name)(object(), record)
                events.append("next-effect-dispatched")
                return {}

            def stop_self(observed_pid: int, observed_signal: int) -> None:
                self.assertEqual(observed_pid, os.getpid())
                self.assertEqual(observed_signal, signal.SIGSTOP)
                events.append("self-sigstop-returned")

            with self.subTest(mode=mode, kind=kind), mock.patch.object(
                journal_type,
                method_name,
                original_append,
            ), mock.patch.object(
                proof,
                "_dispatch_worker",
                side_effect=dispatch,
            ), mock.patch.object(
                proof.os,
                "kill",
                side_effect=stop_self,
            ), self.assertRaisesRegex(
                proof._CooperativeBoundaryStopAbort,
                "unexpectedly_resumed",
            ):
                proof._dispatch_worker_with_cooperative_boundary_stop(
                    mode,
                    context(),
                    boundary_kind=kind,
                )
            self.assertEqual(
                events,
                ["durable-append-returned", "self-sigstop-returned"],
            )

    def test_cooperative_boundary_stop_refuses_wrong_pair_and_is_absent_normally(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            proof.LiveProofError,
            "cooperative_stop_boundary_invalid",
        ):
            proof._cooperative_boundary_target(
                proof.WorkerMode.VERIFY_ABSENCE,
                proof.process_death_arm_receipt.INSTALL_BOUNDARY_KIND,
            )
        expected = {"result": True}
        with mock.patch.object(
            proof,
            "_dispatch_worker",
            return_value=expected,
        ) as dispatch:
            observed = proof._dispatch_worker_with_cooperative_boundary_stop(
                proof.WorkerMode.VERIFY_ABSENCE,
                context(),
                boundary_kind=None,
            )
        self.assertIs(observed, expected)
        dispatch.assert_called_once()

        source = Path(proof.__file__).read_text(encoding="utf-8")
        self.assertNotIn("os.kill(pid, signal.SIGSTOP)", source)

    @unittest.skipUnless(
        sys.platform == "linux" and hasattr(os, "fork"),
        "Linux fork and stop semantics required",
    )
    def test_real_cooperative_boundary_self_stops_and_continuation_aborts(
        self,
    ) -> None:
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            record = mock.Mock(
                step_id=proof.INSTALL_KILL_STEP,
                event=proof.JOURNAL_APPLIED_EVENT,
            )

            def durable_append(
                unused_instance: object,
                unused_record: object,
            ) -> None:
                os.write(write_fd, b"D")

            def dispatch(
                unused_mode: proof.WorkerMode,
                unused_context: proof.ProofContext,
            ) -> dict[str, object]:
                proof.DurableJournal.append_journal(object(), record)
                os.write(write_fd, b"N")
                return {}

            try:
                with (
                    mock.patch.object(
                        proof.DurableJournal,
                        "append_journal",
                        durable_append,
                    ),
                    mock.patch.object(
                        proof,
                        "_dispatch_worker",
                        side_effect=dispatch,
                    ),
                ):
                    proof._dispatch_worker_with_cooperative_boundary_stop(
                        proof.WorkerMode.RESUME_INSTALL,
                        context(),
                        boundary_kind=(
                            proof.process_death_arm_receipt.INSTALL_BOUNDARY_KIND
                        ),
                    )
            except BaseException:
                os.close(write_fd)
                os._exit(7)
            os.close(write_fd)
            os._exit(0)

        os.close(write_fd)
        child_owned = True
        try:
            waited, stopped_status = os.waitpid(pid, os.WUNTRACED)
            self.assertEqual(waited, pid)
            self.assertTrue(os.WIFSTOPPED(stopped_status))
            self.assertEqual(os.WSTOPSIG(stopped_status), signal.SIGSTOP)
            ready, unused_write, unused_error = select.select(
                [read_fd], [], [], 2.0
            )
            self.assertEqual(ready, [read_fd])
            self.assertEqual(os.read(read_fd, 1), b"D")
            os.kill(pid, signal.SIGCONT)
            waited, exit_status = os.waitpid(pid, 0)
            child_owned = False
            self.assertEqual(waited, pid)
            self.assertTrue(os.WIFEXITED(exit_status))
            self.assertEqual(os.WEXITSTATUS(exit_status), 7)
            self.assertEqual(os.read(read_fd, 2), b"")
        finally:
            if child_owned:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass
            os.close(read_fd)

    def test_release_cli_accepts_only_four_immutable_identities(self) -> None:
        mode, parsed = proof.parse_inputs(
            (
                "start-or-recover",
                "--candidate-git-commit",
                COMMIT,
                "--candidate-git-tree",
                TREE,
                "--package-manifest-sha256",
                PACKAGE,
                "--controller-runtime-receipt-sha256",
                RUNTIME,
            )
        )
        self.assertEqual(
            (mode, parsed),
            (proof.RunnerMode.START_OR_RECOVER.value, inputs()),
        )
        destinations = {
            action.dest for action in proof._parser()._actions if action.dest != "help"
        }
        self.assertEqual(
            destinations,
            {
                "mode",
                "candidate_git_commit",
                "candidate_git_tree",
                "package_manifest_sha256",
                "controller_runtime_receipt_sha256",
            },
        )
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            proof.parse_inputs(("run", "--path", "/tmp/escape"))
        with self.assertRaisesRegex(
            proof.LiveProofError, "phase9_live_proof_worker_context_required"
        ):
            proof.main(
                (
                    "install",
                    "--candidate-git-commit",
                    COMMIT,
                    "--candidate-git-tree",
                    TREE,
                    "--package-manifest-sha256",
                    PACKAGE,
                    "--controller-runtime-receipt-sha256",
                    RUNTIME,
                )
            )

    def test_imported_operational_entrypoints_refuse_without_sealed_state(
        self,
    ) -> None:
        calls = (
            proof.preclaim_staged_start_authority_pair,
            proof.verify_published_start_authority_pair,
            proof.start_or_recover_live_proof,
            proof.recover_live_proof,
            proof.run_live_proof,
        )
        with (
            mock.patch.object(
                proof,
                "_require_runner_process_authority",
                REAL_RUNNER_AUTHORITY_CHECK,
            ),
            mock.patch.object(proof, "_PREIMPORT_RUNNER_AUTHORITY", None),
        ):
            for call in calls:
                with (
                    self.subTest(entrypoint=call.__name__),
                    self.assertRaisesRegex(
                        proof.LiveProofError,
                        "^phase9_live_proof_runner_authority_invalid$",
                    ),
                ):
                    call(inputs())

    def test_programmatic_start_main_refuses_public_ids_without_sealed_state(
        self,
    ) -> None:
        arguments = (
            "start-or-recover",
            "--candidate-git-commit",
            COMMIT,
            "--candidate-git-tree",
            TREE,
            "--package-manifest-sha256",
            PACKAGE,
            "--controller-runtime-receipt-sha256",
            RUNTIME,
        )
        with (
            mock.patch.object(
                proof,
                "_require_runner_process_authority",
                REAL_RUNNER_AUTHORITY_CHECK,
            ),
            mock.patch.object(proof, "_PREIMPORT_RUNNER_AUTHORITY", None),
            self.assertRaisesRegex(
                proof.LiveProofError,
                "^phase9_live_proof_runner_authority_invalid$",
            ),
        ):
            proof.main(arguments)

    def test_recovery_mode_requires_sealed_cli_but_not_live_issuer_lease(
        self,
    ) -> None:
        release_root = "/opt/governed-memory-controller/releases/" + PACKAGE
        runtime_python = (
            "/opt/governed-memory-controller/runtimes/"
            + RUNTIME
            + "/bin/python"
        )
        state = (
            proof.RunnerMode.RECOVER_ONLY.value,
            COMMIT,
            TREE,
            PACKAGE,
            RUNTIME,
            release_root,
            runtime_python,
            0,
            0,
            0,
            0,
            "",
            "",
            "",
            "",
        )
        with (
            mock.patch.object(proof, "_PREIMPORT_RUNNER_AUTHORITY", state),
            mock.patch.object(
                proof,
                "_preimport_validate_sealed_invocation",
                return_value=(release_root, runtime_python),
            ) as validate,
            mock.patch.object(proof.subprocess, "run") as lease_guard,
        ):
            REAL_RUNNER_AUTHORITY_CHECK(proof.RunnerMode.RECOVER_ONLY)
        validate.assert_called_once_with(
            proof.RunnerMode.RECOVER_ONLY.value,
            PACKAGE,
            RUNTIME,
        )
        lease_guard.assert_not_called()

    def test_start_mode_reproves_seal_lineage_control_and_live_lease(
        self,
    ) -> None:
        release_root = "/opt/governed-memory-controller/releases/" + PACKAGE
        runtime_python = (
            "/opt/governed-memory-controller/runtimes/"
            + RUNTIME
            + "/bin/python"
        )
        lineage = (
            101,
            102,
            7,
            8,
            "/candidate",
            "lease-1",
            "task-1",
            "thread-1",
        )
        state = (
            proof.RunnerMode.START_OR_RECOVER.value,
            COMMIT,
            TREE,
            PACKAGE,
            RUNTIME,
            release_root,
            runtime_python,
            *lineage,
        )
        with (
            mock.patch.object(proof, "_PREIMPORT_RUNNER_AUTHORITY", state),
            mock.patch.object(
                proof,
                "_preimport_validate_sealed_invocation",
                return_value=(release_root, runtime_python),
            ) as validate_seal,
            mock.patch.object(
                proof,
                "_preimport_validate_issuer_authority",
                return_value=lineage,
            ) as validate_lineage,
            mock.patch.object(
                proof, "_require_active_runner_lease"
            ) as validate_lease,
        ):
            REAL_RUNNER_AUTHORITY_CHECK(proof.RunnerMode.START_OR_RECOVER)
        validate_seal.assert_called_once_with(
            proof.RunnerMode.START_OR_RECOVER.value,
            PACKAGE,
            RUNTIME,
        )
        validate_lineage.assert_called_once_with(runtime_python=runtime_python)
        validate_lease.assert_called_once_with(state)

    def test_start_mode_refuses_each_bound_identity_drift(self) -> None:
        release_root = "/opt/governed-memory-controller/releases/" + PACKAGE
        runtime_python = (
            "/opt/governed-memory-controller/runtimes/"
            + RUNTIME
            + "/bin/python"
        )
        lineage = (
            101,
            102,
            7,
            8,
            "/candidate",
            "lease-1",
            "task-1",
            "thread-1",
        )
        state = (
            proof.RunnerMode.START_OR_RECOVER.value,
            COMMIT,
            TREE,
            PACKAGE,
            RUNTIME,
            release_root,
            runtime_python,
            *lineage,
        )
        drifts = {
            "mode": (0, proof.RunnerMode.RECOVER_ONLY.value),
            "commit": (1, "0" * 39),
            "tree": (2, "0" * 39),
            "package": (3, "0" * 63),
            "runtime": (4, "0" * 63),
            "release": (5, release_root + "-drift"),
            "interpreter": (6, runtime_python + "-drift"),
            "issuer": (7, 103),
            "manager": (8, 104),
            "control-device": (9, 9),
            "control-inode": (10, 10),
            "repository": (11, "/candidate-drift"),
            "lease": (12, "lease-2"),
            "task": (13, "task-2"),
            "thread": (14, "thread-2"),
        }
        for name, (index, value) in drifts.items():
            drifted = list(state)
            drifted[index] = value
            with (
                self.subTest(identity=name),
                mock.patch.object(
                    proof,
                    "_PREIMPORT_RUNNER_AUTHORITY",
                    tuple(drifted),
                ),
                mock.patch.object(
                    proof,
                    "_preimport_validate_sealed_invocation",
                    return_value=(release_root, runtime_python),
                ),
                mock.patch.object(
                    proof,
                    "_preimport_validate_issuer_authority",
                    return_value=lineage,
                ),
                mock.patch.object(proof, "_require_active_runner_lease"),
                self.assertRaisesRegex(
                    proof.LiveProofError,
                    "^phase9_live_proof_runner_authority_invalid$",
                ),
            ):
                REAL_RUNNER_AUTHORITY_CHECK(
                    proof.RunnerMode.START_OR_RECOVER
                )

    def test_start_mode_refuses_lineage_or_lease_recheck_failure(self) -> None:
        release_root = "/opt/governed-memory-controller/releases/" + PACKAGE
        runtime_python = (
            "/opt/governed-memory-controller/runtimes/"
            + RUNTIME
            + "/bin/python"
        )
        lineage = (
            101,
            102,
            7,
            8,
            "/candidate",
            "lease-1",
            "task-1",
            "thread-1",
        )
        state = (
            proof.RunnerMode.START_OR_RECOVER.value,
            COMMIT,
            TREE,
            PACKAGE,
            RUNTIME,
            release_root,
            runtime_python,
            *lineage,
        )
        for failure in ("lineage", "lease"):
            with (
                self.subTest(failure=failure),
                mock.patch.object(proof, "_PREIMPORT_RUNNER_AUTHORITY", state),
                mock.patch.object(
                    proof,
                    "_preimport_validate_sealed_invocation",
                    return_value=(release_root, runtime_python),
                ),
                mock.patch.object(
                    proof,
                    "_preimport_validate_issuer_authority",
                    side_effect=(
                        RuntimeError("fd198")
                        if failure == "lineage"
                        else None
                    ),
                    return_value=lineage,
                ),
                mock.patch.object(
                    proof,
                    "_require_active_runner_lease",
                    side_effect=(
                        proof.LiveProofError(
                            "phase9_live_proof_runner_authority_invalid"
                        )
                        if failure == "lease"
                        else None
                    ),
                ),
                self.assertRaisesRegex(
                    proof.LiveProofError,
                    "^phase9_live_proof_runner_authority_invalid$",
                ),
            ):
                REAL_RUNNER_AUTHORITY_CHECK(
                    proof.RunnerMode.START_OR_RECOVER
                )

    def test_repository_runner_cli_is_not_a_sealed_release_invocation(
        self,
    ) -> None:
        completed = subprocess.run(
            (
                sys.executable,
                "-I",
                "-B",
                str(Path(proof.__file__)),
                "start-or-recover",
                "--candidate-git-commit",
                COMMIT,
                "--candidate-git-tree",
                TREE,
                "--package-manifest-sha256",
                PACKAGE,
                "--controller-runtime-receipt-sha256",
                RUNTIME,
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(
            completed.stderr,
            b"phase9_live_proof_sealed_authority_required\n",
        )

    def test_issuer_cli_accepts_no_operator_selectable_proof_identity(self) -> None:
        arguments = (
            "--candidate-git-commit",
            COMMIT,
            "--candidate-git-tree",
            TREE,
            "--package-manifest-sha256",
            PACKAGE,
            "--controller-runtime-receipt-sha256",
            RUNTIME,
        )
        with (
            mock.patch.object(issuer, "_require_active_manager_authority") as guard,
            mock.patch.object(issuer, "_proof_inputs_from_verified_permit") as permit,
            mock.patch.object(issuer, "issue_and_supervise") as issue,
            self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "^phase9_proof_issuer_arguments_refused$",
            ),
        ):
            issuer.main(arguments)
        guard.assert_not_called()
        permit.assert_not_called()
        issue.assert_not_called()

    def test_issuer_derives_all_proof_inputs_from_verified_permit(self) -> None:
        candidate = mock.Mock(
            candidate_git_commit=COMMIT,
            candidate_git_tree=TREE,
            package_manifest_sha256=PACKAGE,
            controller_runtime_receipt_sha256=RUNTIME,
        )
        with mock.patch.object(
            issuer.phase9_permitted_candidate,
            "require_exact_permitted_candidate",
            return_value=candidate,
        ) as require:
            observed = issuer._proof_inputs_from_verified_permit()
        self.assertEqual(observed, inputs())
        require.assert_called_once_with(
            expected_thread_id=proof.THREAD_ID,
            expected_authorization_text_sha256=proof.AUTHORIZED_TEXT_SHA256,
        )

    def test_source_uses_closed_public_compositions_and_no_private_key(self) -> None:
        source = Path(proof.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        private_imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                private_imports.extend(
                    alias.name for alias in node.names if alias.name.startswith("_")
                )
        self.assertEqual(private_imports, [])
        self.assertNotIn("Ed25519PrivateKey", source)
        self.assertNotIn("dependencies_factory=", source)
        self.assertNotIn("resource_identity_ledger_factory=", source)
        self.assertIn("run_authorized_dormant_store_install", source)
        self.assertIn("resume_authorized_dormant_store_install", source)
        self.assertIn(
            "start_reserved_authorized_empty_store_rollback",
            source,
        )
        self.assertIn("resume_authorized_empty_store_rollback", source)
        self.assertIn(
            "verified_dormant_install_authority_identity",
            source,
        )
        for copied_identity_domain in (
            "governed-memory-execution-binding-v1",
            "governed-memory-authority-claim-v1",
            "governed-memory-dormant_store_install-execution-id-v1",
        ):
            with self.subTest(copied_identity_domain=copied_identity_domain):
                self.assertNotIn(copied_identity_domain, source)

    def test_worker_setup_is_inside_the_global_execution_lock(self) -> None:
        source = Path(proof.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        functions = {
            node.name: ast.get_source_segment(source, node)
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        install_wrapper = functions["_run_install_worker"]
        rollback_wrapper = functions["_run_rollback_worker"]
        install = functions["_execute_install_locked"]
        rollback = functions["_execute_rollback_locked"]
        self.assertGreater(
            install_wrapper.index("_execute_install_locked("),
            install_wrapper.index("with GlobalExecutionLock("),
        )
        self.assertGreater(
            rollback_wrapper.index("_execute_rollback_locked("),
            rollback_wrapper.index("with GlobalExecutionLock("),
        )
        for call in (
            "_claim_or_verify_recovery_reservation(",
            "DurableReceiptStore.production()",
            "_install_prerequisites(context)",
        ):
            self.assertIn(call, install)
        for call in (
            "_verified_rollback_capability(context)",
            "_claim_or_verify_recovery_reservation(",
            "DurableReceiptStore.production()",
        ):
            self.assertIn(call, rollback)

    def test_recovery_selection_and_actions_share_one_global_lock(self) -> None:
        source = Path(proof.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        functions = {
            node.name: ast.get_source_segment(source, node)
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        recovery = functions["_recovery_selection_worker"]
        lock = recovery.index("with GlobalExecutionLock(")
        self.assertGreater(recovery.index("_execute_install_locked("), lock)
        self.assertGreater(recovery.index("_execute_rollback_locked("), lock)
        self.assertNotIn("_fork_worker(", recovery)

    def test_recover_only_reports_pair_without_prior_install_effect(self) -> None:
        selected_context = context()
        execution_id = INSTALL_EXECUTION
        state = mock.Mock()
        state.inspect_nonce_claim.side_effect = (
            mock.Mock(present=True),
            mock.Mock(present=False),
        )
        lock = mock.MagicMock()
        lock.__enter__.return_value = lock
        lock.held_capability.return_value = object()
        receipts = mock.Mock()
        receipts.read_if_present.return_value = None
        pair = {
            "recovery_reservation_claim_sha256": "a" * 64,
            "install_authority_claim_sha256": "b" * 64,
            "start_authority_pair_claimed_atomically": True,
        }
        with (
            mock.patch.object(proof, "GlobalExecutionLock", return_value=lock),
            mock.patch.object(
                proof, "_verify_host_clock_synchronized", return_value=True
            ),
            mock.patch.object(proof, "_authority_state", return_value=state),
            mock.patch.object(
                proof,
                "recovery_reservation_binding",
                return_value=mock.Mock(nonce="reservation"),
            ),
            mock.patch.object(
                proof, "_exact_install_claim_present_locked", return_value=True
            ),
            mock.patch.object(
                proof, "_expected_install_execution_id", return_value=execution_id
            ),
            mock.patch.object(
                proof.DurableReceiptStore, "production", return_value=receipts
            ),
            mock.patch.object(
                proof, "_claim_or_verify_start_authority_pair", return_value=pair
            ),
            mock.patch.object(proof, "_execution_directories", return_value=frozenset()),
            mock.patch.object(proof, "_execute_install_locked") as execute_install,
        ):
            selection = proof._recovery_selection_worker(
                selected_context,
                allow_start=False,
            )
        self.assertEqual(selection["action"], "pair_only")
        self.assertFalse(selection["install_execution_directory_present"])
        self.assertFalse(selection["installation_receipt_present"])
        self.assertFalse(selection["rollback_authority_claim_present"])
        execute_install.assert_not_called()

    def test_recover_only_resumes_exact_prior_install_execution(self) -> None:
        selected_context = context()
        execution_id = INSTALL_EXECUTION
        state = mock.Mock()
        state.inspect_nonce_claim.side_effect = (
            mock.Mock(present=True),
            mock.Mock(present=False),
        )
        lock = mock.MagicMock()
        lock.__enter__.return_value = lock
        lock.held_capability.return_value = object()
        receipts = mock.Mock()
        receipts.read_if_present.return_value = None
        pair = {
            "recovery_reservation_claim_sha256": "a" * 64,
            "install_authority_claim_sha256": "b" * 64,
            "start_authority_pair_claimed_atomically": True,
        }
        with (
            mock.patch.object(proof, "GlobalExecutionLock", return_value=lock),
            mock.patch.object(
                proof, "_verify_host_clock_synchronized", return_value=True
            ),
            mock.patch.object(proof, "_authority_state", return_value=state),
            mock.patch.object(
                proof,
                "recovery_reservation_binding",
                return_value=mock.Mock(nonce="reservation"),
            ),
            mock.patch.object(
                proof, "_exact_install_claim_present_locked", return_value=True
            ),
            mock.patch.object(
                proof, "_expected_install_execution_id", return_value=execution_id
            ),
            mock.patch.object(
                proof.DurableReceiptStore, "production", return_value=receipts
            ),
            mock.patch.object(
                proof, "_claim_or_verify_start_authority_pair", return_value=pair
            ),
            mock.patch.object(
                proof, "_execution_directories", return_value=frozenset({execution_id})
            ),
            mock.patch.object(
                proof, "_install_journal_compensation_complete", return_value=False
            ),
            mock.patch.object(
                proof,
                "_execute_install_locked",
                side_effect=proof.LiveProofError("resume_attempted"),
            ) as execute_install,
            self.assertRaisesRegex(proof.LiveProofError, "resume_attempted"),
        ):
            proof._recovery_selection_worker(
                selected_context,
                allow_start=False,
            )
        self.assertTrue(execute_install.call_args.kwargs["resume_only"])

    def test_exact_hashed_pretty_json_allowed_but_unsafe_json_refused(self) -> None:
        self.assertEqual(
            proof._parse_exact_hashed_json_object(b'{\n  "a": 1\n}\n', "bad"),
            {"a": 1},
        )
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b"[]"):
            with self.assertRaisesRegex(proof.LiveProofError, "bad"):
                proof._parse_exact_hashed_json_object(raw, "bad")

    def test_host_clock_preflight_is_fixed_bounded_and_exact(self) -> None:
        metadata = mock.Mock(
            st_dev=1,
            st_ino=2,
            st_mode=0o100755,
            st_uid=0,
            st_gid=0,
            st_nlink=1,
            st_size=100,
            st_mtime_ns=3,
            st_ctime_ns=4,
        )
        binary = mock.Mock()
        binary.stat.side_effect = (metadata, metadata)
        completed = mock.Mock(returncode=0, stdout=b"yes\n", stderr=b"")
        with (
            mock.patch.object(proof, "TIMEDATECTL_BINARY", binary),
            mock.patch.object(proof.subprocess, "run", return_value=completed) as run,
        ):
            self.assertTrue(proof._verify_host_clock_synchronized())
        run.assert_called_once_with(
            (
                "/usr/bin/timedatectl",
                "show",
                "--property=NTPSynchronized",
                "--value",
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd="/",
            env=dict(proof.FIXED_ENVIRONMENT),
            check=False,
            timeout=5,
        )

    def test_invalid_authority_cannot_prepare_substrate(self) -> None:
        events: list[str] = []

        def clock() -> bool:
            events.append("clock")
            return True

        def invalid_authority(
            unused: object, *, require_current: bool
        ) -> object:
            self.assertFalse(require_current)
            events.append("authority")
            raise proof.LiveProofError("test-invalid-authority")

        with (
            mock.patch.object(proof, "_verify_host_clock_synchronized", side_effect=clock),
            mock.patch.object(proof, "_verified_install_context", side_effect=invalid_authority),
            mock.patch.object(proof, "_prepare_fixed_substrate") as prepare,
            self.assertRaisesRegex(proof.LiveProofError, "test-invalid-authority"),
        ):
            proof.run_live_proof(inputs())
        self.assertEqual(events, ["clock", "authority"])
        prepare.assert_not_called()

    def test_substrate_validation_refuses_absence_without_creating(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing"
            with self.assertRaisesRegex(proof.LiveProofError, "substrate_invalid"):
                proof._verify_preexisting_root_directory(missing, 0o700)
            self.assertFalse(missing.exists())

    def test_receipt_shape_is_exact_and_snapshot_safe(self) -> None:
        hashes = {
            key: "a" * 64
            for key in proof._LIVE_PROOF_RECEIPT_KEYS
            if key.endswith("sha256") or key.endswith("execution_id")
        }
        receipt: dict[str, object] = {
            **hashes,
            "schema_version": proof.LIVE_PROOF_RECEIPT_SCHEMA,
            "result": "disposable_install_crash_resume_and_empty_rollback_proved",
            "observation_scope": "one_bounded_disposable_linux_execution_terminal_snapshot",
            "candidate_git_commit": COMMIT,
            "candidate_git_tree": TREE,
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
            "recovery_capsule_published_before_first_install_effect": True,
            "recovery_reservation_claimed_before_first_install_effect": True,
            "start_authority_pair_claimed_atomically": True,
            "ephemeral_private_signer_retained_at_execution_start": False,
            "recovery_capsule_retained_at_terminal_observation": True,
        }
        receipt["receipt_sha256"] = proof._document_sha(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        )
        self.assertEqual(set(proof.verify_live_proof_receipt(receipt)), proof._LIVE_PROOF_RECEIPT_KEYS)
        changed = dict(receipt, host_reboot_proven=True)
        changed["receipt_sha256"] = proof._document_sha(
            {key: value for key, value in changed.items() if key != "receipt_sha256"}
        )
        with self.assertRaisesRegex(proof.LiveProofError, "receipt_invalid"):
            proof.verify_live_proof_receipt(changed)
        changed = dict(receipt, exact_rollback_resources_absent_count=15.0)
        changed["receipt_sha256"] = proof._document_sha(
            {key: value for key, value in changed.items() if key != "receipt_sha256"}
        )
        with self.assertRaisesRegex(proof.LiveProofError, "receipt_invalid"):
            proof.verify_live_proof_receipt(changed)
        for counter in (
            "source_postgres_read_count",
            "source_postgres_write_count",
            "provider_calls",
        ):
            with self.subTest(counter=counter):
                changed = dict(receipt, **{counter: False})
                changed["receipt_sha256"] = proof._document_sha(
                    {
                        key: value
                        for key, value in changed.items()
                        if key != "receipt_sha256"
                    }
                )
                with self.assertRaisesRegex(
                    proof.LiveProofError, "receipt_invalid"
                ):
                    proof.verify_live_proof_receipt(changed)

    def test_recovery_receipt_refuses_boolean_provider_counter(self) -> None:
        receipt: dict[str, object] = {
            "schema_version": proof.RECOVERY_RECEIPT_SCHEMA,
            "result": "exact_reserved_install_recovered_and_rolled_back_empty",
            "candidate_git_commit": COMMIT,
            "candidate_git_tree": TREE,
            "package_manifest_sha256": "a" * 64,
            "controller_runtime_receipt_sha256": "b" * 64,
            "recovery_capsule_sha256": "c" * 64,
            "recovery_reservation_claim_sha256": "d" * 64,
            "install_authority_claim_sha256": "e" * 64,
            "start_authority_pair_claimed_atomically": True,
            "installation_execution_id": INSTALL_EXECUTION,
            "installation_receipt_sha256": "f" * 64,
            "empty_rollback_execution_id": ROLLBACK_EXECUTION,
            "empty_rollback_receipt_sha256": "0" * 64,
            "exact_resources_absent": True,
            "stores_installed": False,
            "stores_supervisor_installed": False,
            "install_initiated_by_recovery_mode": False,
            "provider_calls": 0,
            "production_data_read": False,
            "activation_performed": False,
        }
        receipt["receipt_sha256"] = proof._document_sha(receipt)
        self.assertEqual(
            set(proof.verify_recovery_receipt(receipt)),
            proof._RECOVERY_RECEIPT_KEYS,
        )
        forged = dict(receipt, provider_calls=False)
        forged["receipt_sha256"] = proof._document_sha(
            {
                key: value
                for key, value in forged.items()
                if key != "receipt_sha256"
            }
        )
        with self.assertRaisesRegex(
            proof.LiveProofError, "recovery_receipt_invalid"
        ):
            proof.verify_recovery_receipt(forged)

    def test_pair_only_receipt_is_exact_and_non_promotable(self) -> None:
        selected_context = context()
        selection = {
            "action": "pair_only",
            "recovery_reservation_claim_sha256": "a" * 64,
            "install_authority_claim_sha256": "b" * 64,
            "start_authority_pair_claimed_atomically": True,
            "expected_installation_execution_id": INSTALL_EXECUTION,
            "install_execution_directory_present": False,
            "installation_receipt_present": False,
            "rollback_authority_claim_present": False,
        }
        receipt = dict(
            proof._pair_only_receipt(
                context=selected_context,
                selection=selection,
            )
        )
        self.assertEqual(
            set(proof.verify_pair_only_receipt(receipt)),
            proof._PAIR_ONLY_RECEIPT_KEYS,
        )
        self.assertEqual(
            receipt["result"],
            "exact_start_pair_present_install_execution_absent",
        )
        forged = dict(receipt, provider_calls=False)
        forged["receipt_sha256"] = proof._document_sha(
            {
                key: value
                for key, value in forged.items()
                if key != "receipt_sha256"
            }
        )
        with self.assertRaisesRegex(
            proof.LiveProofError, "pair_only_receipt_invalid"
        ):
            proof.verify_pair_only_receipt(forged)

    def test_recovery_capsule_is_deterministic_and_private_key_stays_external(self) -> None:
        private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
        fixed = datetime(2026, 8, 13, 17, 0, tzinfo=timezone.utc)
        first = issuer.build_exact_recovery_capsule(
            inputs=inputs(), artifacts=artifacts(), private_key=private, issued_at=fixed
        )
        second = issuer.build_exact_recovery_capsule(
            inputs=inputs(), artifacts=artifacts(), private_key=private, issued_at=fixed
        )
        self.assertEqual(first, second)
        self.assertEqual(set(first), proof._RECOVERY_CAPSULE_KEYS)
        rendered = issuer._canonical(dict(first))
        parsed = json.loads(rendered)
        self.assertNotIn("private_key", parsed)
        self.assertNotIn("secret_key", parsed)
        self.assertNotIn("seed", parsed)
        self.assertEqual(
            set(parsed["rollback_delegation"]),
            {"schema_version", "payload", "signature"},
        )
        source = Path(proof.__file__).read_text(encoding="utf-8")
        self.assertNotIn(
            "issue_disposable_installation_live_proof_permit",
            source,
        )
        self.assertNotIn("phase9_disposable_proof_authority", source)

    def test_publish_uses_fixed_exclusive_nofollow_0400_contract(self) -> None:
        source = Path(issuer.__file__).read_text(encoding="utf-8")
        self.assertIn("os.O_EXCL", source)
        self.assertIn('getattr(os, "O_NOFOLLOW", 0)', source)
        self.assertIn("0o400", source)
        self.assertIn("RECOVERY_CAPSULE_PATH.name", source)
        self.assertNotIn("remove_exact_recovery_capsule", source)
        self.assertIn("recovery-capsule", str(proof.RECOVERY_CAPSULE_PATH))

    def test_reconcile_adopts_verified_final_without_generating_authority(self) -> None:
        verified = proof.VerifiedRecoveryCapsule(
            raw=b"capsule",
            document={},
            install_documents=proof.InstallDocuments(
                b"{}", b"{}", b"{}", "7" * 64
            ),
            rollback_delegation={},
            key_id="7" * 64,
            rollback_nonce="8" * 64,
        )
        metadata = mock.Mock(st_dev=7, st_ino=8, st_nlink=1)
        parent_metadata = mock.Mock(
            st_dev=3,
            st_ino=4,
            st_mode=stat.S_IFDIR | 0o700,
            st_uid=0,
            st_gid=0,
        )
        with (
            mock.patch.object(issuer, "_ensure_recovery_capsule_parent", return_value=77),
            mock.patch.object(
                issuer,
                "_read_optional_capsule_member",
                side_effect=((b"capsule", metadata), None),
            ),
            mock.patch.object(
                issuer,
                "_read_capsule_member",
                return_value=(b"capsule", metadata),
            ),
            mock.patch.object(
                issuer.runner,
                "_verify_recovery_capsule_raw",
                return_value=verified,
            ) as verify,
            mock.patch.object(os, "fsync"),
            mock.patch.object(os, "close"),
        ):
            result = issuer.reconcile_capsule_publication(
                inputs=inputs(),
                artifacts=artifacts(),
            )
        self.assertEqual(
            result,
            issuer.CapsulePublicationObservation(
                state="published",
                capsule=verified,
                capsule_sha256=hashlib.sha256(b"capsule").hexdigest(),
                inode=(7, 8),
            ),
        )
        verify.assert_called_once()

    def test_reconcile_refuses_old_final_and_temp_on_different_inodes(self) -> None:
        final = mock.Mock(st_dev=7, st_ino=8, st_nlink=2)
        temporary = mock.Mock(st_dev=7, st_ino=9, st_nlink=2)
        verified = mock.Mock(raw=b"capsule")
        with (
            mock.patch.object(issuer, "_ensure_recovery_capsule_parent", return_value=77),
            mock.patch.object(
                issuer,
                "_read_optional_capsule_member",
                side_effect=((b"capsule", final), (b"capsule", temporary)),
            ),
            mock.patch.object(
                issuer.runner,
                "_verify_recovery_capsule_raw",
                return_value=verified,
            ),
            mock.patch.object(os, "close"),
        ):
            with self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "capsule_publication_drift",
            ):
                issuer.reconcile_capsule_publication(
                    inputs=inputs(),
                    artifacts=artifacts(),
                )

    def test_inherited_guard_remains_locked_after_supervisor_close(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "locks"
            parent.mkdir(mode=0o700)
            os.chmod(parent, 0o700)
            path = parent / "phase9.lock"
            supervisor = GlobalExecutionLock(path)
            supervisor.retain_across_inherited_processes()
            child = GlobalExecutionLock.from_inherited_descriptor(
                path,
                supervisor.descriptor,
            )
            supervisor.close()
            with self.assertRaises(ExecutionLockBusyError):
                GlobalExecutionLock(path)
            child.close()
            reopened = GlobalExecutionLock(path)
            reopened.close()

    def test_inherited_guard_refuses_unlocked_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "locks"
            parent.mkdir(mode=0o700)
            os.chmod(parent, 0o700)
            path = parent / "phase9.lock"
            owner = GlobalExecutionLock(path)
            descriptor = os.dup(owner.descriptor)
            owner.close()
            try:
                with self.assertRaisesRegex(
                    ExecutionLockSecurityError,
                    "inherited_descriptor_not_locked",
                ):
                    GlobalExecutionLock.from_inherited_descriptor(
                        path,
                        descriptor,
                    )
            finally:
                os.close(descriptor)

    def test_issuer_runtime_gate_requires_isolation_and_no_bytecode(self) -> None:
        for isolated, no_bytecode in ((False, True), (True, False)):
            with (
                self.subTest(isolated=isolated, no_bytecode=no_bytecode),
                mock.patch.object(
                    issuer, "_ISOLATED_RUNTIME_AT_START", isolated
                ),
                mock.patch.object(
                    issuer, "_DONT_WRITE_BYTECODE_AT_START", no_bytecode
                ),
                self.assertRaisesRegex(
                    issuer.Phase9ProofIssuerError,
                    "runtime_isolation_required",
                ),
            ):
                issuer._require_closed_issuer_runtime()

    def test_preimport_manager_pid_requires_exact_direct_parent_lineage(self) -> None:
        cases = (
            ({}, 321),
            ({issuer.MANAGER_PID_ENVIRONMENT_KEY: "321"}, 322),
            ({issuer.MANAGER_PID_ENVIRONMENT_KEY: "0321"}, 321),
        )
        for environment, parent_pid in cases:
            with (
                self.subTest(environment=environment, parent_pid=parent_pid),
                mock.patch.dict(issuer.os.environ, environment, clear=True),
                mock.patch.object(issuer.os, "getppid", return_value=parent_pid),
                self.assertRaisesRegex(
                    RuntimeError,
                    "^phase9_proof_issuer_manager_control_required$",
                ),
            ):
                issuer._preimport_manager_pid()

    def test_preimport_manager_control_installs_pdeath_and_rechecks_pipe(self) -> None:
        opened = mock.Mock(
            st_mode=stat.S_IFIFO | 0o600,
            st_dev=7,
            st_ino=8,
        )
        prctl = mock.Mock(return_value=0)
        library = mock.Mock(prctl=prctl)
        with (
            mock.patch.dict(
                issuer.os.environ,
                {issuer.MANAGER_PID_ENVIRONMENT_KEY: "321"},
                clear=True,
            ),
            mock.patch.object(issuer.os, "getppid", return_value=321),
            mock.patch.object(issuer.os, "fstat", return_value=opened),
            mock.patch.object(issuer.fcntl, "fcntl", return_value=os.O_RDONLY),
            mock.patch.object(issuer.os, "set_blocking") as set_blocking,
            mock.patch.object(
                issuer,
                "_preimport_validate_manager_lineage",
            ) as validate_lineage,
            mock.patch.object(
                issuer.select,
                "select",
                side_effect=(([], [], []), ([], [], [])),
            ),
            mock.patch.object(issuer.signal, "signal") as install_signal,
            mock.patch.object(issuer.ctypes, "CDLL", return_value=library),
            mock.patch.object(issuer, "_MANAGER_CONTROL_LOST", False),
            mock.patch.object(issuer, "_DIRECT_MANAGER_PIPE_ID", None),
        ):
            self.assertEqual(issuer._preimport_require_manager_control(), 321)
            self.assertEqual(issuer._DIRECT_MANAGER_PIPE_ID, (7, 8))
        set_blocking.assert_called_once_with(issuer.MANAGER_CONTROL_FD, False)
        validate_lineage.assert_called_once_with(321, (7, 8))
        install_signal.assert_called_once_with(
            signal.SIGTERM,
            issuer._mark_manager_control_lost,
        )
        prctl.assert_called_once_with(
            issuer.PR_SET_PDEATHSIG,
            int(signal.SIGCONT),
            0,
            0,
            0,
        )

    def test_manager_control_health_refuses_pipe_eof(self) -> None:
        opened = mock.Mock(
            st_mode=stat.S_IFIFO | 0o600,
            st_dev=7,
            st_ino=8,
        )
        with (
            mock.patch.object(issuer, "_preimport_manager_pid", return_value=321),
            mock.patch.object(issuer, "_DIRECT_MANAGER_PID", 321),
            mock.patch.object(issuer, "_DIRECT_MANAGER_PIPE_ID", (7, 8)),
            mock.patch.object(issuer, "_MANAGER_CONTROL_LOST", False),
            mock.patch.object(issuer.os, "fstat", return_value=opened),
            mock.patch.object(issuer.fcntl, "fcntl", return_value=os.O_RDONLY),
            mock.patch.object(
                issuer.select,
                "select",
                return_value=([issuer.MANAGER_CONTROL_FD], [], []),
            ),
            self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "^phase9_proof_issuer_manager_control_lost$",
            ),
        ):
            issuer._require_manager_control_health()

    def test_parent_death_signal_marks_manager_control_lost(self) -> None:
        opened = mock.Mock(
            st_mode=stat.S_IFIFO | 0o600,
            st_dev=7,
            st_ino=8,
        )
        with (
            mock.patch.object(issuer, "_preimport_manager_pid", return_value=321),
            mock.patch.object(issuer, "_DIRECT_MANAGER_PID", 321),
            mock.patch.object(issuer, "_DIRECT_MANAGER_PIPE_ID", (7, 8)),
            mock.patch.object(issuer, "_MANAGER_CONTROL_LOST", False),
            mock.patch.object(issuer.os, "fstat", return_value=opened),
            mock.patch.object(issuer.fcntl, "fcntl", return_value=os.O_RDONLY),
            mock.patch.object(
                issuer.select,
                "select",
                return_value=([], [], []),
            ),
            self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "^phase9_proof_issuer_manager_control_lost$",
            ),
        ):
            issuer._mark_manager_control_lost(signal.SIGTERM, None)
            issuer._require_manager_control_health()

    def test_issuer_lease_guard_uses_fixed_uid_and_system_python(self) -> None:
        command_runner = mock.Mock()
        command_runner.run.return_value = subprocess.CompletedProcess(
            args=("guard",),
            returncode=0,
            stdout=b"",
            stderr=b"",
        )
        environment = {
            "CHAT_MEMORY_LEASE_ID": "lease-1",
            "CODEX_TASK_ID": "task-1",
            "CODEX_THREAD_ID": "thread-1",
        }
        issuer._require_production_write_lease(
            environment,
            command_runner=command_runner,
        )
        call = command_runner.run.call_args
        self.assertEqual(call.args[0][0], issuer.LEASE_GUARD_PYTHON)
        self.assertEqual(call.args[0][1], issuer.LEASE_GUARD)
        self.assertIn("production-write", call.args[0])
        self.assertIn(str(issuer._ISSUER_REPOSITORY_ROOT), call.args[0])
        self.assertEqual(call.kwargs["user"], 1000)
        self.assertEqual(call.kwargs["group"], 1000)
        self.assertEqual(call.kwargs["extra_groups"], ())

    def test_direct_issuer_refuses_each_incomplete_runtime_fence(self) -> None:
        issuer_path = str(Path(issuer.__file__))
        for flags in (("-B",), ("-I",)):
            with self.subTest(flags=flags):
                completed = subprocess.run(
                    (sys.executable, *flags, issuer_path, "--help"),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(completed.returncode, 1)
                self.assertEqual(completed.stdout, b"")
                self.assertEqual(
                    completed.stderr,
                    b"phase9_proof_issuer_runtime_isolation_required\n",
                )

    def test_runner_descriptor_seal_closes_every_unintended_descriptor(
        self,
    ) -> None:
        closed: list[int] = []

        def close_descriptor(descriptor: int) -> None:
            closed.append(descriptor)
            if descriptor == 77:
                raise OSError(errno.EBADF, "already closed")

        with (
            mock.patch.object(
                os,
                "listdir",
                return_value=[
                    "0",
                    "1",
                    "2",
                    "3",
                    "8",
                    "9",
                    "10",
                    "77",
                    "198",
                ],
            ) as listdir,
            mock.patch.object(os, "close", side_effect=close_descriptor),
        ):
            issuer._close_unintended_runner_descriptors(
                retain_manager_control=True,
            )
        listdir.assert_called_once_with("/proc/self/fd")
        self.assertEqual(closed, [3, 8, 10, 77])

    def test_runner_process_seal_installs_only_inert_stdin_outputs_and_guard(
        self,
    ) -> None:
        with (
            mock.patch.object(os, "open", return_value=13) as open_descriptor,
            mock.patch.object(os, "dup2") as duplicate,
            mock.patch.object(
                os,
                "fstat",
                return_value=mock.Mock(st_mode=stat.S_IFIFO | 0o600),
            ),
            mock.patch.object(
                issuer.fcntl,
                "fcntl",
                return_value=os.O_RDONLY,
            ),
            mock.patch.object(os, "set_inheritable") as set_inheritable,
            mock.patch.object(
                issuer, "_close_unintended_runner_descriptors"
            ) as close_unintended,
        ):
            issuer._seal_exact_runner_process(
                guard_descriptor=20,
                stdout_descriptor=21,
                stderr_descriptor=22,
                mode=proof.RunnerMode.START_OR_RECOVER,
            )
        open_descriptor.assert_called_once_with(
            "/dev/null",
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
        self.assertEqual(
            duplicate.call_args_list,
            [
                mock.call(13, 0),
                mock.call(21, 1),
                mock.call(22, 2),
                mock.call(20, proof.LIVE_PROOF_GUARD_FD),
            ],
        )
        self.assertEqual(
            set_inheritable.call_args_list,
            [
                mock.call(0, True),
                mock.call(1, True),
                mock.call(2, True),
                mock.call(proof.LIVE_PROOF_GUARD_FD, True),
                mock.call(issuer.MANAGER_CONTROL_FD, True),
            ],
        )
        close_unintended.assert_called_once_with(
            retain_manager_control=True,
        )

    def test_recovery_runner_closes_manager_control_and_authority_environment(
        self,
    ) -> None:
        with (
            mock.patch.object(os, "open", return_value=13),
            mock.patch.object(os, "dup2"),
            mock.patch.object(os, "set_inheritable") as set_inheritable,
            mock.patch.object(
                issuer, "_close_unintended_runner_descriptors"
            ) as close_unintended,
        ):
            issuer._seal_exact_runner_process(
                guard_descriptor=20,
                stdout_descriptor=21,
                stderr_descriptor=22,
                mode=proof.RunnerMode.RECOVER_ONLY,
            )
        self.assertNotIn(
            mock.call(issuer.MANAGER_CONTROL_FD, True),
            set_inheritable.call_args_list,
        )
        close_unintended.assert_called_once_with(
            retain_manager_control=False,
        )
        self.assertEqual(
            issuer._runner_environment(
                proof.RunnerMode.RECOVER_ONLY,
                issuer_pid=123,
            ),
            dict(issuer._SAFE_ENVIRONMENT),
        )

    def test_missing_recovery_capsule_parent_is_refused_without_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "missing"
            capsule_path = parent / "recovery_capsule.json"
            with (
                mock.patch.object(issuer.runner, "RECOVERY_CAPSULE_PATH", capsule_path),
                mock.patch.object(os, "geteuid", return_value=0),
            ):
                with self.assertRaisesRegex(
                    issuer.Phase9ProofIssuerError,
                    "recovery_capsule_parent_invalid",
                ):
                    issuer._ensure_recovery_capsule_parent()
            self.assertFalse(parent.exists())

    def test_wrong_mode_recovery_capsule_parent_is_refused_without_normalization(
        self,
    ) -> None:
        metadata = mock.Mock(
            st_dev=7,
            st_ino=8,
            st_mode=0o040755,
            st_uid=0,
            st_gid=0,
        )
        with (
            mock.patch.object(os, "geteuid", return_value=0),
            mock.patch.object(os, "open", return_value=77),
            mock.patch.object(os, "fstat", return_value=metadata),
            mock.patch.object(Path, "stat", return_value=metadata),
            mock.patch.object(os, "close") as close,
            mock.patch.object(os, "chmod") as chmod,
            mock.patch.object(os, "chown") as chown,
        ):
            with self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "recovery_capsule_parent_invalid",
            ):
                issuer._ensure_recovery_capsule_parent()
        close.assert_called_once_with(77)
        chmod.assert_not_called()
        chown.assert_not_called()

    def test_partial_recovery_capsule_publication_removes_only_created_inode(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            capsule_path = parent / "recovery_capsule.json"

            def parent_descriptor() -> int:
                return os.open(parent, os.O_RDONLY | os.O_DIRECTORY)

            with (
                mock.patch.object(issuer.runner, "RECOVERY_CAPSULE_PATH", capsule_path),
                mock.patch.object(issuer, "_require_active_manager_authority"),
                mock.patch.object(
                    issuer,
                    "_ensure_recovery_capsule_parent",
                    side_effect=parent_descriptor,
                ),
                mock.patch.object(os, "fchown"),
                mock.patch.object(os, "write", side_effect=OSError("failed")),
            ):
                with self.assertRaisesRegex(
                    issuer.Phase9ProofIssuerError,
                    "recovery_capsule_write_failed",
                ):
                    issuer.prepare_fixed_recovery_capsule({"recovery_capsule": "fixed"})
            self.assertFalse(capsule_path.exists())

            temporary_path = capsule_path.with_name(
                capsule_path.name + issuer.RECOVERY_CAPSULE_TEMP_SUFFIX
            )
            temporary_path.write_bytes(b"preexisting")
            before = temporary_path.stat()
            with (
                mock.patch.object(issuer.runner, "RECOVERY_CAPSULE_PATH", capsule_path),
                mock.patch.object(issuer, "_require_active_manager_authority"),
                mock.patch.object(
                    issuer,
                    "_ensure_recovery_capsule_parent",
                    side_effect=parent_descriptor,
                ),
            ):
                with self.assertRaisesRegex(
                    issuer.Phase9ProofIssuerError,
                    "existing_recovery_capsule_temp_refused",
                ):
                    issuer.prepare_fixed_recovery_capsule({"recovery_capsule": "fixed"})
            after = temporary_path.stat()
            self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
            self.assertEqual(temporary_path.read_bytes(), b"preexisting")

    def test_recovery_capsule_publication_fstat_failure_unlinks_created_name(
        self,
    ) -> None:
        metadata = mock.Mock(st_dev=7, st_ino=8)
        with (
            mock.patch.object(issuer, "_require_active_manager_authority"),
            mock.patch.object(issuer, "_ensure_recovery_capsule_parent", return_value=77),
            mock.patch.object(os, "open", return_value=88),
            mock.patch.object(os, "fstat", side_effect=OSError("fstat failed")),
            mock.patch.object(os, "stat", return_value=metadata) as named_stat,
            mock.patch.object(os, "unlink") as unlink,
            mock.patch.object(os, "fsync"),
            mock.patch.object(os, "close"),
        ):
            with self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "recovery_capsule_write_failed",
            ):
                issuer.prepare_fixed_recovery_capsule({"recovery_capsule": "fixed"})
        named_stat.assert_called_with(
            proof.RECOVERY_CAPSULE_PATH.name
            + issuer.RECOVERY_CAPSULE_TEMP_SUFFIX,
            dir_fd=77,
            follow_symlinks=False,
        )
        unlink.assert_called_once_with(
            proof.RECOVERY_CAPSULE_PATH.name
            + issuer.RECOVERY_CAPSULE_TEMP_SUFFIX,
            dir_fd=77,
        )

    def test_recovery_capsule_publication_close_failure_unlinks_created_inode(
        self,
    ) -> None:
        raw = issuer._canonical({"recovery_capsule": "fixed"})
        metadata = mock.Mock(
            st_dev=7,
            st_ino=8,
            st_mode=0o100400,
            st_uid=0,
            st_gid=0,
            st_nlink=1,
            st_size=len(raw),
        )
        close_failed = False

        def close_once(descriptor: int) -> None:
            nonlocal close_failed
            if descriptor == 88 and not close_failed:
                close_failed = True
                raise OSError("close failed")

        with (
            mock.patch.object(issuer, "_require_active_manager_authority"),
            mock.patch.object(issuer, "_ensure_recovery_capsule_parent", return_value=77),
            mock.patch.object(os, "open", return_value=88),
            mock.patch.object(os, "fstat", return_value=metadata),
            mock.patch.object(os, "stat", return_value=metadata),
            mock.patch.object(os, "fchown"),
            mock.patch.object(os, "fchmod"),
            mock.patch.object(os, "write", side_effect=lambda fd, value: len(value)),
            mock.patch.object(os, "fsync"),
            mock.patch.object(os, "unlink") as unlink,
            mock.patch.object(os, "close", side_effect=close_once),
        ):
            with self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "recovery_capsule_write_failed",
            ):
                issuer.prepare_fixed_recovery_capsule({"recovery_capsule": "fixed"})
        unlink.assert_called_once_with(
            proof.RECOVERY_CAPSULE_PATH.name
            + issuer.RECOVERY_CAPSULE_TEMP_SUFFIX,
            dir_fd=77,
        )

    def test_manager_loss_refuses_capsule_mutations_before_syscall(self) -> None:
        lost = issuer.Phase9ProofIssuerError(
            "phase9_proof_issuer_manager_control_lost"
        )
        metadata = mock.Mock(st_dev=7, st_ino=8, st_nlink=1)
        capsule_sha256 = issuer._sha(b"capsule")

        with (
            mock.patch.object(
                issuer, "_ensure_recovery_capsule_parent", return_value=77
            ),
            mock.patch.object(
                issuer, "_require_active_manager_authority", side_effect=lost
            ),
            mock.patch.object(os, "open") as create,
            mock.patch.object(os, "close"),
            self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "^phase9_proof_issuer_manager_control_lost$",
            ),
        ):
            issuer.prepare_fixed_recovery_capsule(
                {"recovery_capsule": "fixed"}
            )
        create.assert_not_called()

        with (
            mock.patch.object(
                issuer, "_ensure_recovery_capsule_parent", return_value=77
            ),
            mock.patch.object(
                issuer,
                "_read_capsule_member",
                return_value=(b"capsule", metadata),
            ),
            mock.patch.object(
                issuer, "_read_optional_capsule_member", return_value=None
            ),
            mock.patch.object(
                issuer, "_require_active_manager_authority", side_effect=lost
            ),
            mock.patch.object(issuer, "_publish_temp_link") as publish,
            mock.patch.object(os, "close"),
            self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "^phase9_proof_issuer_manager_control_lost$",
            ),
        ):
            issuer.commit_preclaimed_recovery_capsule(
                expected_capsule_sha256=capsule_sha256,
                expected_inode=(7, 8),
            )
        publish.assert_not_called()

        linked = mock.Mock(st_dev=7, st_ino=8, st_nlink=2)
        with (
            mock.patch.object(
                issuer, "_ensure_recovery_capsule_parent", return_value=77
            ),
            mock.patch.object(
                issuer,
                "_read_capsule_member",
                side_effect=((b"capsule", linked), (b"capsule", linked)),
            ),
            mock.patch.object(
                issuer, "_require_active_manager_authority", side_effect=lost
            ),
            mock.patch.object(issuer, "_unlink_exact_member") as unlink,
            mock.patch.object(os, "close"),
            self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "^phase9_proof_issuer_manager_control_lost$",
            ),
        ):
            issuer.complete_linked_capsule_publication(
                expected_capsule_sha256=capsule_sha256,
                expected_inode=(7, 8),
            )
        unlink.assert_not_called()

    def test_recovery_capsule_stability_ignores_access_time_but_not_content_metadata(
        self,
    ) -> None:
        fields = {
            "st_dev": 1,
            "st_ino": 2,
            "st_mode": 0o100400,
            "st_uid": 0,
            "st_gid": 0,
            "st_nlink": 1,
            "st_size": 100,
            "st_mtime_ns": 3,
            "st_ctime_ns": 4,
        }
        before = mock.Mock(**fields, st_atime_ns=5)
        after = mock.Mock(**fields, st_atime_ns=6)
        self.assertEqual(
            proof._stable_file_identity(before),
            proof._stable_file_identity(after),
        )
        after.st_size = 101
        self.assertNotEqual(
            proof._stable_file_identity(before),
            proof._stable_file_identity(after),
        )

    def test_issuer_supervision_is_bounded_and_kills_exact_process_group(self) -> None:
        source = Path(issuer.__file__).read_text(encoding="utf-8")
        self.assertIn("os.setsid()", source)
        self.assertIn("os.killpg(pid, signal.SIGKILL)", source)
        self.assertLessEqual(
            issuer.RUNNER_TIMEOUT_SECONDS,
            20 * 60,
        )
        self.assertLessEqual(issuer.RUNNER_REAP_TIMEOUT_SECONDS, 5)
        self.assertIn("deadline - time.monotonic()", source)
        self.assertNotIn("MAX_RUNNER_ATTEMPTS", source)
        self.assertNotIn("TOTAL_SUPERVISION_TIMEOUT_SECONDS", source)
        self.assertNotIn("for attempt in range", source)
        self.assertNotIn("def _read_bounded", source)

    def test_runner_parent_death_fence_precedes_setsid_and_exec(self) -> None:
        source = Path(issuer.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(
            ast.get_source_segment(source, node)
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_spawn_exact_runner"
        )
        parent_fence = function.index(
            "_arm_runner_parent_death(expected_parent_pid)"
        )
        setsid = function.index("os.setsid()")
        execute = function.index("os.execve(")
        self.assertLess(parent_fence, setsid)
        self.assertLess(setsid, execute)

    def test_linux_runner_parent_death_fence_uses_sigkill_and_parent_recheck(
        self,
    ) -> None:
        prctl = mock.Mock(return_value=0)
        library = mock.Mock(prctl=prctl)
        with (
            mock.patch.object(issuer.sys, "platform", "linux"),
            mock.patch.object(issuer.ctypes, "CDLL", return_value=library),
            mock.patch.object(issuer.os, "getppid", return_value=321),
        ):
            issuer._arm_runner_parent_death(321)
        prctl.assert_called_once_with(
            issuer.PR_SET_PDEATHSIG,
            int(signal.SIGKILL),
            0,
            0,
            0,
        )

        with (
            mock.patch.object(issuer.sys, "platform", "linux"),
            mock.patch.object(issuer.ctypes, "CDLL", return_value=library),
            mock.patch.object(issuer.os, "getppid", return_value=999),
            mock.patch.object(issuer.os, "getpid", return_value=654),
            mock.patch.object(issuer.os, "kill") as kill,
            mock.patch.object(
                issuer.os,
                "_exit",
                side_effect=SystemExit(1),
            ) as exit_process,
            self.assertRaises(SystemExit),
        ):
            issuer._arm_runner_parent_death(321)
        kill.assert_called_once_with(654, signal.SIGKILL)
        exit_process.assert_called_once_with(1)

    def test_each_normal_worker_has_a_finite_completion_budget(self) -> None:
        self.assertGreater(proof.WORKER_COMPLETION_TIMEOUT_SECONDS, 0)
        self.assertLess(
            proof.WORKER_COMPLETION_TIMEOUT_SECONDS,
            15 * 60,
        )
        pid = 321
        descriptor = 654
        with (
            mock.patch.object(
                proof.time,
                "monotonic",
                side_effect=(100.0, 341.0),
            ),
            mock.patch.object(
                proof,
                "_terminate_and_reap_worker",
            ) as terminate,
            self.assertRaisesRegex(
                proof.LiveProofError, "phase9_live_proof_worker_timeout"
            ),
        ):
            proof._wait_worker(pid, descriptor)
        terminate.assert_called_once_with(pid, descriptor)

    def test_bounded_worker_wait_drains_result_and_reaps_owned_child(self) -> None:
        guard = mock.MagicMock()
        guard.__enter__.return_value = guard
        with (
            mock.patch.object(proof, "_arm_worker_parent_death"),
            mock.patch.object(
                proof,
                "_adopt_inherited_live_proof_guard",
                return_value=guard,
            ),
            mock.patch.object(
                proof,
                "_dispatch_worker",
                return_value={"worker": "complete"},
            ),
        ):
            pid, descriptor = proof._fork_worker(
                proof.WorkerMode.INSTALL, context()
            )
            result = proof._wait_worker(pid, descriptor)
        self.assertEqual(dict(result), {"worker": "complete"})

    def test_worker_parent_death_and_guard_fences_precede_dispatch(self) -> None:
        source = Path(proof.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(
            ast.get_source_segment(source, node)
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_fork_worker"
        )
        dispatch = "_dispatch_worker_with_cooperative_boundary_stop("
        self.assertLess(
            function.index("_arm_worker_parent_death(expected_parent_pid)"),
            function.index(dispatch),
        )
        self.assertLess(
            function.index("with _adopt_inherited_live_proof_guard():"),
            function.index(dispatch),
        )

    def test_linux_worker_parent_death_fence_uses_sigkill_and_parent_recheck(
        self,
    ) -> None:
        prctl = mock.Mock(return_value=0)
        library = mock.Mock(prctl=prctl)
        with (
            mock.patch.object(proof.sys, "platform", "linux"),
            mock.patch.object(proof.ctypes, "CDLL", return_value=library),
            mock.patch.object(proof.os, "getppid", return_value=321),
        ):
            proof._arm_worker_parent_death(321)
        prctl.assert_called_once_with(
            proof.PR_SET_PDEATHSIG,
            int(signal.SIGKILL),
            0,
            0,
            0,
        )

    @unittest.skipUnless(
        sys.platform == "linux" and hasattr(os, "fork"),
        "Linux prctl parent-death semantics required",
    )
    def test_real_stopped_worker_is_sigkilled_when_supervisor_dies(self) -> None:
        pr_set_child_subreaper = 36
        pr_get_child_subreaper = 37
        library = proof.ctypes.CDLL(None, use_errno=True)
        prctl = library.prctl
        prctl.argtypes = (
            proof.ctypes.c_int,
            proof.ctypes.c_ulong,
            proof.ctypes.c_ulong,
            proof.ctypes.c_ulong,
            proof.ctypes.c_ulong,
        )
        prctl.restype = proof.ctypes.c_int
        prior = proof.ctypes.c_int()
        self.assertEqual(
            prctl(
                pr_get_child_subreaper,
                proof.ctypes.addressof(prior),
                0,
                0,
                0,
            ),
            0,
        )
        self.assertEqual(prctl(pr_set_child_subreaper, 1, 0, 0, 0), 0)

        read_fd, write_fd = os.pipe()
        supervisor_pid = os.fork()
        if supervisor_pid == 0:
            os.close(read_fd)
            worker_pid = os.fork()
            if worker_pid == 0:
                try:
                    proof._arm_worker_parent_death(os.getppid())
                    os.write(write_fd, f"W{os.getpid()}\n".encode("ascii"))
                    os.kill(os.getpid(), signal.SIGSTOP)
                finally:
                    os.close(write_fd)
                os._exit(91)
            try:
                waited, stopped_status = os.waitpid(worker_pid, os.WUNTRACED)
                if (
                    waited != worker_pid
                    or not os.WIFSTOPPED(stopped_status)
                    or os.WSTOPSIG(stopped_status) != signal.SIGSTOP
                ):
                    os._exit(92)
                os.write(write_fd, b"S\n")
                os.close(write_fd)
                os._exit(0)
            except BaseException:
                try:
                    os.kill(worker_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os._exit(93)

        os.close(write_fd)
        worker_pid: int | None = None
        supervisor_owned = True
        worker_owned = True
        try:
            raw = b""
            while True:
                chunk = os.read(read_fd, 4096)
                if not chunk:
                    break
                raw += chunk
            lines = raw.decode("ascii").splitlines()
            worker_lines = tuple(line for line in lines if line.startswith("W"))
            self.assertEqual(lines[-1:], ["S"])
            self.assertEqual(len(worker_lines), 1)
            worker_pid = int(worker_lines[0][1:])

            waited, supervisor_status = os.waitpid(supervisor_pid, 0)
            supervisor_owned = False
            self.assertEqual(waited, supervisor_pid)
            self.assertTrue(os.WIFEXITED(supervisor_status))
            self.assertEqual(os.WEXITSTATUS(supervisor_status), 0)

            waited, worker_status = os.waitpid(worker_pid, 0)
            worker_owned = False
            self.assertEqual(waited, worker_pid)
            self.assertTrue(os.WIFSIGNALED(worker_status))
            self.assertEqual(os.WTERMSIG(worker_status), signal.SIGKILL)
        finally:
            if supervisor_owned:
                try:
                    os.kill(supervisor_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(supervisor_pid, 0)
                except ChildProcessError:
                    pass
            if worker_owned and worker_pid is not None:
                try:
                    os.kill(worker_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(worker_pid, 0)
                except ChildProcessError:
                    pass
            os.close(read_fd)
            self.assertEqual(
                prctl(pr_set_child_subreaper, int(prior.value), 0, 0, 0),
                0,
            )

    def test_issuer_refuses_before_effects_without_durable_recovery_authority(self) -> None:
        with (
            mock.patch.object(issuer, "_require_closed_issuer_runtime"),
            mock.patch.object(issuer, "_require_active_manager_authority"),
            mock.patch.object(os, "geteuid", return_value=0),
            mock.patch.object(issuer, "verify_exact_clean_candidate"),
            mock.patch.object(
                issuer.runner,
                "_load_release",
                return_value=(b"manifest", {}),
            ),
            mock.patch.object(
                issuer.runner,
                "DURABLE_PRE_EFFECT_ROLLBACK_AUTHORITY_PACKAGED",
                False,
            ),
            mock.patch.object(
                issuer.Ed25519PrivateKey, "generate"
            ) as generate_key,
            mock.patch.object(issuer, "prepare_fixed_recovery_capsule") as publish,
            mock.patch.object(issuer, "_spawn_exact_runner") as spawn,
            self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "durable_recovery_authority_required",
            ),
        ):
            issuer.issue_and_supervise(inputs())
        generate_key.assert_not_called()
        publish.assert_not_called()
        spawn.assert_not_called()

    def test_issuer_refuses_missing_disposition_before_any_v6_mutation(self) -> None:
        with (
            mock.patch.object(issuer, "_require_closed_issuer_runtime"),
            mock.patch.object(issuer, "_require_active_manager_authority"),
            mock.patch.object(os, "geteuid", return_value=0),
            mock.patch.object(issuer, "verify_exact_clean_candidate"),
            mock.patch.object(
                issuer.runner,
                "_load_release",
                return_value=(b"manifest", {}),
            ),
            mock.patch.object(
                issuer.runner,
                "DURABLE_PRE_EFFECT_ROLLBACK_AUTHORITY_PACKAGED",
                True,
            ),
            mock.patch.object(
                issuer,
                "_require_production_pre_effect_disposition",
                side_effect=(
                    issuer.Phase9ProofIssuerError(
                        "phase9_proof_issuer_pre_effect_disposition_required"
                    )
                ),
            ) as require_disposition,
            mock.patch.object(
                issuer, "_ensure_live_proof_guard_parent"
            ) as create_guard_parent,
            mock.patch.object(issuer, "GlobalExecutionLock"),
            mock.patch.object(
                issuer.Ed25519PrivateKey, "generate"
            ) as generate_key,
            mock.patch.object(
                issuer, "prepare_fixed_recovery_capsule"
            ) as publish,
            mock.patch.object(issuer, "_spawn_exact_runner") as spawn,
            self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "phase9_proof_issuer_pre_effect_disposition_required",
            ),
        ):
            issuer.issue_and_supervise(inputs())
        require_disposition.assert_called_once()
        create_guard_parent.assert_called_once_with()
        generate_key.assert_not_called()
        publish.assert_not_called()
        spawn.assert_not_called()

    def test_issuer_cannot_bypass_invalid_disposition_return(self) -> None:
        with (
            mock.patch.object(issuer, "_require_closed_issuer_runtime"),
            mock.patch.object(issuer, "_require_active_manager_authority"),
            mock.patch.object(os, "geteuid", return_value=0),
            mock.patch.object(issuer, "verify_exact_clean_candidate"),
            mock.patch.object(
                issuer.runner,
                "_load_release",
                return_value=(b"manifest", {}),
            ),
            mock.patch.object(
                issuer.runner,
                "DURABLE_PRE_EFFECT_ROLLBACK_AUTHORITY_PACKAGED",
                True,
            ),
            mock.patch.multiple(
                issuer.pre_effect_disposition,
                PRODUCTION_CONTRACT_SHA256=DISPOSITION_CONTRACT,
                PRODUCTION_PREDECESSOR_ATTEMPT_IDENTITY_SHA256=(
                    PREDECESSOR_ATTEMPT
                ),
            ),
            mock.patch.object(
                issuer.pre_effect_disposition,
                "require_production_disposition_receipt",
                return_value=None,
            ),
            mock.patch.object(
                issuer, "_ensure_live_proof_guard_parent"
            ) as create_guard_parent,
            mock.patch.object(issuer, "GlobalExecutionLock"),
            mock.patch.object(
                issuer.Ed25519PrivateKey, "generate"
            ) as generate_key,
            mock.patch.object(issuer, "_spawn_exact_runner") as spawn,
            self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "phase9_proof_issuer_pre_effect_disposition_required",
            ),
        ):
            issuer.issue_and_supervise(inputs())
        create_guard_parent.assert_called_once_with()
        generate_key.assert_not_called()
        spawn.assert_not_called()

    def test_issuer_requires_exact_disposition_inside_held_guard(self) -> None:
        events: list[str] = []
        guard = mock.Mock()
        guard.__enter__ = mock.Mock(
            side_effect=lambda: events.append("guard-enter") or guard
        )
        guard.__exit__ = mock.Mock(return_value=None)
        guard.descriptor = 9
        with (
            mock.patch.object(issuer, "_require_closed_issuer_runtime"),
            mock.patch.object(issuer, "_require_active_manager_authority"),
            mock.patch.object(os, "geteuid", return_value=0),
            mock.patch.object(issuer, "verify_exact_clean_candidate"),
            mock.patch.object(
                issuer.runner,
                "_load_release",
                return_value=(b"manifest", {}),
            ),
            mock.patch.object(
                issuer.runner,
                "DURABLE_PRE_EFFECT_ROLLBACK_AUTHORITY_PACKAGED",
                True,
            ),
            mock.patch.object(issuer, "_ensure_live_proof_guard_parent"),
            mock.patch.object(
                issuer,
                "GlobalExecutionLock",
                return_value=guard,
            ) as construct_guard,
            mock.patch.object(
                issuer,
                "_require_production_pre_effect_disposition",
                side_effect=lambda **unused: events.append("disposition") or disposition_receipt(),
            ) as disposition_check,
            mock.patch.object(
                issuer.substrate_bootstrap,
                "bootstrap_phase9_disposable_store_substrate",
                side_effect=lambda **unused: events.append("bootstrap"),
            ) as bootstrap,
            mock.patch.object(
                issuer.runner,
                "_prepare_fixed_substrate",
                side_effect=lambda: events.append("substrate"),
            ) as substrate_check,
            mock.patch.object(
                issuer,
                "reconcile_capsule_publication",
                side_effect=lambda **unused: (_ for _ in ()).throw(
                    issuer.Phase9ProofIssuerError("after-disposition")
                ),
            ) as reconcile,
            self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError, "after-disposition"
            ),
        ):
            issuer.issue_and_supervise(inputs())
        disposition_check.assert_called_once_with(inputs=inputs())
        bootstrap.assert_called_once_with(
            held_lock=guard.held_capability.return_value
        )
        substrate_check.assert_called_once_with()
        reconcile.assert_called_once()
        self.assertEqual(
            events,
            ["guard-enter", "disposition", "bootstrap", "substrate"],
        )
        construct_guard.assert_called_once_with(
            proof.LIVE_PROOF_GUARD_PATH,
            expected_uid=0,
            expected_gid=0,
        )

    def test_issuer_refuses_invalid_substrate_before_capsule_or_worker(self) -> None:
        guard = mock.Mock()
        guard.__enter__ = mock.Mock(return_value=guard)
        guard.__exit__ = mock.Mock(return_value=None)
        guard.descriptor = 9
        with (
            mock.patch.object(issuer, "_require_closed_issuer_runtime"),
            mock.patch.object(issuer, "_require_active_manager_authority"),
            mock.patch.object(os, "geteuid", return_value=0),
            mock.patch.object(issuer, "verify_exact_clean_candidate"),
            mock.patch.object(
                issuer.runner,
                "_load_release",
                return_value=(b"manifest", {}),
            ),
            mock.patch.object(
                issuer.runner,
                "DURABLE_PRE_EFFECT_ROLLBACK_AUTHORITY_PACKAGED",
                True,
            ),
            mock.patch.object(issuer, "_ensure_live_proof_guard_parent"),
            mock.patch.object(
                issuer,
                "GlobalExecutionLock",
                return_value=guard,
            ),
            mock.patch.object(
                issuer,
                "_require_production_pre_effect_disposition",
                return_value=disposition_receipt(),
            ),
            mock.patch.object(
                issuer.substrate_bootstrap,
                "bootstrap_phase9_disposable_store_substrate",
            ) as bootstrap,
            mock.patch.object(
                issuer.runner,
                "_prepare_fixed_substrate",
                side_effect=proof.LiveProofError(
                    "phase9_live_proof_substrate_invalid"
                ),
            ) as substrate_check,
            mock.patch.object(
                issuer, "reconcile_capsule_publication"
            ) as reconcile,
            mock.patch.object(
                issuer.Ed25519PrivateKey, "generate"
            ) as generate_key,
            mock.patch.object(
                issuer, "build_exact_recovery_capsule"
            ) as build_capsule,
            mock.patch.object(
                issuer, "prepare_fixed_recovery_capsule"
            ) as publish_capsule,
            mock.patch.object(issuer, "_supervise_attempt") as supervise,
            self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "phase9_proof_issuer_substrate_invalid",
            ),
        ):
            issuer.issue_and_supervise(inputs())
        bootstrap.assert_called_once_with(
            held_lock=guard.held_capability.return_value
        )
        substrate_check.assert_called_once_with()
        reconcile.assert_not_called()
        generate_key.assert_not_called()
        build_capsule.assert_not_called()
        publish_capsule.assert_not_called()
        supervise.assert_not_called()
        guard.__exit__.assert_called_once()

    def test_issuer_refuses_bootstrap_failure_before_substrate_or_capsule(self) -> None:
        guard = mock.Mock()
        guard.__enter__ = mock.Mock(return_value=guard)
        guard.__exit__ = mock.Mock(return_value=None)
        guard.descriptor = 9
        with (
            mock.patch.object(issuer, "_require_closed_issuer_runtime"),
            mock.patch.object(issuer, "_require_active_manager_authority"),
            mock.patch.object(os, "geteuid", return_value=0),
            mock.patch.object(issuer, "verify_exact_clean_candidate"),
            mock.patch.object(
                issuer.runner,
                "_load_release",
                return_value=(b"manifest", {}),
            ),
            mock.patch.object(
                issuer.runner,
                "DURABLE_PRE_EFFECT_ROLLBACK_AUTHORITY_PACKAGED",
                True,
            ),
            mock.patch.object(issuer, "_ensure_live_proof_guard_parent"),
            mock.patch.object(issuer, "GlobalExecutionLock", return_value=guard),
            mock.patch.object(
                issuer,
                "_require_production_pre_effect_disposition",
                return_value=disposition_receipt(),
            ),
            mock.patch.object(
                issuer.substrate_bootstrap,
                "bootstrap_phase9_disposable_store_substrate",
                side_effect=(
                    issuer.substrate_bootstrap.Phase9DisposableStoreSubstrateError(
                        "private detail"
                    )
                ),
            ) as bootstrap,
            mock.patch.object(
                issuer.runner, "_prepare_fixed_substrate"
            ) as substrate_check,
            mock.patch.object(
                issuer, "reconcile_capsule_publication"
            ) as reconcile,
            self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "^phase9_proof_issuer_substrate_bootstrap_failed$",
            ),
        ):
            issuer.issue_and_supervise(inputs())
        bootstrap.assert_called_once_with(
            held_lock=guard.held_capability.return_value
        )
        substrate_check.assert_not_called()
        reconcile.assert_not_called()
        guard.__exit__.assert_called_once()

    def test_manager_loss_at_prebootstrap_fence_prevents_all_live_mutators(
        self,
    ) -> None:
        guard = mock.Mock()
        guard.__enter__ = mock.Mock(return_value=guard)
        guard.__exit__ = mock.Mock(return_value=None)
        guard.descriptor = 9
        lost = issuer.Phase9ProofIssuerError(
            "phase9_proof_issuer_manager_control_lost"
        )
        with (
            mock.patch.object(issuer, "_require_closed_issuer_runtime"),
            mock.patch.object(
                issuer,
                "_require_active_manager_authority",
                side_effect=(None, None, None, None, lost),
            ) as authority,
            mock.patch.object(os, "geteuid", return_value=0),
            mock.patch.object(issuer, "verify_exact_clean_candidate"),
            mock.patch.object(
                issuer.runner,
                "_load_release",
                return_value=(b"manifest", {}),
            ),
            mock.patch.object(
                issuer.runner,
                "DURABLE_PRE_EFFECT_ROLLBACK_AUTHORITY_PACKAGED",
                True,
            ),
            mock.patch.object(issuer, "_ensure_live_proof_guard_parent"),
            mock.patch.object(issuer, "GlobalExecutionLock", return_value=guard),
            mock.patch.object(
                issuer,
                "_require_production_pre_effect_disposition",
                return_value=disposition_receipt(),
            ),
            mock.patch.object(
                issuer.substrate_bootstrap,
                "bootstrap_phase9_disposable_store_substrate",
            ) as bootstrap,
            mock.patch.object(
                issuer.runner, "_prepare_fixed_substrate"
            ) as substrate,
            mock.patch.object(
                issuer, "reconcile_capsule_publication"
            ) as reconcile,
            self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "^phase9_proof_issuer_manager_control_lost$",
            ),
        ):
            issuer.issue_and_supervise(inputs())
        self.assertEqual(authority.call_count, 5)
        bootstrap.assert_not_called()
        substrate.assert_not_called()
        reconcile.assert_not_called()
        guard.__exit__.assert_called_once()

    def test_unexpected_supervision_error_kills_and_reaps_exact_group(self) -> None:
        descriptors = [os.open(os.devnull, os.O_RDONLY) for unused in range(2)]
        pid = 424242
        with (
            mock.patch.object(issuer, "_require_active_manager_authority"),
            mock.patch.object(issuer, "_require_manager_control_health"),
            mock.patch.object(
                issuer,
                "_spawn_exact_runner",
                return_value=(pid, *descriptors),
            ),
            mock.patch.object(
                os,
                "waitpid",
                side_effect=OSError("unexpected"),
            ) as waitpid,
            mock.patch.object(
                issuer,
                "_terminate_and_reap_exact_runner",
            ) as terminate,
        ):
            with self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "supervision_failed",
            ):
                issuer._supervise_attempt(
                    inputs=inputs(),
                    guard_descriptor=9,
                )
        terminate.assert_called_once_with(pid)
        waitpid.assert_called_once_with(pid, os.WNOHANG)

    def test_effectful_supervision_rechecks_lease_while_runner_is_alive(self) -> None:
        descriptors = [os.open(os.devnull, os.O_RDONLY) for unused in range(2)]
        pid = 424242
        with (
            mock.patch.object(issuer, "_require_active_manager_authority") as initial,
            mock.patch.object(issuer, "_require_manager_control_health") as lineage,
            mock.patch.object(issuer, "_require_production_write_lease") as lease,
            mock.patch.object(issuer, "MANAGER_AUTHORITY_RECHECK_SECONDS", 0.0),
            mock.patch.object(
                issuer,
                "_spawn_exact_runner",
                return_value=(pid, *descriptors),
            ),
            mock.patch.object(os, "waitpid", return_value=(pid, 0)),
        ):
            observed = issuer._supervise_attempt(
                inputs=inputs(),
                guard_descriptor=9,
            )
        self.assertEqual(observed, (0, b"", b""))
        self.assertEqual(initial.call_count, 2)
        self.assertEqual(initial.call_args_list, [mock.call(), mock.call()])
        lineage.assert_called_once_with()
        lease.assert_called_once_with()

    def test_nonzero_reaped_runner_uses_only_nondestructive_group_fence(
        self,
    ) -> None:
        descriptors = [os.open(os.devnull, os.O_RDONLY) for unused in range(2)]
        pid = 424242
        with (
            mock.patch.object(issuer, "_require_active_manager_authority"),
            mock.patch.object(issuer, "_require_manager_control_health"),
            mock.patch.object(
                issuer,
                "_spawn_exact_runner",
                return_value=(pid, *descriptors),
            ),
            mock.patch.object(os, "waitpid", return_value=(pid, 1 << 8)),
            mock.patch.object(
                issuer,
                "_require_process_group_absent_after_reap",
            ) as require_absent,
            mock.patch.object(
                issuer,
                "_terminate_and_reap_exact_runner",
            ) as destructive_cleanup,
        ):
            observed = issuer._supervise_attempt(
                inputs=inputs(),
                guard_descriptor=9,
            )
        self.assertEqual(observed, (1, b"", b""))
        require_absent.assert_called_once()
        self.assertEqual(require_absent.call_args.args, (pid,))
        destructive_cleanup.assert_not_called()

    def test_signaled_reaped_runner_can_recover_only_after_group_absence(
        self,
    ) -> None:
        descriptors = [os.open(os.devnull, os.O_RDONLY) for unused in range(2)]
        pid = 424242
        with (
            mock.patch.object(issuer, "_require_active_manager_authority"),
            mock.patch.object(issuer, "_require_manager_control_health"),
            mock.patch.object(
                issuer,
                "_spawn_exact_runner",
                return_value=(pid, *descriptors),
            ),
            mock.patch.object(os, "waitpid", return_value=(pid, signal.SIGKILL)),
            mock.patch.object(
                issuer,
                "_require_process_group_absent_after_reap",
            ) as require_absent,
        ):
            observed = issuer._supervise_attempt(
                inputs=inputs(),
                guard_descriptor=9,
            )
        self.assertEqual(observed, (128 + signal.SIGKILL, b"", b""))
        require_absent.assert_called_once()

    def test_post_reap_group_ambiguity_is_terminal_before_recovery(self) -> None:
        descriptors = [os.open(os.devnull, os.O_RDONLY) for unused in range(2)]
        pid = 424242
        with (
            mock.patch.object(issuer, "_require_active_manager_authority"),
            mock.patch.object(issuer, "_require_manager_control_health"),
            mock.patch.object(
                issuer,
                "_spawn_exact_runner",
                return_value=(pid, *descriptors),
            ),
            mock.patch.object(os, "waitpid", return_value=(pid, 1 << 8)),
            mock.patch.object(
                issuer,
                "_require_process_group_absent_after_reap",
                side_effect=issuer.Phase9ProofIssuerError(
                    "phase9_proof_issuer_runner_cleanup_failed"
                ),
            ),
            mock.patch.object(
                issuer,
                "_terminate_and_reap_exact_runner",
            ) as destructive_cleanup,
            self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "^phase9_proof_issuer_runner_cleanup_failed$",
            ),
        ):
            issuer._supervise_attempt(
                inputs=inputs(),
                guard_descriptor=9,
            )
        destructive_cleanup.assert_not_called()

    def test_post_reap_pipe_error_never_destructively_signals_reused_pgid(
        self,
    ) -> None:
        descriptors = [os.open(os.devnull, os.O_RDONLY) for unused in range(2)]
        pid = 424242
        with (
            mock.patch.object(issuer, "_require_active_manager_authority"),
            mock.patch.object(issuer, "_require_manager_control_health"),
            mock.patch.object(
                issuer,
                "_spawn_exact_runner",
                return_value=(pid, *descriptors),
            ),
            mock.patch.object(os, "waitpid", return_value=(pid, 1 << 8)),
            mock.patch.object(
                select,
                "select",
                return_value=([descriptors[0]], [], []),
            ),
            mock.patch.object(os, "read", side_effect=OSError("pipe")),
            mock.patch.object(
                issuer,
                "_require_process_group_absent_after_reap",
            ) as require_absent,
            mock.patch.object(
                issuer,
                "_terminate_and_reap_exact_runner",
            ) as destructive_cleanup,
            self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "^phase9_proof_issuer_supervision_failed$",
            ),
        ):
            issuer._supervise_attempt(
                inputs=inputs(),
                guard_descriptor=9,
            )
        require_absent.assert_called_once()
        destructive_cleanup.assert_not_called()

    def test_initial_authority_loss_refuses_before_runner_spawn(self) -> None:
        with (
            mock.patch.object(
                issuer,
                "_require_active_manager_authority",
                side_effect=issuer.Phase9ProofIssuerError(
                    "phase9_proof_issuer_production_write_lease_denied"
                ),
            ),
            mock.patch.object(issuer, "_spawn_exact_runner") as spawn,
            self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "^phase9_proof_issuer_production_write_lease_denied$",
            ),
        ):
            issuer._supervise_attempt(
                inputs=inputs(),
                guard_descriptor=9,
            )
        spawn.assert_not_called()

    def test_recovery_only_supervision_does_not_require_lost_authority(self) -> None:
        descriptors = [os.open(os.devnull, os.O_RDONLY) for unused in range(2)]
        pid = 424242
        with (
            mock.patch.object(issuer, "_require_active_manager_authority") as initial,
            mock.patch.object(issuer, "_require_manager_control_health") as lineage,
            mock.patch.object(issuer, "_require_production_write_lease") as lease,
            mock.patch.object(
                issuer,
                "_spawn_exact_runner",
                return_value=(pid, *descriptors),
            ),
            mock.patch.object(os, "waitpid", return_value=(pid, 0)),
        ):
            observed = issuer._supervise_attempt(
                inputs=inputs(),
                guard_descriptor=9,
                mode=proof.RunnerMode.RECOVER_ONLY,
            )
        self.assertEqual(observed, (0, b"", b""))
        initial.assert_not_called()
        lineage.assert_not_called()
        lease.assert_not_called()

    def test_manager_control_loss_kills_and_reaps_runner_group(self) -> None:
        descriptors = [os.open(os.devnull, os.O_RDONLY) for unused in range(2)]
        pid = 424242
        with (
            mock.patch.object(issuer, "_require_active_manager_authority"),
            mock.patch.object(
                issuer,
                "_require_manager_control_health",
                side_effect=issuer.Phase9ProofIssuerError(
                    "phase9_proof_issuer_manager_control_lost"
                ),
            ),
            mock.patch.object(
                issuer,
                "_spawn_exact_runner",
                return_value=(pid, *descriptors),
            ),
            mock.patch.object(
                issuer,
                "_terminate_and_reap_exact_runner",
            ) as terminate,
            self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "^phase9_proof_issuer_manager_control_lost$",
            ),
        ):
            issuer._supervise_attempt(
                inputs=inputs(),
                guard_descriptor=9,
            )
        terminate.assert_called_once_with(pid)

    def test_spawn_parent_failure_terminates_child_and_closes_all_pipes(self) -> None:
        pipe_pairs = ((10, 11), (12, 13))
        closed: list[int] = []
        failed = False

        def close_once(descriptor: int) -> None:
            nonlocal failed
            closed.append(descriptor)
            if descriptor == 11 and not failed:
                failed = True
                raise OSError("close failed")

        with (
            mock.patch.object(os, "pipe", side_effect=pipe_pairs),
            mock.patch.object(
                issuer,
                "_runner_environment",
                return_value=dict(issuer._SAFE_ENVIRONMENT),
            ),
            mock.patch.object(os, "fork", return_value=424242),
            mock.patch.object(os, "close", side_effect=close_once),
            mock.patch.object(
                issuer,
                "_terminate_and_reap_exact_runner",
            ) as terminate,
        ):
            with self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "runner_spawn_failed",
            ):
                issuer._spawn_exact_runner(inputs(), 9)
        terminate.assert_called_once_with(424242)
        self.assertEqual(set(closed), set(range(10, 14)))

    def test_group_creation_race_falls_back_to_exact_pid_and_bounded_reap(self) -> None:
        pid = 424242
        missing_group = ProcessLookupError()
        with (
            mock.patch.object(
                os,
                "killpg",
                side_effect=(
                    missing_group,
                    missing_group,
                    missing_group,
                    missing_group,
                ),
            ) as kill_group,
            mock.patch.object(os, "kill") as kill_pid,
            mock.patch.object(
                os,
                "waitpid",
                side_effect=((0, 0), (pid, signal.SIGKILL)),
            ) as waitpid,
            mock.patch.object(
                time,
                "monotonic",
                side_effect=(100.0, 100.1),
            ),
            mock.patch.object(select, "select", return_value=([], [], [])),
        ):
            issuer._terminate_and_reap_exact_runner(pid)
        self.assertEqual(kill_group.call_count, 4)
        self.assertEqual(kill_group.call_args_list[-1], mock.call(pid, 0))
        kill_pid.assert_called_once_with(pid, signal.SIGKILL)
        self.assertEqual(waitpid.call_count, 2)

    def test_reaped_runner_requires_bounded_process_group_absence(self) -> None:
        pid = 424242
        missing_group = ProcessLookupError()
        with (
            mock.patch.object(
                os,
                "killpg",
                side_effect=(None, None, missing_group),
            ) as kill_group,
            mock.patch.object(os, "waitpid", return_value=(pid, 0)),
            mock.patch.object(
                time,
                "monotonic",
                side_effect=(100.0, 100.1),
            ),
            mock.patch.object(select, "select", return_value=([], [], [])),
        ):
            issuer._terminate_and_reap_exact_runner(pid)
        self.assertEqual(
            kill_group.call_args_list,
            [
                mock.call(pid, signal.SIGKILL),
                mock.call(pid, 0),
                mock.call(pid, 0),
            ],
        )

    def test_live_runner_group_at_deadline_refuses_cleanup_proof(self) -> None:
        pid = 424242
        with (
            mock.patch.object(os, "killpg", return_value=None),
            mock.patch.object(os, "waitpid", return_value=(pid, 0)),
            mock.patch.object(
                time,
                "monotonic",
                side_effect=(100.0, 106.0),
            ),
            self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "^phase9_proof_issuer_runner_cleanup_failed$",
            ),
        ):
            issuer._terminate_and_reap_exact_runner(pid)

    def test_exact_runner_timeout_invokes_recovery_only_once(self) -> None:
        schema = b"schema"
        recovered = {
            "candidate_git_commit": COMMIT,
            "candidate_git_tree": TREE,
            "package_manifest_sha256": PACKAGE,
            "controller_runtime_receipt_sha256": RUNTIME,
            "recovery_capsule_sha256": "a" * 64,
        }
        (
            runtime,
            manager_authority,
            guard_parent,
            guard_lock,
            reconcile,
            disposition,
            bootstrap,
            substrate,
            pair,
            durable,
        ) = self._issuer_guard_mocks()
        with (
            runtime,
            manager_authority,
            guard_parent,
            guard_lock,
            reconcile,
            disposition,
            bootstrap,
            substrate,
            pair,
            durable,
            mock.patch.object(os, "geteuid", return_value=0),
            mock.patch.object(
                issuer.runner,
                "DURABLE_PRE_EFFECT_ROLLBACK_AUTHORITY_PACKAGED",
                True,
            ),
            mock.patch.object(issuer, "verify_exact_clean_candidate"),
            mock.patch.object(
                issuer.runner,
                "_load_release",
                return_value=(b"manifest", {proof.LIVE_PROOF_RECEIPT_SCHEMA_RELATIVE: schema}),
            ),
            mock.patch.object(
                issuer,
                "_supervise_attempt",
                side_effect=(
                    issuer.Phase9ProofIssuerError(
                        "phase9_proof_issuer_runner_timeout"
                    ),
                    (0, b"{}\n", b""),
                ),
            ) as attempts,
            mock.patch.object(
                issuer.runner,
                "_parse_canonical_object",
                return_value={
                    "schema_version": proof.RECOVERY_RECEIPT_SCHEMA
                },
            ),
            mock.patch.object(issuer.runner, "verify_recovery_receipt", return_value=recovered),
        ):
            with self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "recovery_completed_live_proof_not_proven",
            ):
                issuer.issue_and_supervise(inputs())
        self.assertEqual(attempts.call_count, 2)
        self.assertIs(attempts.call_args_list[1].kwargs["mode"], proof.RunnerMode.RECOVER_ONLY)

    def test_unexpected_supervision_error_enters_closed_recovery_then_fails(
        self,
    ) -> None:
        (
            runtime,
            manager_authority,
            guard_parent,
            guard_lock,
            reconcile,
            disposition,
            bootstrap,
            substrate,
            pair,
            durable,
        ) = self._issuer_guard_mocks()
        with (
            runtime,
            manager_authority,
            guard_parent,
            guard_lock,
            reconcile,
            disposition,
            bootstrap,
            substrate,
            pair,
            durable,
            mock.patch.object(os, "geteuid", return_value=0),
            mock.patch.object(
                issuer.runner,
                "DURABLE_PRE_EFFECT_ROLLBACK_AUTHORITY_PACKAGED",
                True,
            ),
            mock.patch.object(issuer, "verify_exact_clean_candidate"),
            mock.patch.object(
                issuer.runner, "_load_release", return_value=(b"manifest", {})
            ),
            mock.patch.object(
                issuer,
                "_supervise_attempt",
                side_effect=(
                    issuer.Phase9ProofIssuerError(
                        "phase9_proof_issuer_supervision_failed"
                    ),
                    (0, b"pair\n", b""),
                ),
            ) as attempts,
            mock.patch.object(
                issuer,
                "_verify_supervised_receipt",
                return_value=("pair_only", {}),
            ),
            self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "^phase9_proof_issuer_supervision_failed$",
            ),
        ):
            issuer.issue_and_supervise(inputs())
        self.assertEqual(
            [call.kwargs["mode"] for call in attempts.call_args_list],
            [proof.RunnerMode.START_OR_RECOVER, proof.RunnerMode.RECOVER_ONLY],
        )

    def test_ambiguous_runner_cleanup_never_starts_overlapping_recovery(
        self,
    ) -> None:
        (
            runtime,
            manager_authority,
            guard_parent,
            guard_lock,
            reconcile,
            disposition,
            bootstrap,
            substrate,
            pair,
            durable,
        ) = self._issuer_guard_mocks()
        with (
            runtime,
            manager_authority,
            guard_parent,
            guard_lock,
            reconcile,
            disposition,
            bootstrap,
            substrate,
            pair,
            durable,
            mock.patch.object(os, "geteuid", return_value=0),
            mock.patch.object(
                issuer.runner,
                "DURABLE_PRE_EFFECT_ROLLBACK_AUTHORITY_PACKAGED",
                True,
            ),
            mock.patch.object(issuer, "verify_exact_clean_candidate"),
            mock.patch.object(
                issuer.runner, "_load_release", return_value=(b"manifest", {})
            ),
            mock.patch.object(
                issuer,
                "_supervise_attempt",
                side_effect=issuer.Phase9ProofIssuerError(
                    "phase9_proof_issuer_runner_cleanup_failed"
                ),
            ) as attempts,
            self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "^phase9_proof_issuer_runner_cleanup_failed$",
            ),
        ):
            issuer.issue_and_supervise(inputs())
        self.assertEqual(attempts.call_count, 1)
        self.assertIs(
            attempts.call_args.kwargs["mode"],
            proof.RunnerMode.START_OR_RECOVER,
        )

    def test_invalid_success_receipt_recovers_before_refusal(self) -> None:
        (
            runtime,
            manager_authority,
            guard_parent,
            guard_lock,
            reconcile,
            disposition,
            bootstrap,
            substrate,
            pair,
            durable,
        ) = self._issuer_guard_mocks()
        invalid = issuer.Phase9ProofIssuerError(
            "phase9_proof_issuer_receipt_invalid"
        )
        with (
            runtime,
            manager_authority,
            guard_parent,
            guard_lock,
            reconcile,
            disposition,
            bootstrap,
            substrate,
            pair,
            durable,
            mock.patch.object(os, "geteuid", return_value=0),
            mock.patch.object(
                issuer.runner,
                "DURABLE_PRE_EFFECT_ROLLBACK_AUTHORITY_PACKAGED",
                True,
            ),
            mock.patch.object(issuer, "verify_exact_clean_candidate"),
            mock.patch.object(
                issuer.runner, "_load_release", return_value=(b"manifest", {})
            ),
            mock.patch.object(
                issuer,
                "_supervise_attempt",
                side_effect=((0, b"bad\n", b""), (0, b"pair\n", b"")),
            ) as attempts,
            mock.patch.object(
                issuer,
                "_verify_supervised_receipt",
                side_effect=(invalid, ("pair_only", {})),
            ),
            self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "^phase9_proof_issuer_receipt_invalid$",
            ),
        ):
            issuer.issue_and_supervise(inputs())
        self.assertEqual(
            [call.kwargs["mode"] for call in attempts.call_args_list],
            [proof.RunnerMode.START_OR_RECOVER, proof.RunnerMode.RECOVER_ONLY],
        )

    def test_post_failure_durable_read_error_recovers_before_refusal(self) -> None:
        (
            runtime,
            manager_authority,
            guard_parent,
            guard_lock,
            reconcile,
            disposition,
            bootstrap,
            substrate,
            pair,
            unused_durable,
        ) = self._issuer_guard_mocks()
        del unused_durable
        with (
            runtime,
            manager_authority,
            guard_parent,
            guard_lock,
            reconcile,
            disposition,
            bootstrap,
            substrate,
            pair,
            mock.patch.object(os, "geteuid", return_value=0),
            mock.patch.object(
                issuer.runner,
                "DURABLE_PRE_EFFECT_ROLLBACK_AUTHORITY_PACKAGED",
                True,
            ),
            mock.patch.object(issuer, "verify_exact_clean_candidate"),
            mock.patch.object(
                issuer.runner, "_load_release", return_value=(b"manifest", {})
            ),
            mock.patch.object(
                issuer,
                "_supervise_attempt",
                side_effect=((1, b"", b"failed\n"), (0, b"pair\n", b"")),
            ) as attempts,
            mock.patch.object(
                issuer,
                "_verify_supervised_receipt",
                return_value=("pair_only", {}),
            ),
            mock.patch.object(
                issuer.durable_live_proof_receipt,
                "read_verified_promotable_live_receipt_if_present",
                side_effect=(
                    None,
                    issuer.durable_live_proof_receipt.DurableLiveProofReceiptError(
                        "invalid"
                    ),
                ),
            ),
            self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "^phase9_proof_issuer_durable_receipt_invalid$",
            ),
        ):
            issuer.issue_and_supervise(inputs())
        self.assertEqual(
            [call.kwargs["mode"] for call in attempts.call_args_list],
            [proof.RunnerMode.START_OR_RECOVER, proof.RunnerMode.RECOVER_ONLY],
        )

    def test_manager_loss_continues_only_through_closed_recovery_mode(self) -> None:
        live = {"receipt_sha256": "d" * 64}
        (
            runtime,
            manager_authority,
            guard_parent,
            guard_lock,
            reconcile,
            disposition,
            bootstrap,
            substrate,
            pair,
            durable,
        ) = self._issuer_guard_mocks()
        with (
            runtime,
            manager_authority,
            guard_parent,
            guard_lock,
            reconcile,
            disposition,
            bootstrap,
            substrate,
            pair,
            durable,
            mock.patch.object(os, "geteuid", return_value=0),
            mock.patch.object(
                issuer.runner,
                "DURABLE_PRE_EFFECT_ROLLBACK_AUTHORITY_PACKAGED",
                True,
            ),
            mock.patch.object(issuer, "verify_exact_clean_candidate"),
            mock.patch.object(
                issuer.runner,
                "_load_release",
                return_value=(b"manifest", {}),
            ),
            mock.patch.object(
                issuer,
                "_supervise_attempt",
                side_effect=(
                    issuer.Phase9ProofIssuerError(
                        "phase9_proof_issuer_manager_control_lost"
                    ),
                    (0, b"live\n", b""),
                ),
            ) as attempts,
            mock.patch.object(
                issuer,
                "_verify_supervised_receipt",
                return_value=("live", live),
            ),
        ):
            self.assertEqual(dict(issuer.issue_and_supervise(inputs())), live)
        self.assertEqual(
            [call.kwargs["mode"] for call in attempts.call_args_list],
            [
                proof.RunnerMode.START_OR_RECOVER,
                proof.RunnerMode.RECOVER_ONLY,
            ],
        )

    def test_issuer_rejects_receipt_not_bound_to_exact_inputs(self) -> None:
        schema = b"schema"
        verified = {
            "candidate_git_commit": "9" * 40,
            "candidate_git_tree": TREE,
            "package_manifest_sha256": PACKAGE,
            "controller_runtime_receipt_sha256": RUNTIME,
            "authorization_text_sha256": proof.AUTHORIZED_TEXT_SHA256,
            "live_proof_receipt_schema_sha256": hashlib.sha256(schema).hexdigest(),
            "recovery_capsule_sha256": "a" * 64,
        }
        (
            runtime,
            manager_authority,
            guard_parent,
            guard_lock,
            reconcile,
            disposition,
            bootstrap,
            substrate,
            pair,
            durable,
        ) = self._issuer_guard_mocks()
        with (
            runtime,
            manager_authority,
            guard_parent,
            guard_lock,
            reconcile,
            disposition,
            bootstrap,
            substrate,
            pair,
            durable,
            mock.patch.object(os, "geteuid", return_value=0),
            mock.patch.object(
                issuer.runner,
                "DURABLE_PRE_EFFECT_ROLLBACK_AUTHORITY_PACKAGED",
                True,
            ),
            mock.patch.object(issuer, "verify_exact_clean_candidate"),
            mock.patch.object(
                issuer.runner,
                "_load_release",
                return_value=(
                    b"manifest",
                    {proof.LIVE_PROOF_RECEIPT_SCHEMA_RELATIVE: schema},
                ),
            ),
            mock.patch.object(
                issuer,
                "_supervise_attempt",
                return_value=(0, b"{}\n", b""),
            ),
            mock.patch.object(
                issuer.runner,
                "_parse_canonical_object",
                return_value={
                    "schema_version": proof.LIVE_PROOF_RECEIPT_SCHEMA
                },
            ),
            mock.patch.object(
                issuer.runner,
                "verify_live_proof_receipt",
                return_value=verified,
            ),
        ):
            with self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "receipt_binding_invalid",
            ):
                issuer.issue_and_supervise(inputs())

    def test_runner_nonzero_invokes_recovery_once_and_retains_capsule(self) -> None:
        (
            runtime,
            manager_authority,
            guard_parent,
            guard_lock,
            reconcile,
            disposition,
            bootstrap,
            substrate,
            pair,
            durable,
        ) = self._issuer_guard_mocks()
        with (
            runtime,
            manager_authority,
            guard_parent,
            guard_lock,
            reconcile,
            disposition,
            bootstrap,
            substrate,
            pair,
            durable,
            mock.patch.object(os, "geteuid", return_value=0),
            mock.patch.object(
                issuer.runner,
                "DURABLE_PRE_EFFECT_ROLLBACK_AUTHORITY_PACKAGED",
                True,
            ),
            mock.patch.object(issuer, "verify_exact_clean_candidate"),
            mock.patch.object(
                issuer.runner,
                "_load_release",
                return_value=(b"manifest", {}),
            ),
            mock.patch.object(
                issuer,
                "_supervise_attempt",
                return_value=(
                    1,
                    b"",
                    b"phase9_live_proof_first_failure\n",
                ),
            ) as attempts,
        ):
            with self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "phase9_live_proof_first_failure",
            ):
                issuer.issue_and_supervise(inputs())
        self.assertEqual(attempts.call_count, 2)
        self.assertIs(
            attempts.call_args_list[1].kwargs["mode"],
            proof.RunnerMode.RECOVER_ONLY,
        )

    def test_pair_only_retries_same_start_pair_once(self) -> None:
        (
            runtime,
            manager_authority,
            guard_parent,
            guard_lock,
            reconcile,
            disposition,
            bootstrap,
            substrate,
            pair,
            durable,
        ) = self._issuer_guard_mocks()
        live = {"receipt_sha256": "d" * 64}
        with (
            runtime,
            manager_authority,
            guard_parent,
            guard_lock,
            reconcile,
            disposition,
            bootstrap,
            substrate,
            pair,
            durable,
            mock.patch.object(os, "geteuid", return_value=0),
            mock.patch.object(
                issuer.runner,
                "DURABLE_PRE_EFFECT_ROLLBACK_AUTHORITY_PACKAGED",
                True,
            ),
            mock.patch.object(issuer, "verify_exact_clean_candidate"),
            mock.patch.object(
                issuer.runner,
                "_load_release",
                return_value=(b"manifest", {}),
            ),
            mock.patch.object(
                issuer,
                "_supervise_attempt",
                side_effect=(
                    (1, b"", b"first\n"),
                    (0, b"pair\n", b""),
                    (0, b"live\n", b""),
                ),
            ) as attempts,
            mock.patch.object(
                issuer,
                "_verify_supervised_receipt",
                side_effect=(("pair_only", {}), ("live", live)),
            ),
        ):
            self.assertEqual(dict(issuer.issue_and_supervise(inputs())), live)
        self.assertEqual(
            [call.kwargs["mode"] for call in attempts.call_args_list],
            [
                proof.RunnerMode.START_OR_RECOVER,
                proof.RunnerMode.RECOVER_ONLY,
                proof.RunnerMode.START_OR_RECOVER,
            ],
        )

    def test_second_pair_only_stops_without_looping(self) -> None:
        (
            runtime,
            manager_authority,
            guard_parent,
            guard_lock,
            reconcile,
            disposition,
            bootstrap,
            substrate,
            pair,
            durable,
        ) = self._issuer_guard_mocks()
        with (
            runtime,
            manager_authority,
            guard_parent,
            guard_lock,
            reconcile,
            disposition,
            bootstrap,
            substrate,
            pair,
            durable,
            mock.patch.object(os, "geteuid", return_value=0),
            mock.patch.object(
                issuer.runner,
                "DURABLE_PRE_EFFECT_ROLLBACK_AUTHORITY_PACKAGED",
                True,
            ),
            mock.patch.object(issuer, "verify_exact_clean_candidate"),
            mock.patch.object(
                issuer.runner,
                "_load_release",
                return_value=(b"manifest", {}),
            ),
            mock.patch.object(
                issuer,
                "_supervise_attempt",
                side_effect=(
                    (1, b"", b"first\n"),
                    (0, b"pair\n", b""),
                    (1, b"", b"retry\n"),
                    (0, b"pair\n", b""),
                ),
            ) as attempts,
            mock.patch.object(
                issuer,
                "_verify_supervised_receipt",
                side_effect=(("pair_only", {}), ("pair_only", {})),
            ),
            self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "start_not_observed_pair_only_pristine",
            ),
        ):
            issuer.issue_and_supervise(inputs())
        self.assertEqual(
            [call.kwargs["mode"] for call in attempts.call_args_list],
            [
                proof.RunnerMode.START_OR_RECOVER,
                proof.RunnerMode.RECOVER_ONLY,
                proof.RunnerMode.START_OR_RECOVER,
                proof.RunnerMode.RECOVER_ONLY,
            ],
        )

    def test_runner_output_framing_is_exact(self) -> None:
        self.assertEqual(
            issuer._strict_runner_error(
                1, b"", b"phase9_live_proof_refused\n"
            ),
            "phase9_live_proof_refused",
        )
        self.assertEqual(
            issuer._strict_runner_receipt_payload(b"{}\n", b""), b"{}"
        )
        invalid_errors = (
            (1, b"unexpected", b"phase9_live_proof_refused\n"),
            (1, b"", b"phase9_live_proof_refused\nextra\n"),
            (1, b"", b"phase9_live_proof_refused\xff\n"),
            (2, b"", b"phase9_live_proof_refused\n"),
        )
        for status, stdout, stderr in invalid_errors:
            with self.subTest(status=status, stdout=stdout, stderr=stderr):
                with self.assertRaisesRegex(
                    issuer.Phase9ProofIssuerError,
                    "runner_output_invalid",
                ):
                    issuer._strict_runner_error(status, stdout, stderr)
        for stdout, stderr in (
            (b"{}\n\n", b""),
            (b"{}\n", b"unexpected\n"),
            (b"{}", b""),
        ):
            with self.subTest(stdout=stdout, stderr=stderr):
                with self.assertRaisesRegex(
                    issuer.Phase9ProofIssuerError,
                    "runner_output_invalid",
                ):
                    issuer._strict_runner_receipt_payload(stdout, stderr)

    def test_online_signing_transport_is_absent(self) -> None:
        source = Path(proof.__file__).read_text(encoding="utf-8")
        issuer_source = Path(issuer.__file__).read_text(encoding="utf-8")
        self.assertNotIn("ROLLBACK_REQUEST_FD", source)
        self.assertNotIn("ROLLBACK_RESPONSE_FD", source)
        self.assertNotIn("ExactRollbackSigningAuthority", issuer_source)
        self.assertNotIn("signing_authority", issuer_source)

    def test_pre_spawn_failure_never_starts_install_recovery(self) -> None:
        selected_context = context()
        with (
            mock.patch.object(
                proof, "_verify_host_clock_synchronized", return_value=True
            ),
            mock.patch.object(proof, "_prepare_fixed_substrate"),
            mock.patch.object(
                proof,
                "preflight_anonymous_publication_capability",
                return_value=None,
            ),
            mock.patch.object(
                proof,
                "_verify_start_authority_pair_before_install",
                return_value={
                    "recovery_reservation_claim_sha256": "a" * 64,
                    "install_authority_claim_sha256": "b" * 64,
                    "start_authority_pair_claimed_atomically": True,
                },
            ),
            mock.patch.object(
                proof,
                "_verified_install_context",
                return_value=(selected_context, object()),
            ),
            mock.patch.object(
                proof,
                "_expected_install_execution_id",
                return_value=INSTALL_EXECUTION,
            ),
            mock.patch.object(
                proof,
                "_execution_directories",
                side_effect=RuntimeError("pre-spawn-failure"),
            ),
            mock.patch.object(proof, "_fork_worker") as fork_worker,
            mock.patch.object(
                proof, "_recover_verified_install_receipt"
            ) as recover,
            self.assertRaisesRegex(RuntimeError, "pre-spawn-failure"),
        ):
            proof.run_live_proof(inputs())
        fork_worker.assert_not_called()
        recover.assert_not_called()

    def test_install_recovery_requires_exact_durable_execution_evidence(self) -> None:
        with (
            mock.patch.object(
                proof, "_exact_install_execution_evidence_present",
                side_effect=proof.LiveProofError(
                    "phase9_live_proof_install_recovery_evidence_absent"
                ),
            ),
            mock.patch.object(proof, "_fork_worker") as fork_worker,
            self.assertRaisesRegex(
                proof.LiveProofError, "install_recovery_evidence_absent"
            ),
        ):
            proof._recover_verified_install_receipt(
                context=context(), execution_id=INSTALL_EXECUTION
            )
        fork_worker.assert_not_called()

    def test_boundary_monitor_failure_terminates_and_reaps_owned_child(self) -> None:
        pid = 321
        descriptor = 654
        with (
            mock.patch.object(proof.time, "monotonic", return_value=0.0),
            mock.patch.object(
                proof.os,
                "waitpid",
                side_effect=((0, 0), (pid, signal.SIGKILL)),
            ) as waitpid,
            mock.patch.object(proof.os, "kill") as kill,
            mock.patch.object(proof.os, "close") as close,
            mock.patch.object(
                proof,
                "_execution_directories",
                side_effect=RuntimeError("monitor-failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "monitor-failure"),
        ):
            proof._kill_after_new_durable_boundary(
                context=context(),
                boundary_kind="install",
                pid=pid,
                read_fd=descriptor,
                expected_execution_id=INSTALL_EXECUTION,
                allowed_existing_directories=frozenset(),
                journal_name="journal.jsonl",
                step_id=proof.INSTALL_KILL_STEP,
                recovery_reservation_claim_sha256="a" * 64,
                install_authority_claim_sha256="b" * 64,
            )
        kill.assert_called_once_with(pid, signal.SIGKILL)
        self.assertEqual(waitpid.call_count, 2)
        close.assert_called_once_with(descriptor)

    def test_boundary_kill_is_armed_durably_before_sigkill(self) -> None:
        events: list[str] = []
        evidence = proof.BoundaryJournalEvidence(
            boundary_kind="install",
            execution_id=INSTALL_EXECUTION,
            journal_name="journal.jsonl",
            plan_sha256="7" * 64,
            attempt_id="install-" + "8" * 40,
            boundary_step_id=proof.INSTALL_KILL_STEP,
            boundary_record_sequence=8,
            boundary_record_sha256="9" * 64,
            journal_sequence_at_arm=8,
            journal_head_sha256_at_arm="9" * 64,
            journal_record_sha256s=tuple(str(index) * 64 for index in range(1, 9)),
        )
        arm = {"execution_id": INSTALL_EXECUTION, "receipt_sha256": "a" * 64}

        def observe_stop(
            unused_pid: int,
            unused_options: int,
        ) -> tuple[int, int]:
            events.append("sigstop-observed")
            return 321, (int(signal.SIGSTOP) << 8) | 0x7F

        def read_boundary(**unused: object) -> proof.BoundaryJournalEvidence:
            events.append("journal-reread")
            return evidence

        def exclude() -> bool:
            events.append("global-lock-excluded")
            return True

        def exclude_children(unused_pid: int) -> bool:
            events.append("worker-children-absent")
            return True

        def persist(unused: object) -> dict[str, object]:
            events.append("arm-fsynced")
            return arm

        def kill(unused_pid: int, unused_fd: int | None) -> int:
            events.append("sigkill-reaped")
            return int(signal.SIGKILL)

        with (
            mock.patch.object(proof.time, "monotonic", return_value=0.0),
            mock.patch.object(
                proof.os,
                "waitpid",
                side_effect=observe_stop,
            ),
            mock.patch.object(
                proof,
                "_execution_directories",
                return_value=frozenset({INSTALL_EXECUTION}),
            ),
            mock.patch.object(
                proof, "_boundary_journal_evidence", side_effect=read_boundary
            ),
            mock.patch.object(
                proof,
                "_verify_stopped_worker_has_no_children",
                side_effect=exclude_children,
            ),
            mock.patch.object(
                proof,
                "_verify_stopped_worker_holds_global_execution_lock",
                side_effect=exclude,
            ),
            mock.patch.object(
                proof,
                "_build_process_death_arm_receipt",
                return_value=arm,
            ),
            mock.patch.object(
                proof.process_death_arm_receipt,
                "persist_process_death_arm_receipt",
                side_effect=persist,
            ),
            mock.patch.object(
                proof, "_terminate_and_reap_worker", side_effect=kill
            ),
        ):
            result = proof._kill_after_new_durable_boundary(
                context=context(),
                boundary_kind="install",
                pid=321,
                read_fd=654,
                expected_execution_id=INSTALL_EXECUTION,
                allowed_existing_directories=frozenset(),
                journal_name="journal.jsonl",
                step_id=proof.INSTALL_KILL_STEP,
                recovery_reservation_claim_sha256="b" * 64,
                install_authority_claim_sha256="c" * 64,
            )
        self.assertEqual(dict(result), arm)
        self.assertEqual(
            events,
            [
                "sigstop-observed",
                "journal-reread",
                "worker-children-absent",
                "global-lock-excluded",
                "arm-fsynced",
                "sigkill-reaped",
            ],
        )

    def test_nonempty_worker_child_set_refuses_before_arm_and_kills_worker(
        self,
    ) -> None:
        pid = 321
        descriptor = 654
        evidence = proof.BoundaryJournalEvidence(
            boundary_kind="install",
            execution_id=INSTALL_EXECUTION,
            journal_name="journal.jsonl",
            plan_sha256="7" * 64,
            attempt_id="install-" + "8" * 40,
            boundary_step_id=proof.INSTALL_KILL_STEP,
            boundary_record_sequence=8,
            boundary_record_sha256="9" * 64,
            journal_sequence_at_arm=8,
            journal_head_sha256_at_arm="9" * 64,
            journal_record_sha256s=tuple(str(index) * 64 for index in range(1, 9)),
        )
        with (
            mock.patch.object(proof.time, "monotonic", return_value=0.0),
            mock.patch.object(
                proof.os,
                "waitpid",
                return_value=(pid, (int(signal.SIGSTOP) << 8) | 0x7F),
            ),
            mock.patch.object(
                proof,
                "_execution_directories",
                return_value=frozenset({INSTALL_EXECUTION}),
            ),
            mock.patch.object(
                proof,
                "_boundary_journal_evidence",
                return_value=evidence,
            ),
            mock.patch.object(
                proof,
                "_verify_stopped_worker_has_no_children",
                side_effect=proof.LiveProofError(
                    "phase9_live_proof_worker_child_exclusion_unproved"
                ),
            ),
            mock.patch.object(
                proof,
                "_build_process_death_arm_receipt",
            ) as build_arm,
            mock.patch.object(
                proof.process_death_arm_receipt,
                "persist_process_death_arm_receipt",
            ) as persist_arm,
            mock.patch.object(
                proof,
                "_terminate_and_reap_worker",
                return_value=int(signal.SIGKILL),
            ) as terminate,
            self.assertRaisesRegex(
                proof.LiveProofError,
                "worker_child_exclusion_unproved",
            ),
        ):
            proof._kill_after_new_durable_boundary(
                context=context(),
                boundary_kind="install",
                pid=pid,
                read_fd=descriptor,
                expected_execution_id=INSTALL_EXECUTION,
                allowed_existing_directories=frozenset(),
                journal_name="journal.jsonl",
                step_id=proof.INSTALL_KILL_STEP,
                recovery_reservation_claim_sha256="a" * 64,
                install_authority_claim_sha256="b" * 64,
            )
        build_arm.assert_not_called()
        persist_arm.assert_not_called()
        terminate.assert_called_once_with(pid, descriptor)

    def test_later_journal_head_refuses_before_arm_and_kills_worker(self) -> None:
        pid = 321
        descriptor = 654
        later_head = proof.BoundaryJournalEvidence(
            boundary_kind="install",
            execution_id=INSTALL_EXECUTION,
            journal_name="journal.jsonl",
            plan_sha256="7" * 64,
            attempt_id="install-" + "8" * 40,
            boundary_step_id=proof.INSTALL_KILL_STEP,
            boundary_record_sequence=8,
            boundary_record_sha256="9" * 64,
            journal_sequence_at_arm=9,
            journal_head_sha256_at_arm="a" * 64,
            journal_record_sha256s=tuple(str(index) * 64 for index in range(1, 10)),
        )
        with (
            mock.patch.object(proof.time, "monotonic", return_value=0.0),
            mock.patch.object(
                proof.os,
                "waitpid",
                return_value=(pid, (int(signal.SIGSTOP) << 8) | 0x7F),
            ),
            mock.patch.object(
                proof,
                "_execution_directories",
                return_value=frozenset({INSTALL_EXECUTION}),
            ),
            mock.patch.object(
                proof,
                "_boundary_journal_evidence",
                return_value=later_head,
            ),
            mock.patch.object(
                proof,
                "_build_process_death_arm_receipt",
            ) as build_arm,
            mock.patch.object(
                proof.process_death_arm_receipt,
                "persist_process_death_arm_receipt",
            ) as persist_arm,
            mock.patch.object(
                proof,
                "_terminate_and_reap_worker",
                return_value=int(signal.SIGKILL),
            ) as terminate,
            self.assertRaisesRegex(
                proof.LiveProofError,
                "cooperative_stop_boundary_invalid",
            ),
        ):
            proof._kill_after_new_durable_boundary(
                context=context(),
                boundary_kind="install",
                pid=pid,
                read_fd=descriptor,
                expected_execution_id=INSTALL_EXECUTION,
                allowed_existing_directories=frozenset(),
                journal_name="journal.jsonl",
                step_id=proof.INSTALL_KILL_STEP,
                recovery_reservation_claim_sha256="a" * 64,
                install_authority_claim_sha256="b" * 64,
            )
        build_arm.assert_not_called()
        persist_arm.assert_not_called()
        terminate.assert_called_once_with(pid, descriptor)

    @unittest.skipUnless(
        sys.platform == "linux" and hasattr(os, "fork") and os.geteuid() == 0,
        "root Linux lock ownership proof required",
    )
    def test_real_stopped_worker_has_no_children_holds_lock_and_is_sigkilled(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "locks"
            parent.mkdir(mode=0o700)
            os.chmod(parent, 0o700)
            path = parent / "phase9.lock"
            read_fd, write_fd = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(read_fd)
                lock = GlobalExecutionLock(
                    path,
                    expected_uid=0,
                    expected_gid=0,
                )
                try:
                    os.write(write_fd, b"L")
                    os.kill(os.getpid(), signal.SIGSTOP)
                finally:
                    lock.close()
                    os.close(write_fd)
                os._exit(1)

            os.close(write_fd)
            child_owned = True
            try:
                self.assertEqual(os.read(read_fd, 1), b"L")
                waited, stopped_status = os.waitpid(pid, os.WUNTRACED)
                self.assertEqual(waited, pid)
                self.assertTrue(os.WIFSTOPPED(stopped_status))
                self.assertEqual(os.WSTOPSIG(stopped_status), signal.SIGSTOP)
                with mock.patch.object(proof, "GLOBAL_LOCK_PATH", path):
                    self.assertTrue(
                        proof._verify_stopped_worker_has_no_children(pid)
                    )
                    self.assertTrue(
                        proof._verify_stopped_worker_holds_global_execution_lock()
                    )
                os.kill(pid, signal.SIGKILL)
                waited, killed_status = os.waitpid(pid, 0)
                child_owned = False
                self.assertEqual(waited, pid)
                self.assertTrue(os.WIFSIGNALED(killed_status))
                self.assertEqual(os.WTERMSIG(killed_status), signal.SIGKILL)
            finally:
                if child_owned:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        os.waitpid(pid, 0)
                    except ChildProcessError:
                        pass
                os.close(read_fd)

    def test_completed_journal_must_contain_exact_armed_boundary_and_head(
        self,
    ) -> None:
        arm = {
            "candidate_git_commit": COMMIT,
            "candidate_git_tree": TREE,
            "package_manifest_sha256": PACKAGE,
            "controller_runtime_receipt_sha256": RUNTIME,
            "recovery_capsule_sha256": proof._sha(b"{}"),
            "execution_id": INSTALL_EXECUTION,
            "journal_plan_sha256": "a" * 64,
            "journal_attempt_id": "install-" + "b" * 40,
            "journal_name": "journal.jsonl",
            "boundary_step_id": proof.INSTALL_KILL_STEP,
            "boundary_record_sequence": 2,
            "boundary_record_sha256": "c" * 64,
            "journal_sequence_at_arm": 2,
            "journal_head_sha256_at_arm": "c" * 64,
        }
        completed = {
            "execution_id": INSTALL_EXECUTION,
            "plan_sha256": "a" * 64,
            "attempt_id": "install-" + "b" * 40,
            "journal_sequence": 3,
            "journal_head_sha256": "d" * 64,
        }
        evidence = proof.BoundaryJournalEvidence(
            boundary_kind="install",
            execution_id=INSTALL_EXECUTION,
            journal_name="journal.jsonl",
            plan_sha256="a" * 64,
            attempt_id="install-" + "b" * 40,
            boundary_step_id=proof.INSTALL_KILL_STEP,
            boundary_record_sequence=2,
            boundary_record_sha256="c" * 64,
            journal_sequence_at_arm=3,
            journal_head_sha256_at_arm="d" * 64,
            journal_record_sha256s=("e" * 64, "c" * 64, "d" * 64),
        )
        with (
            mock.patch.object(
                proof, "verify_install_receipt", side_effect=lambda value: dict(value)
            ),
            mock.patch.object(
                proof.process_death_arm_receipt,
                "verify_process_death_arm_receipt",
                side_effect=lambda value, **unused: dict(value),
            ),
            mock.patch.object(
                proof, "_boundary_journal_evidence", return_value=evidence
            ),
        ):
            verified = proof._verify_death_arm_against_completed_journal(
                context=context(),
                arm_receipt=arm,
                completed_receipt=completed,
                boundary_kind="install",
            )
            self.assertEqual(dict(verified), arm)
            with self.assertRaisesRegex(
                proof.LiveProofError, "process_death_arm_mismatch"
            ):
                proof._verify_death_arm_against_completed_journal(
                    context=context(),
                    arm_receipt=dict(arm, journal_head_sha256_at_arm="f" * 64),
                    completed_receipt=completed,
                    boundary_kind="install",
                )

    def test_one_arm_is_cleanup_only_and_two_arms_reconstruct(self) -> None:
        selected_context = context()
        install = {"execution_id": INSTALL_EXECUTION}
        rollback = {"execution_id": ROLLBACK_EXECUTION}
        absence = {"exact_resources_absent": True}
        install_arm = {"receipt_sha256": "a" * 64}
        rollback_arm = {"receipt_sha256": "b" * 64}
        pair = (
            mock.Mock(claim_sha256="c" * 64),
            mock.Mock(claim_sha256="d" * 64),
        )
        with (
            mock.patch.object(
                proof, "verify_install_receipt", side_effect=lambda value: dict(value)
            ),
            mock.patch.object(
                proof,
                "verify_empty_rollback_receipt",
                side_effect=lambda value: dict(value),
            ),
            mock.patch.object(
                proof.process_death_arm_receipt,
                "read_process_death_arm_receipt_if_present",
                side_effect=(install_arm, None),
            ),
            mock.patch.object(proof, "_build_rollback_documents") as build,
            mock.patch.object(
                proof, "persist_verified_promotable_live_receipt"
            ) as persist,
        ):
            self.assertIsNone(
                proof._reconstruct_promotable_live_receipt_from_retained_evidence(
                    context=selected_context,
                    install_receipt=install,
                    rollback_receipt=rollback,
                    absence=absence,
                )
            )
        build.assert_not_called()
        persist.assert_not_called()

        live = {"receipt_sha256": "e" * 64}
        with (
            mock.patch.object(
                proof, "verify_install_receipt", side_effect=lambda value: dict(value)
            ),
            mock.patch.object(
                proof,
                "verify_empty_rollback_receipt",
                side_effect=lambda value: dict(value),
            ),
            mock.patch.object(
                proof.process_death_arm_receipt,
                "read_process_death_arm_receipt_if_present",
                side_effect=(install_arm, rollback_arm),
            ),
            mock.patch.object(
                proof, "_build_rollback_documents", return_value=mock.Mock()
            ),
            mock.patch.object(
                proof, "_start_authority_pair_identities", return_value=pair
            ),
            mock.patch.object(proof, "_proof_receipt", return_value=live),
            mock.patch.object(
                proof,
                "persist_verified_promotable_live_receipt",
                return_value=live,
            ) as persist,
        ):
            reconstructed = (
                proof._reconstruct_promotable_live_receipt_from_retained_evidence(
                    context=selected_context,
                    install_receipt=install,
                    rollback_receipt=rollback,
                    absence=absence,
                )
            )
        self.assertEqual(dict(reconstructed or {}), live)
        persist.assert_called_once()

    def test_recover_only_promotes_both_retained_arms_after_fresh_absence(
        self,
    ) -> None:
        selected_context = context()
        install = {"execution_id": INSTALL_EXECUTION}
        rollback = {"execution_id": ROLLBACK_EXECUTION}
        absence = {"exact_resources_absent": True}
        live = {"receipt_sha256": "f" * 64}
        with (
            mock.patch.object(
                proof, "_verify_host_clock_synchronized", return_value=True
            ),
            mock.patch.object(
                proof,
                "_verified_install_context",
                return_value=(selected_context, object()),
            ),
            mock.patch.object(proof, "_prepare_fixed_substrate"),
            mock.patch.object(proof, "_fork_worker", return_value=(1, 2)),
            mock.patch.object(
                proof,
                "_wait_worker",
                return_value={
                    "action": "recovered",
                    "install_receipt": install,
                    "rollback_receipt": rollback,
                    "absence": absence,
                },
            ),
            mock.patch.object(
                proof, "verify_install_receipt", side_effect=lambda value: dict(value)
            ),
            mock.patch.object(
                proof,
                "verify_empty_rollback_receipt",
                side_effect=lambda value: dict(value),
            ),
            mock.patch.object(
                proof,
                "read_verified_promotable_live_receipt_if_present",
                return_value=None,
            ),
            mock.patch.object(
                proof,
                "_reconstruct_promotable_live_receipt_from_retained_evidence",
                return_value=live,
            ) as reconstruct,
        ):
            result = proof.recover_live_proof(inputs())
        self.assertEqual(dict(result), live)
        reconstruct.assert_called_once()

    def test_partial_install_recovery_resumes_then_exactly_rolls_back(self) -> None:
        selected_context = context()
        install_receipt = {"execution_id": INSTALL_EXECUTION}
        rollback_receipt = {"exact_resources_absent": True}
        absence = {"exact_resources_absent": True}
        documents = proof.RollbackDocuments(
            scope=b"{}",
            authorization=b"{}",
            trust_bundle=b"{}",
            key_id="9" * 64,
            eligibility={},
            resources=object(),
            resolved_store_spec={},
        )
        modes: list[proof.WorkerMode] = []

        def fork(
            mode: proof.WorkerMode,
            unused: object,
            **unused_options: object,
        ) -> tuple[int, int]:
            modes.append(mode)
            return len(modes), len(modes) + 10

        with (
            mock.patch.object(
                proof, "_verify_host_clock_synchronized", return_value=True
            ),
            mock.patch.object(proof, "_prepare_fixed_substrate"),
            mock.patch.object(
                proof,
                "preflight_anonymous_publication_capability",
                return_value=None,
            ),
            mock.patch.object(
                proof,
                "_verify_start_authority_pair_before_install",
                return_value={
                    "recovery_reservation_claim_sha256": "a" * 64,
                    "install_authority_claim_sha256": "b" * 64,
                    "start_authority_pair_claimed_atomically": True,
                },
            ),
            mock.patch.object(proof, "_verified_install_context", return_value=(selected_context, object())),
            mock.patch.object(proof, "_expected_install_execution_id", return_value=INSTALL_EXECUTION),
            mock.patch.object(proof, "_execution_directories", return_value=frozenset()),
            mock.patch.object(proof, "_fork_worker", side_effect=fork),
            mock.patch.object(proof, "_kill_after_new_durable_boundary", side_effect=RuntimeError("install-killed")),
            mock.patch.object(proof, "_recover_verified_install_receipt", return_value=install_receipt),
            mock.patch.object(proof, "_build_rollback_documents", return_value=documents),
            mock.patch.object(proof, "_verified_rollback_capability"),
            mock.patch.object(proof, "_wait_worker", side_effect=(rollback_receipt, absence)),
            mock.patch.object(proof, "verify_empty_rollback_receipt", side_effect=lambda value: dict(value)),
        ):
            with self.assertRaisesRegex(RuntimeError, "install-killed"):
                proof.run_live_proof(inputs())
        self.assertEqual(
            modes,
            [
                proof.WorkerMode.RESUME_INSTALL,
                proof.WorkerMode.ROLLBACK,
                proof.WorkerMode.VERIFY_ABSENCE,
            ],
        )

    def test_ambiguous_install_worker_cleanup_never_starts_sibling_recovery(
        self,
    ) -> None:
        selected_context = context()
        modes: list[proof.WorkerMode] = []

        def fork(
            mode: proof.WorkerMode,
            unused_context: object,
            **unused_options: object,
        ) -> tuple[int, int]:
            modes.append(mode)
            return 1, 11

        with (
            mock.patch.object(
                proof, "_verify_host_clock_synchronized", return_value=True
            ),
            mock.patch.object(proof, "_prepare_fixed_substrate"),
            mock.patch.object(
                proof,
                "preflight_anonymous_publication_capability",
                return_value=None,
            ),
            mock.patch.object(
                proof,
                "_verify_start_authority_pair_before_install",
                return_value={
                    "recovery_reservation_claim_sha256": "a" * 64,
                    "install_authority_claim_sha256": "b" * 64,
                    "start_authority_pair_claimed_atomically": True,
                },
            ),
            mock.patch.object(
                proof,
                "_verified_install_context",
                return_value=(selected_context, object()),
            ),
            mock.patch.object(
                proof,
                "_expected_install_execution_id",
                return_value=INSTALL_EXECUTION,
            ),
            mock.patch.object(
                proof, "_execution_directories", return_value=frozenset()
            ),
            mock.patch.object(proof, "_fork_worker", side_effect=fork),
            mock.patch.object(
                proof,
                "_kill_after_new_durable_boundary",
                side_effect=proof.LiveProofError(
                    "phase9_live_proof_worker_cleanup_unproved"
                ),
            ),
            mock.patch.object(
                proof, "_recover_verified_install_receipt"
            ) as recover_install,
            mock.patch.object(
                proof, "_complete_exact_rollback_recovery"
            ) as recover_rollback,
            self.assertRaisesRegex(
                proof.LiveProofError,
                "^phase9_live_proof_worker_cleanup_unproved$",
            ),
        ):
            proof.run_live_proof(inputs())
        self.assertEqual(modes, [proof.WorkerMode.RESUME_INSTALL])
        recover_install.assert_not_called()
        recover_rollback.assert_not_called()

    def test_ambiguous_rollback_worker_cleanup_never_starts_sibling_recovery(
        self,
    ) -> None:
        selected_context = context()
        install_receipt = {"execution_id": INSTALL_EXECUTION}
        modes: list[proof.WorkerMode] = []

        def fork(
            mode: proof.WorkerMode,
            unused_context: object,
            **unused_options: object,
        ) -> tuple[int, int]:
            modes.append(mode)
            return len(modes), len(modes) + 10

        with (
            mock.patch.object(
                proof, "_verify_host_clock_synchronized", return_value=True
            ),
            mock.patch.object(proof, "_prepare_fixed_substrate"),
            mock.patch.object(
                proof,
                "preflight_anonymous_publication_capability",
                return_value=None,
            ),
            mock.patch.object(
                proof,
                "_verify_start_authority_pair_before_install",
                return_value={
                    "recovery_reservation_claim_sha256": "a" * 64,
                    "install_authority_claim_sha256": "b" * 64,
                    "start_authority_pair_claimed_atomically": True,
                },
            ),
            mock.patch.object(
                proof,
                "_verified_install_context",
                return_value=(selected_context, object()),
            ),
            mock.patch.object(
                proof,
                "_expected_install_execution_id",
                return_value=INSTALL_EXECUTION,
            ),
            mock.patch.object(
                proof,
                "_expected_rollback_execution_id",
                return_value=ROLLBACK_EXECUTION,
            ),
            mock.patch.object(
                proof,
                "_execution_directories",
                side_effect=(frozenset(), frozenset()),
            ),
            mock.patch.object(proof, "_fork_worker", side_effect=fork),
            mock.patch.object(
                proof,
                "_kill_after_new_durable_boundary",
                side_effect=(
                    {"execution_id": INSTALL_EXECUTION},
                    proof.LiveProofError(
                        "phase9_live_proof_worker_cleanup_unproved"
                    ),
                ),
            ),
            mock.patch.object(
                proof, "_wait_worker", return_value=install_receipt
            ),
            mock.patch.object(
                proof,
                "verify_install_receipt",
                side_effect=lambda value: dict(value),
            ),
            mock.patch.object(
                proof, "_build_rollback_documents", return_value=mock.Mock()
            ),
            mock.patch.object(
                proof, "_complete_exact_rollback_recovery"
            ) as recover_rollback,
            self.assertRaisesRegex(
                proof.LiveProofError,
                "^phase9_live_proof_worker_cleanup_unproved$",
            ),
        ):
            proof.run_live_proof(inputs())
        self.assertEqual(
            modes,
            [
                proof.WorkerMode.RESUME_INSTALL,
                proof.WorkerMode.RESUME_INSTALL,
                proof.WorkerMode.ROLLBACK,
            ],
        )
        recover_rollback.assert_not_called()

    def test_live_success_persists_promotable_receipt_before_return(self) -> None:
        selected_context = context()
        install_receipt = {
            "execution_id": INSTALL_EXECUTION,
        }
        rollback_receipt = {
            "execution_id": ROLLBACK_EXECUTION,
        }
        receipt = {"receipt_sha256": "c" * 64}
        events: list[str] = []

        def persist(**kwargs: object) -> dict[str, object]:
            events.append("persist")
            self.assertIs(kwargs["receipt"], receipt)
            return receipt

        with (
            mock.patch.object(
                proof, "_verify_host_clock_synchronized", return_value=True
            ),
            mock.patch.object(proof, "_prepare_fixed_substrate"),
            mock.patch.object(
                proof,
                "preflight_anonymous_publication_capability",
                return_value=None,
            ),
            mock.patch.object(
                proof,
                "_verify_start_authority_pair_before_install",
                return_value={
                    "recovery_reservation_claim_sha256": "a" * 64,
                    "install_authority_claim_sha256": "b" * 64,
                    "start_authority_pair_claimed_atomically": True,
                },
            ),
            mock.patch.object(
                proof,
                "_verified_install_context",
                return_value=(selected_context, object()),
            ),
            mock.patch.object(
                proof,
                "_expected_install_execution_id",
                return_value=INSTALL_EXECUTION,
            ),
            mock.patch.object(
                proof,
                "_expected_rollback_execution_id",
                return_value=ROLLBACK_EXECUTION,
            ),
            mock.patch.object(
                proof,
                "_execution_directories",
                side_effect=(frozenset(), frozenset()),
            ),
            mock.patch.object(
                proof,
                "_fork_worker",
                side_effect=((1, 11), (2, 12), (3, 13), (4, 14), (5, 15)),
            ) as fork_worker,
            mock.patch.object(
                proof,
                "_kill_after_new_durable_boundary",
                side_effect=(
                    {"execution_id": INSTALL_EXECUTION},
                    {"execution_id": ROLLBACK_EXECUTION},
                ),
            ),
            mock.patch.object(
                proof,
                "_wait_worker",
                side_effect=(install_receipt, rollback_receipt, {"absent": True}),
            ),
            mock.patch.object(
                proof,
                "verify_install_receipt",
                side_effect=lambda value: dict(value),
            ),
            mock.patch.object(
                proof,
                "verify_empty_rollback_receipt",
                side_effect=lambda value: dict(value),
            ),
            mock.patch.object(proof, "_build_rollback_documents", return_value=mock.Mock()),
            mock.patch.object(proof, "_proof_receipt", return_value=receipt),
            mock.patch.object(
                proof,
                "persist_verified_promotable_live_receipt",
                side_effect=persist,
            ),
        ):
            result = proof.run_live_proof(inputs())
            events.append("returned")
        self.assertEqual(dict(result), receipt)
        self.assertEqual(events, ["persist", "returned"])
        self.assertEqual(
            tuple(
                (
                    call.args[0],
                    call.kwargs.get("cooperative_boundary_kind"),
                )
                for call in fork_worker.call_args_list
            ),
            (
                (
                    proof.WorkerMode.RESUME_INSTALL,
                    proof.process_death_arm_receipt.INSTALL_BOUNDARY_KIND,
                ),
                (proof.WorkerMode.RESUME_INSTALL, None),
                (
                    proof.WorkerMode.ROLLBACK,
                    proof.process_death_arm_receipt.EMPTY_ROLLBACK_BOUNDARY_KIND,
                ),
                (proof.WorkerMode.RESUME_ROLLBACK, None),
                (proof.WorkerMode.VERIFY_ABSENCE, None),
            ),
        )

    def test_live_receipt_persistence_failure_blocks_success(self) -> None:
        source = Path(proof.__file__).read_text(encoding="utf-8")
        persist_position = source.index(
            "durable_live_receipt = persist_verified_promotable_live_receipt("
        )
        return_position = source.index(
            "return MappingProxyType(dict(durable_live_receipt))",
            persist_position,
        )
        stdout_position = source.index("sys.stdout.buffer.write(", return_position)
        self.assertLess(persist_position, return_position)
        self.assertLess(return_position, stdout_position)
        self.assertIn(
            '"phase9_live_proof_durable_receipt_persistence_failed"',
            source[persist_position:return_position],
        )

    def test_durable_publication_preflight_failure_precedes_authority_and_workers(
        self,
    ) -> None:
        selected_context = context()
        with (
            mock.patch.object(
                proof, "_verify_host_clock_synchronized", return_value=True
            ),
            mock.patch.object(proof, "_prepare_fixed_substrate"),
            mock.patch.object(
                proof,
                "_verified_install_context",
                return_value=(selected_context, object()),
            ),
            mock.patch.object(
                proof,
                "preflight_anonymous_publication_capability",
                side_effect=proof.DurableLiveProofReceiptError("unavailable"),
            ),
            mock.patch.object(
                proof, "_verify_start_authority_pair_before_install"
            ) as claim,
            mock.patch.object(proof, "_fork_worker") as fork,
            self.assertRaisesRegex(
                proof.LiveProofError, "durable_receipt_preflight_failed"
            ),
        ):
            proof.run_live_proof(inputs())
        claim.assert_not_called()
        fork.assert_not_called()

    def test_persistence_exception_cannot_return_live_success(self) -> None:
        selected_context = context()
        install_receipt = {"execution_id": INSTALL_EXECUTION}
        rollback_receipt = {"execution_id": ROLLBACK_EXECUTION}
        with (
            mock.patch.object(
                proof, "_verify_host_clock_synchronized", return_value=True
            ),
            mock.patch.object(proof, "_prepare_fixed_substrate"),
            mock.patch.object(
                proof,
                "preflight_anonymous_publication_capability",
                return_value=None,
            ),
            mock.patch.object(
                proof,
                "_verify_start_authority_pair_before_install",
                return_value={
                    "recovery_reservation_claim_sha256": "a" * 64,
                    "install_authority_claim_sha256": "b" * 64,
                    "start_authority_pair_claimed_atomically": True,
                },
            ),
            mock.patch.object(
                proof,
                "_verified_install_context",
                return_value=(selected_context, object()),
            ),
            mock.patch.object(
                proof,
                "_expected_install_execution_id",
                return_value=INSTALL_EXECUTION,
            ),
            mock.patch.object(
                proof,
                "_expected_rollback_execution_id",
                return_value=ROLLBACK_EXECUTION,
            ),
            mock.patch.object(
                proof,
                "_execution_directories",
                side_effect=(frozenset(), frozenset()),
            ),
            mock.patch.object(
                proof,
                "_fork_worker",
                side_effect=((1, 11), (2, 12), (3, 13), (4, 14), (5, 15)),
            ),
            mock.patch.object(
                proof,
                "_kill_after_new_durable_boundary",
                side_effect=(
                    {"execution_id": INSTALL_EXECUTION},
                    {"execution_id": ROLLBACK_EXECUTION},
                ),
            ),
            mock.patch.object(
                proof,
                "_wait_worker",
                side_effect=(install_receipt, rollback_receipt, {"absent": True}),
            ),
            mock.patch.object(
                proof,
                "verify_install_receipt",
                side_effect=lambda value: dict(value),
            ),
            mock.patch.object(
                proof,
                "verify_empty_rollback_receipt",
                side_effect=lambda value: dict(value),
            ),
            mock.patch.object(proof, "_build_rollback_documents", return_value=mock.Mock()),
            mock.patch.object(
                proof, "_proof_receipt", return_value={"receipt_sha256": "c" * 64}
            ),
            mock.patch.object(
                proof,
                "persist_verified_promotable_live_receipt",
                side_effect=proof.DurableLiveProofReceiptError("fsync"),
            ),
            mock.patch.object(
                proof, "_complete_exact_rollback_recovery"
            ) as recovery,
            self.assertRaisesRegex(
                proof.LiveProofError, "durable_receipt_persistence_failed"
            ),
        ):
            proof.run_live_proof(inputs())
        recovery.assert_called_once()

    def test_authority_and_issuer_are_not_package_artifacts(self) -> None:
        from tools.governed_memory_install import package

        self.assertNotIn(
            "tools/governed_memory_validation/phase9_disposable_proof_authority.py",
            package.EXPECTED_ARTIFACTS,
        )
        self.assertNotIn(
            "tools/governed_memory_validation/issue_disposable_installation_live_proof.py",
            package.EXPECTED_ARTIFACTS,
        )

    def test_issuer_binds_its_repo_only_source_to_exact_clean_git_tree(self) -> None:
        source = Path(issuer.__file__).read_text(encoding="utf-8")
        self.assertIn('("rev-parse", "HEAD")', source)
        self.assertIn('("rev-parse", "HEAD^{tree}")', source)
        self.assertIn('("status", "--porcelain=v1", "--untracked-files=all")', source)
        self.assertIn('"safe.directory=" + str(_ISSUER_REPOSITORY_ROOT)', source)
        self.assertIn("verify_exact_clean_candidate(inputs)", source)
        held_guard = source.split("with guard:", 1)[1]
        disposition = held_guard.index(
            "_require_production_pre_effect_disposition(inputs=inputs)"
        )
        checks = [
            index
            for index in range(len(held_guard))
            if held_guard.startswith("verify_exact_clean_candidate(inputs)", index)
        ]
        bootstrap = held_guard.index(
            "substrate_bootstrap.bootstrap_phase9_disposable_store_substrate("
        )
        substrate = held_guard.index("runner._prepare_fixed_substrate()")
        self.assertGreaterEqual(len(checks), 2)
        self.assertLess(checks[0], disposition)
        self.assertLess(disposition, checks[1])
        self.assertLess(checks[1], bootstrap)
        self.assertLess(bootstrap, substrate)

    def test_issuer_permit_bridge_binds_all_proof_inputs_and_authority(self) -> None:
        authority = mock.sentinel.authority
        with mock.patch.object(
            issuer.phase9_permitted_candidate,
            "require_exact_permitted_candidate",
            return_value=authority,
        ) as require:
            observed = issuer._require_exact_permitted_candidate(inputs())
        self.assertIs(observed, authority)
        require.assert_called_once_with(
            expected_candidate_git_commit=COMMIT,
            expected_candidate_git_tree=TREE,
            expected_package_manifest_sha256=PACKAGE,
            expected_controller_runtime_receipt_sha256=RUNTIME,
            expected_thread_id=proof.THREAD_ID,
            expected_authorization_text_sha256=proof.AUTHORIZED_TEXT_SHA256,
        )

    def test_issuer_maps_permit_refusal_before_candidate_authority(self) -> None:
        refusal = (
            issuer.phase9_permitted_candidate.Phase9PermittedCandidateError(
                "private detail"
            )
        )
        with (
            mock.patch.object(
                issuer.phase9_permitted_candidate,
                "require_exact_permitted_candidate",
                side_effect=refusal,
            ),
            self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "^phase9_proof_issuer_candidate_permit_required$",
            ),
        ):
            issuer._require_exact_permitted_candidate(inputs())

    def test_issuer_accepts_devnull_git_stderr_as_none(self) -> None:
        issuer_relative = (
            "tools/governed_memory_validation/"
            "issue_disposable_installation_live_proof.py"
        )
        blob = "c" * 40
        outputs = (
            str(issuer._ISSUER_REPOSITORY_ROOT) + "\n",
            COMMIT + "\n",
            TREE + "\n",
            "",
            f"100644 {blob} 0\t{issuer_relative}\n",
            blob + "\n",
            blob + "\n",
        )
        completed = tuple(
            subprocess.CompletedProcess(
                args=("git",),
                returncode=0,
                stdout=value.encode("ascii"),
                stderr=None,
            )
            for value in outputs
        )
        with (
            mock.patch.object(
                issuer.subprocess, "run", side_effect=completed
            ) as run,
            mock.patch.object(issuer, "_require_exact_permitted_candidate"),
        ):
            issuer.verify_exact_clean_candidate(inputs())
        self.assertEqual(run.call_count, len(outputs))
        self.assertTrue(
            all(
                call.kwargs["stderr"] is subprocess.DEVNULL
                for call in run.call_args_list
            )
        )

    def test_direct_issuer_requires_the_pinned_phase9j_runtime_before_imports(self) -> None:
        completed = subprocess.run(
            (sys.executable, "-I", "-B", str(Path(issuer.__file__)), "--help"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(
            completed.stderr,
            b"phase9_proof_issuer_pinned_runtime_required\n",
        )
        source = Path(issuer.__file__).read_text(encoding="utf-8")
        self.assertLess(
            source.index("phase9_proof_issuer_pinned_runtime_required"),
            source.index("from cryptography.hazmat.primitives import"),
        )

    def test_issuer_execs_runner_with_isolation_and_no_bytecode_writes(self) -> None:
        arguments = issuer._runner_argv(inputs())
        self.assertEqual(
            arguments[:3],
            (
                "/opt/governed-memory-controller/runtimes/"
                + RUNTIME
                + "/bin/python",
                "-I",
                "-B",
            ),
        )
        source = Path(proof.__file__).read_text(encoding="utf-8")
        self.assertIn("phase9_live_proof_bytecode_writes_not_disabled", source)

    def test_empty_pre_effect_install_journal_selects_resume(self) -> None:
        journal = Path(
            "/var/lib/governed-memory-controller/executions-v2/"
            + INSTALL_EXECUTION
            + "/journal.jsonl"
        )
        for raw in (None, b""):
            with self.subTest(raw=raw), mock.patch.object(
                proof,
                "_read_optional_root_regular_no_follow",
                return_value=raw,
            ) as read:
                self.assertFalse(
                    proof._install_journal_compensation_complete(journal)
                )
            self.assertTrue(read.call_args.kwargs["allow_empty"])

    def test_optional_file_empty_policy_is_narrow(self) -> None:
        self.assertFalse(
            proof._optional_root_file_size_allowed(
                0, 1024, allow_empty=False
            )
        )
        self.assertTrue(
            proof._optional_root_file_size_allowed(
                0, 1024, allow_empty=True
            )
        )
        self.assertTrue(
            proof._optional_root_file_size_allowed(
                1, 1024, allow_empty=False
            )
        )
        self.assertFalse(
            proof._optional_root_file_size_allowed(
                1025, 1024, allow_empty=True
            )
        )
        self.assertFalse(
            proof._optional_root_file_size_allowed(
                0, 1024, allow_empty=1  # type: ignore[arg-type]
            )
        )

    def test_nonempty_install_journal_remains_strict(self) -> None:
        journal = Path(
            "/var/lib/governed-memory-controller/executions-v2/"
            + INSTALL_EXECUTION
            + "/journal.jsonl"
        )
        for raw in (b"{}", b"not-json\n"):
            with self.subTest(raw=raw), mock.patch.object(
                proof,
                "_read_optional_root_regular_no_follow",
                return_value=raw,
            ), self.assertRaisesRegex(
                proof.LiveProofError,
                "phase9_live_proof_install_journal_invalid",
            ):
                proof._install_journal_compensation_complete(journal)
        terminal = json.dumps(
            {"event": "compensation_complete", "step_id": "I00_ATTEMPT"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii") + b"\n"
        with mock.patch.object(
            proof,
            "_read_optional_root_regular_no_follow",
            return_value=terminal,
        ):
            self.assertTrue(
                proof._install_journal_compensation_complete(journal)
            )

    def test_start_authority_pair_uses_one_clock_read_and_resumes_after_expiry(
        self,
    ) -> None:
        selected = context()
        selected.recovery_capsule.document.update(
            {
                "not_before": "2026-08-13T10:00:00Z",
                "expires_at": "2026-08-13T10:15:00Z",
            }
        )
        reservation_identity = mock.Mock(claim_sha256="a" * 64)
        install_identity = mock.Mock(claim_sha256="b" * 64)
        pair = mock.Mock(
            result="exact_execution_resumed",
            first=mock.Mock(
                claim_sha256="a" * 64,
                operation_sha256=proof.authority_operation_sha256(
                    proof.RECOVERY_RESERVATION_OPERATION
                ),
            ),
            second=mock.Mock(
                claim_sha256="b" * 64,
                operation_sha256=proof.authority_operation_sha256(
                    proof.authority.AUTHORIZATION_OPERATION
                ),
            ),
        )
        state = mock.Mock()
        state.claim_exact_nonce_pair.return_value = pair
        reading = mock.Mock(
            observed_at=datetime(2026, 8, 13, 10, 16, tzinfo=timezone.utc)
        )
        with (
            mock.patch.object(
                proof,
                "_start_authority_pair_identities",
                return_value=(reservation_identity, install_identity),
            ),
            mock.patch.object(
                proof, "read_trusted_utc", return_value=reading
            ) as clock,
        ):
            result = proof._claim_or_verify_start_authority_pair(
                selected,
                state=state,
                held_lock=object(),
                allow_new_pair=True,
            )
        self.assertEqual(result["result"], "exact_execution_resumed")
        self.assertTrue(result["start_authority_pair_claimed_atomically"])
        clock.assert_called_once()
        self.assertFalse(
            state.claim_exact_nonce_pair.call_args.kwargs["allow_new_pair"]
        )

    def test_expired_unclaimed_start_authority_pair_is_refused(self) -> None:
        selected = context()
        selected.recovery_capsule.document.update(
            {
                "not_before": "2026-08-13T10:00:00Z",
                "expires_at": "2026-08-13T10:15:00Z",
            }
        )
        state = mock.Mock()
        state.claim_exact_nonce_pair.side_effect = (
            proof.AuthorityClaimNotAllowedError("absent")
        )
        with (
            mock.patch.object(
                proof,
                "_start_authority_pair_identities",
                return_value=(mock.Mock(), mock.Mock()),
            ),
            mock.patch.object(
                proof,
                "read_trusted_utc",
                return_value=mock.Mock(
                    observed_at=datetime(
                        2026, 8, 13, 10, 16, tzinfo=timezone.utc
                    )
                ),
            ),
            self.assertRaisesRegex(
                proof.LiveProofError, "start_authority_pair_expired"
            ),
        ):
            proof._claim_or_verify_start_authority_pair(
                selected,
                state=state,
                held_lock=object(),
                allow_new_pair=True,
            )

    def test_new_capsule_preflights_durable_publication_before_pair_claim(
        self,
    ) -> None:
        events: list[str] = []
        verified_capsule = proof.VerifiedRecoveryCapsule(
            raw=b"capsule",
            document={},
            install_documents=proof.InstallDocuments(
                b"{}", b"{}", b"{}", "7" * 64
            ),
            rollback_delegation={},
            key_id="7" * 64,
            rollback_nonce="8" * 64,
        )
        base_pair = {
            "recovery_reservation_claim_sha256": "b" * 64,
            "install_authority_claim_sha256": "c" * 64,
            "start_authority_pair_claimed_atomically": True,
        }

        def run_pair(**kwargs: object) -> dict[str, object]:
            mode = kwargs["mode"]
            events.append(str(mode.value))
            return {
                **base_pair,
                "result": (
                    "nonce_claimed"
                    if mode is proof.RunnerMode.PRECLAIM_STAGED_START
                    else "exact_execution_resumed"
                ),
            }

        with (
            mock.patch.object(
                issuer.Ed25519PrivateKey, "generate", return_value=object()
            ),
            mock.patch.object(
                issuer, "build_exact_recovery_capsule", return_value={}
            ),
            mock.patch.object(issuer, "verify_exact_clean_candidate"),
            mock.patch.object(
                issuer,
                "prepare_fixed_recovery_capsule",
                side_effect=lambda unused: (
                    events.append("prepare") or ("a" * 64, (1, 2))
                ),
            ),
            mock.patch.object(
                issuer.durable_live_proof_receipt,
                "preflight_anonymous_publication_capability",
                side_effect=lambda **unused: events.append("preflight"),
            ),
            mock.patch.object(
                issuer, "_run_start_authority_pair_mode", side_effect=run_pair
            ),
            mock.patch.object(
                issuer,
                "commit_preclaimed_recovery_capsule",
                side_effect=lambda **unused: events.append("publish"),
            ),
            mock.patch.object(
                issuer.runner,
                "_load_verified_recovery_capsule",
                return_value=verified_capsule,
            ),
            mock.patch.object(
                issuer, "_sha", return_value="a" * 64
            ),
        ):
            observation, pair = issuer._prepare_preclaim_publish_new_capsule(
                inputs=inputs(), artifacts={}, guard_descriptor=9
            )
        self.assertEqual(
            events,
            [
                "prepare",
                "preflight",
                proof.RunnerMode.PRECLAIM_STAGED_START.value,
                "publish",
                proof.RunnerMode.VERIFY_PUBLISHED_START_PAIR.value,
            ],
        )
        self.assertEqual(observation.state, "published")
        self.assertEqual(pair["install_authority_claim_sha256"], "c" * 64)

    def test_durable_preflight_failure_cleans_only_staged_capsule_before_claim(
        self,
    ) -> None:
        events: list[str] = []
        with (
            mock.patch.object(
                issuer.Ed25519PrivateKey, "generate", return_value=object()
            ),
            mock.patch.object(
                issuer, "build_exact_recovery_capsule", return_value={}
            ),
            mock.patch.object(issuer, "verify_exact_clean_candidate"),
            mock.patch.object(
                issuer,
                "prepare_fixed_recovery_capsule",
                side_effect=lambda unused: (
                    events.append("prepare") or ("a" * 64, (1, 2))
                ),
            ),
            mock.patch.object(
                issuer.durable_live_proof_receipt,
                "preflight_anonymous_publication_capability",
                side_effect=lambda **unused: (
                    events.append("preflight")
                    or (_ for _ in ()).throw(
                        issuer.durable_live_proof_receipt.DurableLiveProofReceiptError(
                            "unavailable"
                        )
                    )
                ),
            ),
            mock.patch.object(
                issuer,
                "_remove_exact_staged_capsule_after_pristine_proof",
                side_effect=lambda **unused: events.append("cleanup"),
            ),
            mock.patch.object(
                issuer, "_run_start_authority_pair_mode"
            ) as claim,
            self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "durable_receipt_preflight_failed",
            ),
        ):
            issuer._prepare_preclaim_publish_new_capsule(
                inputs=inputs(), artifacts={}, guard_descriptor=9
            )
        self.assertEqual(events, ["prepare", "preflight", "cleanup"])
        claim.assert_not_called()

    def test_reconcile_retains_linked_capsule_until_pair_reverification(
        self,
    ) -> None:
        verified = proof.VerifiedRecoveryCapsule(
            raw=b"capsule",
            document={},
            install_documents=proof.InstallDocuments(
                b"{}", b"{}", b"{}", "7" * 64
            ),
            rollback_delegation={},
            key_id="7" * 64,
            rollback_nonce="8" * 64,
        )
        metadata = mock.Mock(st_dev=7, st_ino=8, st_nlink=2)
        with (
            mock.patch.object(
                issuer, "_ensure_recovery_capsule_parent", return_value=77
            ),
            mock.patch.object(
                issuer,
                "_read_optional_capsule_member",
                side_effect=(
                    (b"capsule", metadata),
                    (b"capsule", metadata),
                ),
            ),
            mock.patch.object(
                issuer.runner,
                "_verify_recovery_capsule_raw",
                return_value=verified,
            ),
            mock.patch.object(issuer, "_unlink_exact_member") as unlink,
            mock.patch.object(os, "fsync"),
            mock.patch.object(os, "close"),
        ):
            observation = issuer.reconcile_capsule_publication(
                inputs=inputs(), artifacts={}
            )
        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertEqual(observation.state, "linked")
        unlink.assert_not_called()

    def test_partial_staging_cleanup_requires_pristine_proof(self) -> None:
        metadata = mock.Mock(st_dev=7, st_ino=8, st_nlink=1)
        events: list[str] = []
        with (
            mock.patch.object(
                issuer, "_ensure_recovery_capsule_parent", return_value=77
            ),
            mock.patch.object(
                issuer,
                "_read_optional_capsule_member",
                side_effect=(None, (b"", metadata), None),
            ),
            mock.patch.object(
                issuer.runner,
                "_verify_recovery_capsule_raw",
                side_effect=proof.LiveProofError("invalid"),
            ),
            mock.patch.object(
                issuer,
                "_require_pristine_staged_cleanup_state",
                side_effect=lambda **unused: events.append("pristine"),
            ),
            mock.patch.object(issuer, "_require_active_manager_authority"),
            mock.patch.object(
                issuer,
                "_unlink_exact_member",
                side_effect=lambda *unused, **kwargs: events.append("unlink"),
            ),
            mock.patch.object(os, "close"),
        ):
            result = issuer.reconcile_capsule_publication(
                inputs=inputs(), artifacts={}
            )
        self.assertIsNone(result)
        self.assertEqual(events, ["pristine", "unlink"])

    def test_durable_adoption_requires_fresh_absence_and_exact_claim_bindings(
        self,
    ) -> None:
        durable = {
            "candidate_git_commit": COMMIT,
            "candidate_git_tree": TREE,
            "package_manifest_sha256": PACKAGE,
            "controller_runtime_receipt_sha256": RUNTIME,
            "recovery_capsule_sha256": "a" * 64,
            "recovery_reservation_claim_sha256": "b" * 64,
            "install_authority_claim_sha256": "c" * 64,
            "installation_execution_id": INSTALL_EXECUTION,
            "installation_receipt_sha256": "d" * 64,
            "empty_rollback_execution_id": ROLLBACK_EXECUTION,
            "empty_rollback_receipt_sha256": "e" * 64,
        }
        recovery = {
            **durable,
            "exact_resources_absent": True,
            "stores_installed": False,
            "stores_supervisor_installed": False,
            "start_authority_pair_claimed_atomically": True,
        }
        with (
            mock.patch.object(
                issuer, "_supervise_attempt", return_value=(0, b"{}\n", b"")
            ),
            mock.patch.object(
                issuer,
                "_verify_supervised_receipt",
                return_value=("recovery", recovery),
            ),
        ):
            adopted = issuer._adopt_durable_success_after_fresh_absence_recheck(
                inputs=inputs(),
                artifacts={},
                capsule_sha256="a" * 64,
                durable_receipt=durable,
                guard_descriptor=9,
            )
        self.assertEqual(dict(adopted), durable)
        drifted = dict(recovery, install_authority_claim_sha256="f" * 64)
        with (
            mock.patch.object(
                issuer, "_supervise_attempt", return_value=(0, b"{}\n", b"")
            ),
            mock.patch.object(
                issuer,
                "_verify_supervised_receipt",
                return_value=("recovery", drifted),
            ),
            self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError,
                "durable_receipt_adoption_invalid",
            ),
        ):
            issuer._adopt_durable_success_after_fresh_absence_recheck(
                inputs=inputs(),
                artifacts={},
                capsule_sha256="a" * 64,
                durable_receipt=durable,
                guard_descriptor=9,
            )


if __name__ == "__main__":
    unittest.main()
