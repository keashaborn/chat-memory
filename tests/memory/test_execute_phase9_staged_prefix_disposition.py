from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from tools.governed_memory_validation import (
    execute_phase9_staged_prefix_disposition as subject,
)
from tools.governed_memory_validation import staged_prefix_disposition as disposition


def permit(**changes: object) -> dict[str, object]:
    unsigned: dict[str, object] = {
        "schema_version": subject.PERMIT_SCHEMA,
        "result": subject.PERMIT_RESULT,
        "candidate_git_commit": "a" * 40,
        "candidate_git_tree": "b" * 40,
        "base_candidate_git_commit": subject.BASE_CANDIDATE_COMMIT,
        "base_candidate_git_tree": subject.BASE_CANDIDATE_TREE,
        "package_manifest_sha256": subject.PACKAGE_MANIFEST_SHA256,
        "controller_runtime_receipt_sha256": (
            subject.CONTROLLER_RUNTIME_RECEIPT_SHA256
        ),
        "contract_sha256": subject.CONTRACT_SHA256,
        "predecessor_attempt_identity_sha256": (
            subject.PREDECESSOR_ATTEMPT_IDENTITY_SHA256
        ),
        "successor_attempt_identity_sha256": (
            subject.SUCCESSOR_ATTEMPT_IDENTITY_SHA256
        ),
        "authorization_text_sha256": subject.AUTHORIZATION_TEXT_SHA256,
        "thread_id": subject.THREAD_ID,
        "source_blobs": {
            path: f"{index + 1:x}" * 40
            for index, path in enumerate(subject._SOURCE_PATHS)
        },
        "authorized_action": (
            "execute_staged_prefix_disposition_and_disposable_live_proof_only"
        ),
        "activation_performed": False,
        "provider_calls": 0,
        "production_data_read": False,
        "deletion_performed": False,
    }
    unsigned.update(changes)
    return {
        **unsigned,
        "permit_sha256": subject._sha(subject._canonical(unsigned)),
    }


class PreimportBoundaryTests(unittest.TestCase):
    def test_exact_manager_runtime_and_lease_lineage_is_required(self) -> None:
        manager_pid = 4242
        environment = {
            subject._PREIMPORT_MANAGER_PID_KEY: str(manager_pid),
            "CHAT_MEMORY_LEASE_ID": "lease-1",
            "CODEX_TASK_ID": "task-1",
            "CODEX_THREAD_ID": "thread-1",
        }
        manager_path = str(
            subject._REPOSITORY_ROOT / subject._PREIMPORT_MANAGER_RELATIVE
        )
        command_line = b"\0".join(
            value.encode("utf-8")
            for value in (
                subject._PREIMPORT_CONTROLLER_PYTHON,
                "-I",
                "-B",
                manager_path,
            )
        ) + b"\0"
        self.assertTrue(
            subject._preimport_manager_lineage_valid(
                environment,
                observed_parent_pid=manager_pid,
                observed_parent_executable=subject._PREIMPORT_CONTROLLER_PYTHON,
                observed_parent_command_line=command_line,
            )
        )
        for changes in (
            {"observed_parent_pid": manager_pid + 1},
            {"observed_parent_executable": "/usr/bin/python3"},
            {"observed_parent_command_line": command_line + b"foreign\0"},
        ):
            values = {
                "observed_parent_pid": manager_pid,
                "observed_parent_executable": (
                    subject._PREIMPORT_CONTROLLER_PYTHON
                ),
                "observed_parent_command_line": command_line,
                **changes,
            }
            with self.subTest(changes=changes):
                self.assertFalse(
                    subject._preimport_manager_lineage_valid(
                        environment, **values
                    )
                )
        missing_lease = dict(environment)
        del missing_lease["CHAT_MEMORY_LEASE_ID"]
        self.assertFalse(
            subject._preimport_manager_lineage_valid(
                missing_lease,
                observed_parent_pid=manager_pid,
                observed_parent_executable=subject._PREIMPORT_CONTROLLER_PYTHON,
                observed_parent_command_line=command_line,
            )
        )

    def test_runtime_and_manager_gates_precede_repository_imports(self) -> None:
        source = Path(subject.__file__).read_text(encoding="utf-8")
        later_import = source.index("from collections.abc import")
        for marker in (
            "sys.executable == _PREIMPORT_CONTROLLER_PYTHON",
            "not _preimport_manager_lineage_valid()",
            "/proc/{manager_pid}/exe",
            "/proc/{manager_pid}/cmdline",
        ):
            with self.subTest(marker=marker):
                self.assertLess(source.index(marker), later_import)

    def test_000004_pins_are_exactly_sealed(self) -> None:
        self.assertEqual(
            subject._PREIMPORT_CONTROLLER_RUNTIME_RECEIPT_SHA256,
            "ed0b3518484eec292f35f8b996bccefd01e13038105634016b4253a8c2a732a7",
        )
        self.assertEqual(
            subject.CONTROLLER_RUNTIME_RECEIPT_SHA256,
            "ed0b3518484eec292f35f8b996bccefd01e13038105634016b4253a8c2a732a7",
        )
        self.assertEqual(
            subject.CONTRACT_SHA256,
            "daf64a4a6a17d6666d408f0beb216f44ba7d755efab4b43835ec0c7e3ad11f15",
        )
        self.assertEqual(
            subject.SUCCESSOR_ATTEMPT_IDENTITY_SHA256,
            "7d36e9326e1b333b4203d167f776ef75759ff295f943f79ae5838b068c3b8ba5",
        )
        self.assertEqual(
            subject.PACKAGE_MANIFEST_SHA256,
            "634669dbca4f2ccfed929951bcdd0d555d19e53f9b736ee21b42217e8e8cd629",
        )
        self.assertTrue(subject._bindings_sealed())
        self.assertEqual(
            subject.EXPECTED_CANDIDATE_REF,
            "refs/tags/governed-memory-phase9j-pre-effect-disposition-000004",
        )


class ContractInstallationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state_root = Path(self.temporary.name) / "state"
        self.state_root.mkdir(mode=0o700)
        self.contract_path = self.state_root / "contracts" / "review.json"
        self.raw = b'{"review":"exact"}'

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def install(self, raw: bytes | None = None) -> None:
        subject._install_create_once(
            state_root=self.state_root,
            contract_path=self.contract_path,
            raw=self.raw if raw is None else raw,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
        )

    def patch_identity(self) -> mock._patch:
        return mock.patch.multiple(
            subject,
            ROOT_UID=os.getuid(),
            ROOT_GID=os.getgid(),
            CONTRACT_STAGING_NAME=".review.json.publishing",
        )

    def test_create_once_is_0400_and_exact_replay_preserves_inode(self) -> None:
        with self.patch_identity():
            self.install()
            identity = self.contract_path.stat()
            self.install()
            self.assertEqual(self.contract_path.read_bytes(), self.raw)
            self.assertEqual(stat.S_IMODE(self.contract_path.stat().st_mode), 0o400)
            self.assertEqual(self.contract_path.stat().st_ino, identity.st_ino)
            with self.assertRaisesRegex(
                subject.Phase9StagedPrefixDispositionEntrypointError,
                "contract_replay_mismatch",
            ):
                self.install(b'{"review":"different"}')
            self.assertEqual(self.contract_path.read_bytes(), self.raw)
            self.assertEqual(self.contract_path.stat().st_ino, identity.st_ino)

    def test_symlink_contract_root_is_refused_without_external_write(self) -> None:
        target = Path(self.temporary.name) / "outside"
        target.mkdir(mode=0o700)
        self.contract_path.parent.symlink_to(target, target_is_directory=True)
        with self.patch_identity(), self.assertRaises(
            subject.Phase9StagedPrefixDispositionEntrypointError
        ):
            self.install()
        self.assertEqual(tuple(target.iterdir()), ())

    def test_complete_staging_reconciles_but_foreign_staging_is_preserved(self) -> None:
        with self.patch_identity():
            self.contract_path.parent.mkdir(mode=0o700)
            staging = self.contract_path.parent / ".review.json.publishing"
            staging.write_bytes(self.raw)
            staging.chmod(0o400)
            inode = staging.stat().st_ino
            self.install()
            self.assertFalse(staging.exists())
            self.assertEqual(self.contract_path.stat().st_ino, inode)

            self.contract_path.unlink()
            staging.write_bytes(b"foreign")
            staging.chmod(0o400)
            foreign_inode = staging.stat().st_ino
            with self.assertRaisesRegex(
                subject.Phase9StagedPrefixDispositionEntrypointError,
                "contract_publication_conflict",
            ):
                self.install()
            self.assertEqual(staging.read_bytes(), b"foreign")
            self.assertEqual(staging.stat().st_ino, foreign_inode)
            self.assertFalse(self.contract_path.exists())


