from __future__ import annotations

import os
import errno
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from tools.governed_memory_validation import execute_phase9_pre_effect_disposition as subject
from tools.governed_memory_validation import pre_effect_disposition as disposition


def permit(**changes: object) -> dict[str, object]:
    unsigned: dict[str, object] = {
        "schema_version": subject.PERMIT_SCHEMA,
        "result": subject.PERMIT_RESULT,
        "candidate_git_commit": "a" * 40,
        "candidate_git_tree": "b" * 40,
        "base_candidate_git_commit": subject.BASE_CANDIDATE_COMMIT,
        "base_candidate_git_tree": subject.BASE_CANDIDATE_TREE,
        "package_manifest_sha256": subject.PACKAGE_MANIFEST_SHA256,
        "controller_runtime_receipt_sha256": subject.CONTROLLER_RUNTIME_RECEIPT_SHA256,
        "contract_sha256": subject.CONTRACT_SHA256,
        "predecessor_attempt_identity_sha256": subject.PREDECESSOR_ATTEMPT_IDENTITY_SHA256,
        "successor_attempt_identity_sha256": subject.SUCCESSOR_ATTEMPT_IDENTITY_SHA256,
        "authorization_text_sha256": subject.AUTHORIZATION_TEXT_SHA256,
        "thread_id": subject.THREAD_ID,
        "source_blobs": {
            path: f"{index + 1:x}" * 40
            for index, path in enumerate(subject._SOURCE_PATHS)
        },
        "authorized_action": (
            "execute_pre_effect_disposition_and_disposable_live_proof_only"
        ),
        "activation_performed": False,
        "provider_calls": 0,
        "production_data_read": False,
        "deletion_performed": False,
    }
    unsigned.update(changes)
    return {**unsigned, "permit_sha256": subject._sha(subject._canonical(unsigned))}


