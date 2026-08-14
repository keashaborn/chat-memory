from __future__ import annotations

import os
import errno
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from tools.governed_memory_validation import publish_phase9_pre_effect_permit as subject


class CandidateSelectionTests(unittest.TestCase):
    @staticmethod
    def outputs(*, status: bytes = b"", tag_commit: str = "a" * 40,
                tag_tree: str = "b" * 40, head_commit: str = "a" * 40,
                head_tree: str = "b" * 40, blob_drift: bool = False):
        def run(*arguments: str, allow_status: int = 0) -> bytes:
            del allow_status
            if arguments == ("rev-parse", "--show-toplevel"):
                return (str(subject._REPOSITORY_ROOT) + "\n").encode()
            if arguments == ("status", "--porcelain=v1", "--untracked-files=all"):
                return status
            if arguments[:4] == ("ls-files", "--error-unmatch", "--stage", "--"):
                return b"tracked\n"
            values = {
                ("rev-parse", "--verify", subject._EXPECTED_CANDIDATE_REF + "^{commit}"): tag_commit,
                ("rev-parse", "--verify", subject._EXPECTED_CANDIDATE_REF + "^{tree}"): tag_tree,
                ("rev-parse", "--verify", "HEAD^{commit}"): head_commit,
                ("rev-parse", "--verify", "HEAD^{tree}"): head_tree,
                ("rev-parse", "--verify", subject.BASE_CANDIDATE_COMMIT + "^{tree}"): subject.BASE_CANDIDATE_TREE,
            }
            if arguments == ("merge-base", "--is-ancestor", subject.BASE_CANDIDATE_COMMIT, "HEAD"):
                return b""
            if arguments in values:
                return (values[arguments] + "\n").encode()
            for index, relative in enumerate(subject._SOURCE_PATHS):
                value = chr(99 + index) * 40
                if arguments == ("rev-parse", "HEAD:" + relative):
                    return (value + "\n").encode()
                if arguments == ("hash-object", str(subject._REPOSITORY_ROOT / relative)):
                    if blob_drift and relative == subject._CONTROLLER_RELATIVE:
                        value = "f" * 40
                    return (value + "\n").encode()
            raise AssertionError(arguments)
        return run

    def test_fixed_tag_selects_clean_descendant_and_four_exact_blobs(self) -> None:
        with mock.patch.object(subject, "_git", side_effect=self.outputs()):
            commit, tree, blobs = subject._verify_selected_candidate()
        self.assertEqual(commit, "a" * 40)
        self.assertEqual(tree, "b" * 40)
        self.assertEqual(set(blobs), set(subject._SOURCE_PATHS))

    def test_moved_tag_dirty_candidate_or_blob_drift_is_refused(self) -> None:
        cases = (
            {"tag_commit": "e" * 40},
            {"tag_tree": "e" * 40},
            {"status": b"?? foreign.py\n"},
            {"blob_drift": True},
        )
        for values in cases:
            with (
                self.subTest(values=values),
                mock.patch.object(subject, "_git", side_effect=self.outputs(**values)),
                self.assertRaises(subject.Phase9PreEffectPermitPublicationError),
            ):
                subject._verify_selected_candidate()

    def test_git_command_failure_is_content_free(self) -> None:
        completed = mock.Mock(returncode=128, stdout=b"", stderr=b"")
        with (
            mock.patch.object(subject.subprocess, "run", return_value=completed),
            self.assertRaisesRegex(
                subject.Phase9PreEffectPermitPublicationError,
                "phase9_pre_effect_permit_git_failed",
            ),
        ):
            subject._git("rev-parse", "--show-toplevel")