class PermitAndCandidateTests(unittest.TestCase):
    def test_exact_permit_is_accepted_and_substitution_is_refused(self) -> None:
        raw = subject._canonical(permit())
        with mock.patch.object(subject, "_read_root_file", return_value=raw):
            observed = subject._read_and_verify_permit()
        self.assertEqual(observed["candidate_git_commit"], "a" * 40)

        substituted = permit(authorized_action="activate")
        with (
            mock.patch.object(
                subject,
                "_read_root_file",
                return_value=subject._canonical(substituted),
            ),
            self.assertRaises(
                subject.Phase9StagedPrefixDispositionEntrypointError
            ),
        ):
            subject._read_and_verify_permit()

    @staticmethod
    def git_outputs(
        document: dict[str, object],
        *,
        status: bytes = b"",
        tag_commit: str | None = None,
        blob_drift: bool = False,
    ):
        blobs = document["source_blobs"]
        assert isinstance(blobs, dict)

        def run(
            *arguments: str,
            maximum: int = subject.MAX_GIT_OUTPUT_BYTES,
        ) -> bytes:
            del maximum
            if arguments == ("rev-parse", "--show-toplevel"):
                return (str(subject._REPOSITORY_ROOT) + "\n").encode()
            if arguments == (
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ):
                return status
            if arguments[:4] == (
                "ls-files",
                "--error-unmatch",
                "--stage",
                "--",
            ):
                return b"tracked\n"
            if arguments == ("rev-parse", "--verify", "HEAD^{commit}"):
                return (str(document["candidate_git_commit"]) + "\n").encode()
            if arguments == ("rev-parse", "--verify", "HEAD^{tree}"):
                return (str(document["candidate_git_tree"]) + "\n").encode()
            if arguments == (
                "rev-parse",
                "--verify",
                subject.EXPECTED_CANDIDATE_REF + "^{commit}",
            ):
                return (
                    (tag_commit or str(document["candidate_git_commit"])) + "\n"
                ).encode()
            if arguments == (
                "rev-parse",
                "--verify",
                subject.EXPECTED_CANDIDATE_REF + "^{tree}",
            ):
                return (str(document["candidate_git_tree"]) + "\n").encode()
            for relative in subject._SOURCE_PATHS:
                value = str(blobs[relative])
                if arguments == ("rev-parse", "HEAD:" + relative):
                    return (value + "\n").encode()
                if arguments == (
                    "hash-object",
                    str(subject._REPOSITORY_ROOT / relative),
                ):
                    if blob_drift and relative == subject._CONTROLLER_RELATIVE:
                        value = "f" * 40
                    return (value + "\n").encode()
            raise AssertionError(arguments)

        return run

    def test_candidate_requires_clean_exact_fixed_tag_and_source_blobs(self) -> None:
        document = permit()
        with mock.patch.object(
            subject, "_git", side_effect=self.git_outputs(document)
        ):
            identity = subject._verified_candidate_identity(document)
        self.assertEqual(identity.commit, document["candidate_git_commit"])

        cases = (
            {"status": b" M tracked.py\n"},
            {"tag_commit": "f" * 40},
            {"blob_drift": True},
        )
        for changes in cases:
            with (
                self.subTest(changes=changes),
                mock.patch.object(
                    subject,
                    "_git",
                    side_effect=self.git_outputs(document, **changes),
                ),
                self.assertRaises(
                    subject.Phase9StagedPrefixDispositionEntrypointError
                ),
            ):
                subject._verified_candidate_identity(document)