class PreimportLineageTests(unittest.TestCase):
    def test_exact_r7_manager_and_lease_lineage_is_required(self) -> None:
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
        cases = (
            {"observed_parent_pid": manager_pid + 1},
            {"observed_parent_executable": "/usr/bin/python3"},
            {"observed_parent_command_line": command_line + b"foreign\0"},
        )
        for changes in cases:
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
        del missing_lease["CODEX_THREAD_ID"]
        self.assertFalse(
            subject._preimport_manager_lineage_valid(
                missing_lease,
                observed_parent_pid=manager_pid,
                observed_parent_executable=subject._PREIMPORT_CONTROLLER_PYTHON,
                observed_parent_command_line=command_line,
            )
        )

    def test_runtime_and_manager_lineage_gates_precede_later_imports(self) -> None:
        source = Path(subject.__file__).read_text(encoding="utf-8")
        later_import = source.index("from collections.abc import")
        for marker in (
            "sys.executable == _PREIMPORT_CONTROLLER_PYTHON",
            "not _preimport_manager_lineage_valid()",
            "/proc/{manager_pid}/exe",
            "/proc/{manager_pid}/cmdline",
            "phase9_pre_effect_disposition_manager_lineage_required",
        ):
            with self.subTest(marker=marker):
                self.assertLess(source.index(marker), later_import)


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
            expected_uid=os.getuid(), expected_gid=os.getgid(),
        )

    def test_create_once_and_exact_replay_only(self) -> None:
        with mock.patch.multiple(
            subject,
            ROOT_UID=os.getuid(), ROOT_GID=os.getgid(),
            CONTRACT_STAGING_NAME=".review.json.publishing",
        ):
            self.install()
            identity = self.contract_path.stat()
            self.install()
            self.assertEqual(self.contract_path.read_bytes(), self.raw)
            self.assertEqual(self.contract_path.stat().st_mode & 0o777, 0o400)
            self.assertEqual(self.contract_path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(self.contract_path.stat().st_ino, identity.st_ino)
            with self.assertRaisesRegex(
                subject.Phase9PreEffectDispositionEntrypointError,
                "phase9_pre_effect_disposition_contract_replay_mismatch",
            ):
                self.install(b'{"review":"different"}')

    def test_symlink_contract_root_is_refused(self) -> None:
        target = Path(self.temporary.name) / "outside"
        target.mkdir(mode=0o700)
        self.contract_path.parent.symlink_to(target, target_is_directory=True)
        with (
            mock.patch.multiple(
                subject,
                ROOT_UID=os.getuid(), ROOT_GID=os.getgid(),
                CONTRACT_STAGING_NAME=".review.json.publishing",
            ),
            self.assertRaises(subject.Phase9PreEffectDispositionEntrypointError),
        ):
            self.install()
        self.assertEqual(tuple(target.iterdir()), ())

    def test_complete_staging_is_reconciled_and_partial_staging_is_untouched(self) -> None:
        staging_name = ".review.json.publishing"
        with mock.patch.multiple(
            subject,
            ROOT_UID=os.getuid(), ROOT_GID=os.getgid(),
            CONTRACT_STAGING_NAME=staging_name,
        ):
            self.contract_path.parent.mkdir(mode=0o700)
            staging = self.contract_path.parent / staging_name
            staging.write_bytes(self.raw)
            staging.chmod(0o400)
            inode = staging.stat().st_ino
            self.install()
            self.assertFalse(staging.exists())
            self.assertEqual(self.contract_path.stat().st_ino, inode)
            self.contract_path.unlink()
            staging.write_bytes(self.raw[:7])
            staging.chmod(0o400)
            partial_inode = staging.stat().st_ino
            with self.assertRaisesRegex(
                subject.Phase9PreEffectDispositionEntrypointError,
                "phase9_pre_effect_disposition_contract_publication_conflict",
            ):
                self.install()
            self.assertEqual(staging.read_bytes(), self.raw[:7])
            self.assertEqual(staging.stat().st_ino, partial_inode)
            self.assertFalse(self.contract_path.exists())

    def test_foreign_final_is_refused_and_not_replaced(self) -> None:
        with mock.patch.multiple(
            subject,
            ROOT_UID=os.getuid(), ROOT_GID=os.getgid(),
            CONTRACT_STAGING_NAME=".review.json.publishing",
        ):
            self.contract_path.parent.mkdir(mode=0o700)
            self.contract_path.write_bytes(b"foreign")
            self.contract_path.chmod(0o400)
            inode = self.contract_path.stat().st_ino
            with self.assertRaisesRegex(
                subject.Phase9PreEffectDispositionEntrypointError,
                "phase9_pre_effect_disposition_contract_replay_mismatch",
            ):
                self.install()
            self.assertEqual(self.contract_path.read_bytes(), b"foreign")
            self.assertEqual(self.contract_path.stat().st_ino, inode)

    def test_foreign_contract_staging_is_refused_and_not_unlinked(self) -> None:
        staging_name = ".review.json.publishing"
        with mock.patch.multiple(
            subject,
            ROOT_UID=os.getuid(), ROOT_GID=os.getgid(),
            CONTRACT_STAGING_NAME=staging_name,
        ):
            self.contract_path.parent.mkdir(mode=0o700)
            staging = self.contract_path.parent / staging_name
            staging.write_bytes(b"foreign")
            staging.chmod(0o400)
            inode = staging.stat().st_ino
            with self.assertRaisesRegex(
                subject.Phase9PreEffectDispositionEntrypointError,
                "phase9_pre_effect_disposition_contract_publication_conflict",
            ):
                self.install()
            self.assertEqual(staging.read_bytes(), b"foreign")
            self.assertEqual(staging.stat().st_ino, inode)
            self.assertFalse(self.contract_path.exists())

    def test_short_writes_complete_before_contract_publication(self) -> None:
        original_write = os.write
        calls = 0

        def short_write(descriptor: int, value: object) -> int:
            nonlocal calls
            calls += 1
            view = memoryview(value)
            return original_write(descriptor, view[: max(1, len(view) // 3)])

        with (
            mock.patch.multiple(
                subject,
                ROOT_UID=os.getuid(), ROOT_GID=os.getgid(),
                CONTRACT_STAGING_NAME=".review.json.publishing",
            ),
            mock.patch.object(subject.os, "write", side_effect=short_write),
        ):
            self.install()
        self.assertGreater(calls, 1)
        self.assertEqual(self.contract_path.read_bytes(), self.raw)

    def test_enospc_preserves_partial_contract_staging_without_final(self) -> None:
        original_write = os.write
        calls = 0

        def fail_after_prefix(descriptor: int, value: object) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                return original_write(descriptor, memoryview(value)[:5])
            raise OSError(errno.ENOSPC, "full")

        with (
            mock.patch.multiple(
                subject,
                ROOT_UID=os.getuid(), ROOT_GID=os.getgid(),
                CONTRACT_STAGING_NAME=".review.json.publishing",
            ),
            mock.patch.object(
                subject.os, "write", side_effect=fail_after_prefix
            ),
            self.assertRaises(
                subject.Phase9PreEffectDispositionEntrypointError
            ),
        ):
            self.install()
        staging = self.contract_path.parent / ".review.json.publishing"
        self.assertEqual(staging.read_bytes(), self.raw[:5])
        self.assertFalse(self.contract_path.exists())

    def test_fsync_failure_leaves_complete_reconcilable_contract_staging(self) -> None:
        original_fsync = os.fsync
        failed = False

        def fail_first(descriptor: int) -> None:
            nonlocal failed
            if not failed and stat.S_ISREG(os.fstat(descriptor).st_mode):
                failed = True
                raise OSError(errno.EIO, "fsync")
            original_fsync(descriptor)

        with mock.patch.multiple(
            subject,
            ROOT_UID=os.getuid(), ROOT_GID=os.getgid(),
            CONTRACT_STAGING_NAME=".review.json.publishing",
        ):
            with mock.patch.object(subject.os, "fsync", side_effect=fail_first):
                with self.assertRaises(
                    subject.Phase9PreEffectDispositionEntrypointError
                ):
                    self.install()
                staging = self.contract_path.parent / ".review.json.publishing"
                self.assertEqual(staging.read_bytes(), self.raw)
                self.assertFalse(self.contract_path.exists())
            self.install()
            self.assertFalse(staging.exists())
            self.assertEqual(self.contract_path.read_bytes(), self.raw)

    def test_close_failure_leaves_complete_reconcilable_contract_staging(self) -> None:
        original_close = os.close
        failed = False

        def fail_regular_once(descriptor: int) -> None:
            nonlocal failed
            metadata = os.fstat(descriptor)
            original_close(descriptor)
            if stat.S_ISREG(metadata.st_mode) and not failed:
                failed = True
                raise OSError(errno.EIO, "close")

        with mock.patch.multiple(
            subject,
            ROOT_UID=os.getuid(), ROOT_GID=os.getgid(),
            CONTRACT_STAGING_NAME=".review.json.publishing",
        ):
            with mock.patch.object(subject.os, "close", side_effect=fail_regular_once):
                with self.assertRaises(
                    subject.Phase9PreEffectDispositionEntrypointError
                ):
                    self.install()
                staging = self.contract_path.parent / ".review.json.publishing"
                self.assertEqual(staging.read_bytes(), self.raw)
                self.assertFalse(self.contract_path.exists())
            self.install()
            self.assertFalse(staging.exists())
            self.assertEqual(self.contract_path.read_bytes(), self.raw)


class PermitTests(unittest.TestCase):
    def test_exact_permit_is_accepted(self) -> None:
        raw = subject._canonical(permit())
        with mock.patch.object(subject, "_read_root_file", return_value=raw):
            observed = subject._read_and_verify_permit()
        self.assertEqual(observed["candidate_git_commit"], "a" * 40)

    def test_missing_malformed_or_substituted_permit_is_refused(self) -> None:
        substituted = permit(authorized_action="activate")
        cases = (
            None,
            b'{"malformed":',
            subject._canonical(substituted),
        )
        for raw in cases:
            with self.subTest(raw=raw):
                if raw is None:
                    effect = subject.Phase9PreEffectDispositionEntrypointError(
                        "phase9_pre_effect_disposition_permit_invalid"
                    )
                    patcher = mock.patch.object(subject, "_read_root_file", side_effect=effect)
                else:
                    patcher = mock.patch.object(subject, "_read_root_file", return_value=raw)
                with patcher, self.assertRaises(subject.Phase9PreEffectDispositionEntrypointError):
                    subject._read_and_verify_permit()

    def test_bad_permit_mode_owner_or_link_count_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "permit.json"
            path.write_bytes(subject._canonical(permit()))
            modes = (0o600, 0o644)
            for mode in modes:
                path.chmod(mode)
                with (
                    self.subTest(mode=mode),
                    mock.patch.multiple(subject, PERMIT_PATH=path, ROOT_UID=os.getuid(), ROOT_GID=os.getgid()),
                    self.assertRaises(subject.Phase9PreEffectDispositionEntrypointError),
                ):
                    subject._read_and_verify_permit()
            path.chmod(0o400)
            with (
                mock.patch.multiple(subject, PERMIT_PATH=path, ROOT_UID=os.getuid() + 1, ROOT_GID=os.getgid()),
                self.assertRaises(subject.Phase9PreEffectDispositionEntrypointError),
            ):
                subject._read_and_verify_permit()
            link = path.with_name("permit-link.json")
            os.link(path, link)
            with (
                mock.patch.multiple(subject, PERMIT_PATH=path, ROOT_UID=os.getuid(), ROOT_GID=os.getgid()),
                self.assertRaises(subject.Phase9PreEffectDispositionEntrypointError),
            ):
                subject._read_and_verify_permit()


class CandidateAndExecutionTests(unittest.TestCase):
    @staticmethod
    def git_outputs(document: dict[str, object], *, status: bytes = b"", head: str | None = None,
                    tree: str | None = None, blob_drift: bool = False):
        blobs = document["source_blobs"]
        assert isinstance(blobs, dict)
        def run(*arguments: str, maximum: int = subject.MAX_GIT_OUTPUT_BYTES) -> bytes:
            del maximum
            if arguments == ("rev-parse", "--show-toplevel"):
                return (str(subject._REPOSITORY_ROOT) + "\n").encode()
            if arguments == ("status", "--porcelain=v1", "--untracked-files=all"):
                return status
            if arguments[:4] == ("ls-files", "--error-unmatch", "--stage", "--"):
                return b"tracked\n"
            if arguments == ("rev-parse", "--verify", "HEAD^{commit}"):
                return ((head or str(document["candidate_git_commit"])) + "\n").encode()
            if arguments == ("rev-parse", "--verify", "HEAD^{tree}"):
                return ((tree or str(document["candidate_git_tree"])) + "\n").encode()
            for relative in subject._SOURCE_PATHS:
                value = str(blobs[relative])
                if arguments == ("rev-parse", "HEAD:" + relative):
                    return (value + "\n").encode()
                if arguments == ("hash-object", str(subject._REPOSITORY_ROOT / relative)):
                    if blob_drift and relative == subject._CONTROLLER_RELATIVE:
                        value = "f" * 40
                    return (value + "\n").encode()
            raise AssertionError(arguments)
        return run

    def test_clean_current_candidate_matches_permit_without_reading_tag(self) -> None:
        document = permit()
        with mock.patch.object(subject, "_git", side_effect=self.git_outputs(document)) as git:
            identity = subject._verified_candidate_identity(document)
        self.assertEqual(identity.commit, document["candidate_git_commit"])
        self.assertFalse(any("refs/tags/" in call.args for call in git.call_args_list))

    def test_dirty_head_mismatch_or_blob_drift_is_refused(self) -> None:
        document = permit()
        cases = (
            {"status": b" M tracked.py\n"},
            {"head": "f" * 40},
            {"tree": "e" * 40},
            {"blob_drift": True},
        )
        for values in cases:
            with (
                self.subTest(values=values),
                mock.patch.object(subject, "_git", side_effect=self.git_outputs(document, **values)),
                self.assertRaises(subject.Phase9PreEffectDispositionEntrypointError),
            ):
                subject._verified_candidate_identity(document)

    def test_permit_precedes_controller_import_and_install(self) -> None:
        document = permit()
        identity = subject.CandidateIdentity("a" * 40, "b" * 40, tuple(sorted(document["source_blobs"].items())))
        expectation = disposition.ReviewedDispositionExpectation(
            contract_sha256=subject.CONTRACT_SHA256,
            disposition_id=disposition.PRODUCTION_DISPOSITION_ID,
            authorization_text_sha256=subject.AUTHORIZATION_TEXT_SHA256,
            predecessor_attempt_identity_sha256=subject.PREDECESSOR_ATTEMPT_IDENTITY_SHA256,
            successor_attempt_identity_sha256=subject.SUCCESSOR_ATTEMPT_IDENTITY_SHA256,
            successor_generation="000002",
        )
        events: list[str] = []
        receipt = {"result": disposition.RESULT}
        with (
            mock.patch.multiple(subject, _ISOLATED_RUNTIME_AT_START=True, _DONT_WRITE_BYTECODE_AT_START=True),
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
            mock.patch.object(subject, "_read_and_verify_permit", side_effect=lambda: events.append("permit") or document),
            mock.patch.object(subject, "_verified_candidate_identity", side_effect=lambda unused: events.append("candidate") or identity),
            mock.patch.object(subject, "_load_disposition", side_effect=lambda: events.append("import") or disposition),
            mock.patch.object(subject, "_expectation", return_value=expectation),
            mock.patch.object(subject, "_read_repository_contract", return_value=b"contract"),
            mock.patch.object(subject, "_reverify_candidate"),
            mock.patch.object(subject, "_install_create_once", side_effect=lambda **unused: events.append("install")),
            mock.patch.object(disposition, "execute_pre_effect_disposition", return_value=receipt),
        ):
            observed = subject.execute_exact_production_disposition()
        self.assertIs(observed, receipt)
        self.assertEqual(events[:4], ["permit", "candidate", "import", "install"])

    def test_import_callable_disposition_rechecks_exact_runtime_and_manager(self) -> None:
        with (
            mock.patch.multiple(
                subject,
                _ISOLATED_RUNTIME_AT_START=True,
                _DONT_WRITE_BYTECODE_AT_START=True,
            ),
            mock.patch.object(subject.sys, "platform", "linux"),
            mock.patch.object(subject.sys, "executable", "/usr/bin/python3"),
            mock.patch.object(subject, "_read_and_verify_permit") as read_permit,
            self.assertRaisesRegex(
                subject.Phase9PreEffectDispositionEntrypointError,
                "phase9_pre_effect_disposition_runtime_isolation_required",
            ),
        ):
            subject.execute_exact_production_disposition()
        read_permit.assert_not_called()

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
                subject, "_preimport_manager_lineage_valid", return_value=False
            ),
            mock.patch.object(subject, "_read_and_verify_permit") as read_permit,
            self.assertRaisesRegex(
                subject.Phase9PreEffectDispositionEntrypointError,
                "phase9_pre_effect_disposition_manager_lineage_required",
            ),
        ):
            subject.execute_exact_production_disposition()
        read_permit.assert_not_called()

    def test_no_operational_arguments(self) -> None:
        with self.assertRaisesRegex(
            subject.Phase9PreEffectDispositionEntrypointError,
            "phase9_pre_effect_disposition_arguments_refused",
        ):
            subject.main(("--permit", "/tmp/foreign"))


if __name__ == "__main__":
    unittest.main()
