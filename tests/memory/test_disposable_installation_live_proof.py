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


def inputs() -> proof.ProofInputs:
    return proof.ProofInputs(
        candidate_git_commit=COMMIT,
        candidate_git_tree=TREE,
        package_manifest_sha256=PACKAGE,
        controller_runtime_receipt_sha256=RUNTIME,
    )


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
    def _issuer_guard_mocks(
        self,
    ) -> tuple[mock._patch, mock._patch, mock._patch, mock._patch]:
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
            mock.patch.object(issuer, "_ensure_live_proof_guard_parent"),
            mock.patch.object(issuer, "GlobalExecutionLock", return_value=guard),
            mock.patch.object(
                issuer,
                "reconcile_capsule_publication",
                return_value=(capsule, "a" * 64, (1, 2)),
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

        def invalid_authority(unused: object) -> object:
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
        self.assertEqual(result, (verified, hashlib.sha256(b"capsule").hexdigest(), (7, 8)))
        self.assertEqual(verify.call_count, 2)

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
                return_value=["0", "1", "2", "3", "8", "9", "10", "77"],
            ) as listdir,
            mock.patch.object(os, "close", side_effect=close_descriptor),
        ):
            issuer._close_unintended_runner_descriptors()
        listdir.assert_called_once_with("/proc/self/fd")
        self.assertEqual(closed, [3, 8, 10, 77])

    def test_runner_process_seal_installs_only_inert_stdin_outputs_and_guard(
        self,
    ) -> None:
        with (
            mock.patch.object(os, "open", return_value=13) as open_descriptor,
            mock.patch.object(os, "dup2") as duplicate,
            mock.patch.object(os, "set_inheritable") as set_inheritable,
            mock.patch.object(
                issuer, "_close_unintended_runner_descriptors"
            ) as close_unintended,
        ):
            issuer._seal_exact_runner_process(
                guard_descriptor=20,
                stdout_descriptor=21,
                stderr_descriptor=22,
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
            ],
        )
        close_unintended.assert_called_once_with()

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
                    issuer.publish_fixed_recovery_capsule({"recovery_capsule": "fixed"})
            self.assertFalse(capsule_path.exists())

            temporary_path = capsule_path.with_name(
                capsule_path.name + issuer.RECOVERY_CAPSULE_TEMP_SUFFIX
            )
            temporary_path.write_bytes(b"preexisting")
            before = temporary_path.stat()
            with (
                mock.patch.object(issuer.runner, "RECOVERY_CAPSULE_PATH", capsule_path),
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
                    issuer.publish_fixed_recovery_capsule({"recovery_capsule": "fixed"})
            after = temporary_path.stat()
            self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
            self.assertEqual(temporary_path.read_bytes(), b"preexisting")

    def test_recovery_capsule_publication_fstat_failure_unlinks_created_name(
        self,
    ) -> None:
        metadata = mock.Mock(st_dev=7, st_ino=8)
        with (
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
                issuer.publish_fixed_recovery_capsule({"recovery_capsule": "fixed"})
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
                "recovery_capsule_publication_failed",
            ):
                issuer.publish_fixed_recovery_capsule({"recovery_capsule": "fixed"})
        unlink.assert_called_once_with(
            proof.RECOVERY_CAPSULE_PATH.name
            + issuer.RECOVERY_CAPSULE_TEMP_SUFFIX,
            dir_fd=77,
        )

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
        with mock.patch.object(
            proof,
            "_dispatch_worker",
            return_value={"worker": "complete"},
        ):
            pid, descriptor = proof._fork_worker(
                proof.WorkerMode.INSTALL, context()
            )
            result = proof._wait_worker(pid, descriptor)
        self.assertEqual(dict(result), {"worker": "complete"})

    def test_issuer_refuses_before_effects_without_durable_recovery_authority(self) -> None:
        with (
            mock.patch.object(issuer, "_require_closed_issuer_runtime"),
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
            mock.patch.object(issuer, "publish_fixed_recovery_capsule") as publish,
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

    def test_unexpected_supervision_error_kills_and_reaps_exact_group(self) -> None:
        descriptors = [os.open(os.devnull, os.O_RDONLY) for unused in range(2)]
        pid = 424242
        with (
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
                side_effect=(missing_group, missing_group, missing_group),
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
        self.assertEqual(kill_group.call_count, 3)
        kill_pid.assert_called_once_with(pid, signal.SIGKILL)
        self.assertEqual(waitpid.call_count, 2)

    def test_exact_runner_timeout_invokes_recovery_only_once(self) -> None:
        schema = b"schema"
        recovered = {
            "candidate_git_commit": COMMIT,
            "candidate_git_tree": TREE,
            "package_manifest_sha256": PACKAGE,
            "controller_runtime_receipt_sha256": RUNTIME,
            "recovery_capsule_sha256": "a" * 64,
        }
        runtime, guard_parent, guard_lock, reconcile = self._issuer_guard_mocks()
        with (
            runtime,
            guard_parent,
            guard_lock,
            reconcile,
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
            mock.patch.object(issuer.Ed25519PrivateKey, "generate", return_value=object()),
            mock.patch.object(issuer, "build_exact_recovery_capsule", return_value={}),
            mock.patch.object(
                issuer, "publish_fixed_recovery_capsule", return_value=("a" * 64, (1, 2))
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
        runtime, guard_parent, guard_lock, reconcile = self._issuer_guard_mocks()
        with (
            runtime,
            guard_parent,
            guard_lock,
            reconcile,
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
                issuer.Ed25519PrivateKey,
                "generate",
                return_value=object(),
            ),
            mock.patch.object(issuer, "build_exact_recovery_capsule", return_value={}),
            mock.patch.object(
                issuer,
                "publish_fixed_recovery_capsule",
                return_value=("a" * 64, (1, 2)),
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
        runtime, guard_parent, guard_lock, reconcile = self._issuer_guard_mocks()
        with (
            runtime,
            guard_parent,
            guard_lock,
            reconcile,
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
                issuer.Ed25519PrivateKey,
                "generate",
                return_value=object(),
            ),
            mock.patch.object(issuer, "build_exact_recovery_capsule", return_value={}),
            mock.patch.object(
                issuer,
                "publish_fixed_recovery_capsule",
                return_value=("a" * 64, (1, 2)),
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
            mock.patch.object(proof, "_claim_recovery_reservation_before_install"),
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
                pid=pid,
                read_fd=descriptor,
                expected_execution_id=INSTALL_EXECUTION,
                allowed_existing_directories=frozenset(),
                journal_name="journal.jsonl",
                step_id=proof.INSTALL_KILL_STEP,
            )
        kill.assert_called_once_with(pid, signal.SIGKILL)
        self.assertEqual(waitpid.call_count, 2)
        close.assert_called_once_with(descriptor)

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

        def fork(mode: proof.WorkerMode, unused: object) -> tuple[int, int]:
            modes.append(mode)
            return len(modes), len(modes) + 10

        with (
            mock.patch.object(
                proof, "_verify_host_clock_synchronized", return_value=True
            ),
            mock.patch.object(proof, "_prepare_fixed_substrate"),
            mock.patch.object(proof, "_claim_recovery_reservation_before_install"),
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
                proof.WorkerMode.INSTALL,
                proof.WorkerMode.ROLLBACK,
                proof.WorkerMode.VERIFY_ABSENCE,
            ],
        )

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

    def test_issuer_has_direct_isolated_runtime_invocation_surface(self) -> None:
        completed = subprocess.run(
            (sys.executable, "-I", "-B", str(Path(issuer.__file__)), "--help"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertIn(b"--package-manifest-sha256", completed.stdout)

    def test_issuer_execs_runner_with_isolation_and_no_bytecode_writes(self) -> None:
        arguments = issuer._runner_argv(inputs())
        self.assertEqual(arguments[:3], ("python", "-I", "-B"))
        source = Path(proof.__file__).read_text(encoding="utf-8")
        self.assertIn("phase9_live_proof_bytecode_writes_not_disabled", source)


if __name__ == "__main__":
    unittest.main()