class ExecutionTests(unittest.TestCase):
    def test_permit_and_candidate_precede_import_and_candidate_is_rechecked(self) -> None:
        document = permit()
        identity = subject.CandidateIdentity(
            "a" * 40,
            "b" * 40,
            tuple(document["source_blobs"].items()),
        )
        expectation = disposition.ReviewedStagedPrefixExpectation(
            contract_sha256=subject.CONTRACT_SHA256,
            disposition_id=subject.DISPOSITION_ID,
            old_tag_ref=subject.FAILED_TAG_REF,
            old_tag_commit=subject.FAILED_TAG_COMMIT,
            old_tag_tree=subject.FAILED_TAG_TREE,
            failed_package_manifest_sha256=(
                subject.FAILED_PACKAGE_MANIFEST_SHA256
            ),
            failed_controller_runtime_receipt_sha256=(
                subject.FAILED_CONTROLLER_RUNTIME_RECEIPT_SHA256
            ),
            corrected_generation=subject.CORRECTED_GENERATION,
            corrected_package_manifest_sha256=subject.PACKAGE_MANIFEST_SHA256,
            corrected_controller_runtime_receipt_sha256=(
                subject.CONTROLLER_RUNTIME_RECEIPT_SHA256
            ),
        )
        receipt = {
            "result": disposition.RESULT,
            "contract_sha256": subject.CONTRACT_SHA256,
            "tombstone_sha256": "c" * 64,
        }
        events: list[str] = []

        def reverify(*unused: object) -> None:
            events.append("reverify")

        with (
            mock.patch.multiple(
                subject,
                _ISOLATED_RUNTIME_AT_START=True,
                _DONT_WRITE_BYTECODE_AT_START=True,
            ),
            mock.patch.object(subject.sys, "platform", "linux"),
            mock.patch.object(
                subject.sys,
                "executable",
                subject._PREIMPORT_CONTROLLER_PYTHON,
            ),
            mock.patch.object(
                subject, "_preimport_manager_lineage_valid", return_value=True
            ),
            mock.patch.object(subject.os, "geteuid", return_value=0),
            mock.patch.object(subject.os, "getegid", return_value=0),
            mock.patch.object(subject, "_bindings_sealed", return_value=True),
            mock.patch.object(
                subject,
                "_read_and_verify_permit",
                side_effect=lambda: events.append("permit") or document,
            ),
            mock.patch.object(
                subject,
                "_verified_candidate_identity",
                side_effect=lambda unused: events.append("candidate") or identity,
            ),
            mock.patch.object(
                subject,
                "_load_disposition",
                side_effect=lambda: events.append("import") or disposition,
            ),
            mock.patch.object(subject, "_expectation", return_value=expectation),
            mock.patch.object(subject, "_read_repository_contract", return_value=b"contract"),
            mock.patch.object(subject, "_reverify_candidate", side_effect=reverify),
            mock.patch.object(
                subject,
                "_install_create_once",
                side_effect=lambda **unused: events.append("install"),
            ),
            mock.patch.object(
                disposition,
                "execute_staged_prefix_disposition",
                side_effect=lambda *unused: events.append("execute") or receipt,
            ),
        ):
            observed = subject.execute_exact_production_disposition()
        self.assertIs(observed, receipt)
        self.assertEqual(
            events,
            [
                "permit",
                "candidate",
                "import",
                "reverify",
                "install",
                "reverify",
                "execute",
                "reverify",
            ],
        )

    def test_import_callable_still_requires_runtime_manager_and_root(self) -> None:
        cases = (
            {
                "executable": "/usr/bin/python3",
                "lineage": True,
                "uid": 0,
                "code": "runtime_isolation_required",
            },
            {
                "executable": subject._PREIMPORT_CONTROLLER_PYTHON,
                "lineage": False,
                "uid": 0,
                "code": "manager_lineage_required",
            },
            {
                "executable": subject._PREIMPORT_CONTROLLER_PYTHON,
                "lineage": True,
                "uid": 1000,
                "code": "root_required",
            },
        )
        for case in cases:
            with (
                self.subTest(case=case),
                mock.patch.multiple(
                    subject,
                    _ISOLATED_RUNTIME_AT_START=True,
                    _DONT_WRITE_BYTECODE_AT_START=True,
                ),
                mock.patch.object(subject.sys, "platform", "linux"),
                mock.patch.object(
                    subject.sys, "executable", str(case["executable"])
                ),
                mock.patch.object(
                    subject,
                    "_preimport_manager_lineage_valid",
                    return_value=bool(case["lineage"]),
                ),
                mock.patch.object(
                    subject.os, "geteuid", return_value=int(case["uid"])
                ),
                mock.patch.object(
                    subject.os, "getegid", return_value=int(case["uid"])
                ),
                mock.patch.object(subject, "_read_and_verify_permit") as read,
                self.assertRaisesRegex(
                    subject.Phase9StagedPrefixDispositionEntrypointError,
                    str(case["code"]),
                ),
            ):
                subject.execute_exact_production_disposition()
            read.assert_not_called()

    def test_no_operational_arguments(self) -> None:
        with self.assertRaisesRegex(
            subject.Phase9StagedPrefixDispositionEntrypointError,
            "arguments_refused",
        ):
            subject.main(("--contract", "/tmp/foreign"))


if __name__ == "__main__":
    unittest.main()
