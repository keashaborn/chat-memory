from __future__ import annotations

import ast
import contextlib
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tools.governed_memory_validation import (
    issue_disposable_installation_live_proof_permit as issuer,
)
from tools.governed_memory_validation import (
    run_disposable_installation_live_proof as proof,
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
    permit = proof.VerifiedPermit(
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
        install_documents=permit.install_documents,
        verified_scope_capability=object(),
        verified_package_capability=object(),
        verified_runtime_capability=object(),
        permit=permit,
    )


class DisposableInstallationLiveProofTests(unittest.TestCase):
    def test_worker_modes_are_closed_and_exact(self) -> None:
        self.assertEqual(
            tuple(item.value for item in proof.WorkerMode),
            (
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
                "run",
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
        self.assertEqual((mode, parsed), ("run", inputs()))
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
        self.assertIn("run_authorized_dormant_store_install(", source)
        self.assertIn("run_authorized_empty_store_rollback(", source)

    def test_exact_hashed_pretty_json_allowed_but_unsafe_json_refused(self) -> None:
        self.assertEqual(
            proof._parse_exact_hashed_json_object(b'{\n  "a": 1\n}\n', "bad"),
            {"a": 1},
        )
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b"[]"):
            with self.assertRaisesRegex(proof.LiveProofError, "bad"):
                proof._parse_exact_hashed_json_object(raw, "bad")

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
            "exact_targets_absent_count": 15,
            "exact_resources_absent_at_terminal_observation": True,
            "terminal_absence_is_continuous_guarantee": False,
            "stores_installed_at_terminal_observation": False,
            "stores_supervisor_installed_at_terminal_observation": False,
            "controller_runtime_capability_reverified": True,
            "source_postgres_read_count": 0,
            "source_postgres_write_count": 0,
            "provider_calls": 0,
            "production_data_read": False,
            "application_services_installed": False,
            "activation_performed": False,
            "issuer_or_host_death_durable_cleanup_proven": False,
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

    def test_issuer_permit_is_deterministic_exact_and_private_key_stays_external(self) -> None:
        private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
        fixed = datetime(2026, 8, 13, 17, 0, tzinfo=timezone.utc)
        first = issuer.build_exact_permit(
            inputs=inputs(), artifacts=artifacts(), private_key=private, issued_at=fixed
        )
        second = issuer.build_exact_permit(
            inputs=inputs(), artifacts=artifacts(), private_key=private, issued_at=fixed
        )
        self.assertEqual(first, second)
        self.assertEqual(set(first), proof._PERMIT_KEYS)
        rendered = issuer._canonical(dict(first))
        self.assertNotIn(b"private", rendered.lower())
        source = Path(proof.__file__).read_text(encoding="utf-8")
        self.assertNotIn("issue_disposable_installation_live_proof_permit", source)
        self.assertNotIn("phase9_disposable_proof_authority", source)

    def test_publish_uses_fixed_exclusive_nofollow_0400_contract(self) -> None:
        source = Path(issuer.__file__).read_text(encoding="utf-8")
        self.assertIn("os.O_EXCL", source)
        self.assertIn('getattr(os, "O_NOFOLLOW", 0)', source)
        self.assertIn("0o400", source)
        self.assertIn("PERMIT_PATH.name", source)
        self.assertIn("remove_exact_permit(permit_inode)", source)
        self.assertIn("os.unlink(runner.PERMIT_PATH.name", source)

    def test_issuer_supervision_is_bounded_and_kills_exact_process_group(self) -> None:
        source = Path(issuer.__file__).read_text(encoding="utf-8")
        self.assertIn("os.setsid()", source)
        self.assertIn("os.killpg(pid, signal.SIGKILL)", source)
        self.assertIn("TOTAL_SUPERVISION_TIMEOUT_SECONDS", source)
        self.assertLess(
            issuer.TOTAL_SUPERVISION_TIMEOUT_SECONDS,
            15 * 60,
        )
        self.assertIn("deadline - time.monotonic()", source)

    def test_signing_frame_reader_returns_at_newline_without_eof(self) -> None:
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b'{"one":1}\n')
            self.assertEqual(
                issuer._read_one_frame(read_fd, 1024, time.monotonic() + 1),
                b'{"one":1}\n',
            )
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_exact_runner_timeout_retries_once_with_same_authority(self) -> None:
        schema = b"schema"
        verified = {
            "live_proof_receipt_schema_sha256": hashlib.sha256(schema).hexdigest(),
            "external_proof_permit_sha256": "a" * 64,
        }
        signing_authority = object()
        with (
            mock.patch.object(os, "geteuid", return_value=0),
            mock.patch.object(issuer, "verify_exact_clean_candidate"),
            mock.patch.object(
                issuer.runner,
                "_load_release",
                return_value=(b"manifest", {proof.LIVE_PROOF_RECEIPT_SCHEMA_RELATIVE: schema}),
            ),
            mock.patch.object(issuer.Ed25519PrivateKey, "generate", return_value=object()),
            mock.patch.object(issuer, "build_exact_permit", return_value={}),
            mock.patch.object(
                issuer, "publish_fixed_permit", return_value=("a" * 64, (1, 2))
            ),
            mock.patch.object(
                issuer, "ExactRollbackSigningAuthority", return_value=signing_authority
            ),
            mock.patch.object(
                issuer,
                "_supervise_attempt",
                side_effect=(
                    issuer.Phase9ProofIssuerError(
                        "phase9_proof_issuer_runner_timeout"
                    ),
                    (0, b"{}", b""),
                ),
            ) as attempts,
            mock.patch.object(proof, "_parse_canonical_object", return_value={}),
            mock.patch.object(proof, "verify_live_proof_receipt", return_value=verified),
            mock.patch.object(issuer, "remove_exact_permit") as remove,
        ):
            result = issuer.issue_and_supervise(inputs())
        self.assertEqual(dict(result), verified)
        self.assertEqual(attempts.call_count, 2)
        self.assertIs(
            attempts.call_args_list[0].kwargs["signing_authority"],
            attempts.call_args_list[1].kwargs["signing_authority"],
        )
        remove.assert_called_once_with((1, 2))

    def test_partial_signing_frame_obeys_deadline(self) -> None:
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b'{"partial":')
            with self.assertRaisesRegex(
                issuer.Phase9ProofIssuerError, "runner_timeout"
            ):
                issuer._read_one_frame(
                    read_fd, 1024, time.monotonic() + 0.01
                )
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_worker_closes_inherited_signing_descriptors(self) -> None:
        source = Path(proof.__file__).read_text(encoding="utf-8")
        self.assertIn(
            "for signing_fd in (ROLLBACK_REQUEST_FD, ROLLBACK_RESPONSE_FD):",
            source,
        )
        self.assertIn("os.close(signing_fd)", source)

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
            mock.patch.object(proof, "_prepare_fixed_substrate"),
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
                proof.WorkerMode.RESUME_ROLLBACK,
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
            "tools/governed_memory_validation/issue_disposable_installation_live_proof_permit.py",
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