class PermitPublicationTests(unittest.TestCase):
    def publication_fixture(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        root.chmod(0o700)
        path = root / subject.PERMIT_PATH.name
        raw = subject._permit(
            "a" * 40, "b" * 40,
            {path: chr(99 + index) * 40 for index, path in enumerate(subject._SOURCE_PATHS)},
        )
        patcher = mock.patch.multiple(
            subject, STATE_ROOT=root, PERMIT_PATH=path,
            PERMIT_STAGING_NAME="." + path.name + ".publishing",
            ROOT_UID=os.getuid(), ROOT_GID=os.getgid(),
        )
        return temporary, root, path, raw, patcher

    def test_tag_or_candidate_change_before_publication_is_refused(self) -> None:
        first = (
            "a" * 40,
            "b" * 40,
            {path: chr(99 + index) * 40 for index, path in enumerate(subject._SOURCE_PATHS)},
        )
        changed = ("e" * 40, first[1], first[2])
        with (
            mock.patch.multiple(
                subject,
                _ISOLATED_RUNTIME_AT_START=True,
                _DONT_WRITE_BYTECODE_AT_START=True,
            ),
            mock.patch.object(subject.sys, "platform", "linux"),
            mock.patch.object(subject.os, "geteuid", return_value=0),
            mock.patch.object(subject.os, "getegid", return_value=0),
            mock.patch.object(
                subject,
                "_verify_selected_candidate",
                side_effect=(first, changed),
            ),
            mock.patch.object(subject, "_verify_runtime_receipt"),
            mock.patch.object(subject, "_publish_create_once") as publish,
            self.assertRaisesRegex(
                subject.Phase9PreEffectPermitPublicationError,
                "phase9_pre_effect_permit_candidate_changed",
            ),
        ):
            subject.publish_phase9_pre_effect_permit()
        publish.assert_not_called()

    def test_permit_binds_exact_authority_and_action_only_boundary(self) -> None:
        blobs = {path: chr(99 + index) * 40 for index, path in enumerate(subject._SOURCE_PATHS)}
        raw = subject._permit("a" * 40, "b" * 40, blobs)
        value = subject._document(raw, "invalid")
        self.assertEqual(value["base_candidate_git_commit"], subject.BASE_CANDIDATE_COMMIT)
        self.assertEqual(value["package_manifest_sha256"], subject.PACKAGE_MANIFEST_SHA256)
        self.assertEqual(value["contract_sha256"], subject.CONTRACT_SHA256)
        self.assertEqual(value["authorized_action"], "execute_pre_effect_disposition_only")
        self.assertIs(value["activation_performed"], False)
        self.assertEqual(value["provider_calls"], 0)
        self.assertIs(value["production_data_read"], False)
        self.assertIs(value["deletion_performed"], False)

    def test_create_once_root_permit_allows_exact_replay_only(self) -> None:
        temporary, unused_root, path, raw, patcher = self.publication_fixture()
        with temporary, patcher:
                subject._publish_create_once(raw)
                inode = path.stat().st_ino
                subject._publish_create_once(raw)
                self.assertEqual(path.stat().st_ino, inode)
                self.assertEqual(path.stat().st_mode & 0o777, 0o400)
                with self.assertRaisesRegex(
                    subject.Phase9PreEffectPermitPublicationError,
                    "phase9_pre_effect_permit_replay_mismatch",
                ):
                    subject._publish_create_once(raw + b"x")

    def test_exact_complete_staging_is_atomically_completed(self) -> None:
        temporary, root, path, raw, patcher = self.publication_fixture()
        with temporary, patcher:
            staging = root / subject.PERMIT_STAGING_NAME
            staging.write_bytes(raw)
            staging.chmod(0o400)
            staged_inode = staging.stat().st_ino
            subject._publish_create_once(raw)
            self.assertFalse(staging.exists())
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(path.stat().st_ino, staged_inode)
            self.assertEqual(path.stat().st_nlink, 1)

    def test_partial_or_foreign_staging_and_final_fail_closed_untouched(self) -> None:
        for kind in ("partial_staging", "foreign_staging", "foreign_final"):
            temporary, root, path, raw, patcher = self.publication_fixture()
            with temporary, patcher, self.subTest(kind=kind):
                selected = (
                    path
                    if kind == "foreign_final"
                    else root / subject.PERMIT_STAGING_NAME
                )
                selected.write_bytes(
                    raw[:17] if kind == "partial_staging" else b"foreign"
                )
                selected.chmod(0o400)
                inode = selected.stat().st_ino
                before = selected.read_bytes()
                with self.assertRaises(subject.Phase9PreEffectPermitPublicationError):
                    subject._publish_create_once(raw)
                self.assertEqual(selected.read_bytes(), before)
                self.assertEqual(selected.stat().st_ino, inode)
                if kind != "foreign_final":
                    self.assertFalse(path.exists())

    def test_hardlinked_staging_is_foreign_and_never_unlinked(self) -> None:
        temporary, root, path, raw, patcher = self.publication_fixture()
        with temporary, patcher:
            staging = root / subject.PERMIT_STAGING_NAME
            staging.write_bytes(raw)
            staging.chmod(0o400)
            foreign_link = root / "foreign-link.json"
            os.link(staging, foreign_link)
            inode = staging.stat().st_ino
            with self.assertRaises(
                subject.Phase9PreEffectPermitPublicationError
            ):
                subject._publish_create_once(raw)
            self.assertFalse(path.exists())
            self.assertEqual(staging.stat().st_ino, inode)
            self.assertEqual(foreign_link.stat().st_ino, inode)
            self.assertEqual(staging.stat().st_nlink, 2)

    def test_foreign_fifo_staging_fails_closed_without_blocking_or_unlink(self) -> None:
        temporary, root, path, raw, patcher = self.publication_fixture()
        with temporary, patcher:
            staging = root / subject.PERMIT_STAGING_NAME
            os.mkfifo(staging, 0o400)
            inode = staging.stat().st_ino
            with self.assertRaises(
                subject.Phase9PreEffectPermitPublicationError
            ):
                subject._publish_create_once(raw)
            self.assertFalse(path.exists())
            self.assertEqual(staging.stat().st_ino, inode)
            self.assertTrue(stat.S_ISFIFO(staging.stat().st_mode))

    def test_short_writes_are_completed_before_publication(self) -> None:
        temporary, unused_root, path, raw, patcher = self.publication_fixture()
        original_write = os.write
        writes = 0
        def short_write(descriptor: int, value: object) -> int:
            nonlocal writes
            writes += 1
            view = memoryview(value)
            return original_write(descriptor, view[: max(1, len(view) // 3)])
        with temporary, patcher, mock.patch.object(subject.os, "write", side_effect=short_write):
            subject._publish_create_once(raw)
            self.assertGreater(writes, 1)
            self.assertEqual(path.read_bytes(), raw)

    def test_write_enospc_preserves_partial_private_staging_and_no_final(self) -> None:
        temporary, root, path, raw, patcher = self.publication_fixture()
        original_write = os.write
        calls = 0
        def fail_after_prefix(descriptor: int, value: object) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                return original_write(descriptor, memoryview(value)[:11])
            raise OSError(errno.ENOSPC, "full")
        with temporary, patcher, mock.patch.object(subject.os, "write", side_effect=fail_after_prefix):
            with self.assertRaises(subject.Phase9PreEffectPermitPublicationError):
                subject._publish_create_once(raw)
            self.assertFalse(path.exists())
            staging = root / subject.PERMIT_STAGING_NAME
            self.assertEqual(staging.read_bytes(), raw[:11])

    def test_file_fsync_failure_leaves_complete_reconcilable_staging(self) -> None:
        temporary, root, path, raw, patcher = self.publication_fixture()
        original_fsync = os.fsync
        failed = False
        def fail_first(descriptor: int) -> None:
            nonlocal failed
            if not failed:
                failed = True
                raise OSError(errno.EIO, "fsync")
            original_fsync(descriptor)
        with temporary, patcher:
            with mock.patch.object(subject.os, "fsync", side_effect=fail_first):
                with self.assertRaises(subject.Phase9PreEffectPermitPublicationError):
                    subject._publish_create_once(raw)
                self.assertFalse(path.exists())
                self.assertEqual((root / subject.PERMIT_STAGING_NAME).read_bytes(), raw)
            subject._publish_create_once(raw)
            self.assertEqual(path.read_bytes(), raw)

    def test_close_failure_after_complete_staging_is_reconcilable(self) -> None:
        temporary, root, path, raw, patcher = self.publication_fixture()
        original_close = os.close
        failed = False
        def fail_regular_once(descriptor: int) -> None:
            nonlocal failed
            metadata = os.fstat(descriptor)
            original_close(descriptor)
            if stat.S_ISREG(metadata.st_mode) and not failed:
                failed = True
                raise OSError(errno.EIO, "close")
        with temporary, patcher:
            with mock.patch.object(subject.os, "close", side_effect=fail_regular_once):
                with self.assertRaises(subject.Phase9PreEffectPermitPublicationError):
                    subject._publish_create_once(raw)
                self.assertFalse(path.exists())
                self.assertEqual((root / subject.PERMIT_STAGING_NAME).read_bytes(), raw)
            subject._publish_create_once(raw)
            self.assertEqual(path.read_bytes(), raw)

    def test_parent_fsync_failure_after_link_reconciles_linked_state(self) -> None:
        temporary, root, path, raw, patcher = self.publication_fixture()
        original_fsync = os.fsync
        calls = 0

        def fail_after_link(descriptor: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError(errno.EIO, "parent fsync")
            original_fsync(descriptor)

        with temporary, patcher:
            with mock.patch.object(
                subject.os, "fsync", side_effect=fail_after_link
            ):
                with self.assertRaises(
                    subject.Phase9PreEffectPermitPublicationError
                ):
                    subject._publish_create_once(raw)
                staging = root / subject.PERMIT_STAGING_NAME
                self.assertEqual(path.stat().st_ino, staging.stat().st_ino)
                self.assertEqual(path.stat().st_nlink, 2)
            subject._publish_create_once(raw)
            self.assertFalse(staging.exists())
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(path.stat().st_nlink, 1)

    def test_no_operational_arguments(self) -> None:
        with self.assertRaisesRegex(
            subject.Phase9PreEffectPermitPublicationError,
            "phase9_pre_effect_permit_arguments_refused",
        ):
            subject.main(("--candidate", "foreign"))


if __name__ == "__main__":
    unittest.main()
