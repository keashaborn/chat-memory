from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from tools.governed_memory_validation import (
    bootstrap_phase9_disposable_store_substrate as subject,
)


class Phase9DisposableStoreSubstrateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store_parent = root / "governed-memory-stores"
        self.store_parent.mkdir(mode=0o755)
        self.store_target = self.store_parent / "9a54cf123493-000002"
        self.execution_parent = root / "governed-memory-controller"
        self.execution_parent.mkdir(mode=0o700)
        self.execution_target = self.execution_parent / "executions-v2"
        self.selected = (
            subject._FixedDirectory(
                self.store_parent, 0o755, self.store_target.name, self.store_target
            ),
            subject._FixedDirectory(
                self.execution_parent,
                0o700,
                self.execution_target.name,
                self.execution_target,
            ),
        )
        self.runtime = mock.patch.multiple(
            subject,
            ROOT_UID=os.getuid(),
            ROOT_GID=os.getgid(),
            _ISOLATED_RUNTIME_AT_START=True,
            _DONT_WRITE_BYTECODE_AT_START=True,
        )
        self.platform = mock.patch.object(subject.sys, "platform", "linux")
        self.runtime.start()
        self.platform.start()

    def tearDown(self) -> None:
        self.platform.stop()
        self.runtime.stop()
        self.temporary.cleanup()

    def bootstrap(self) -> tuple[tuple[str, str], ...]:
        return subject._bootstrap_directories(self.selected)

    def production_bootstrap(self) -> dict[str, object]:
        with (
            mock.patch.object(subject.os, "geteuid", return_value=os.getuid()),
            mock.patch.object(subject.os, "getegid", return_value=os.getgid()),
        ):
            return dict(subject.bootstrap_phase9_disposable_store_substrate())

    def test_create_and_exact_replay_preserve_identity_and_mode(self) -> None:
        first = self.bootstrap()
        identities = {
            path: (path.stat().st_dev, path.stat().st_ino)
            for path in (self.store_target, self.execution_target)
        }
        second = self.bootstrap()
        self.assertEqual({state for _, state in first}, {"created"})
        self.assertEqual({state for _, state in second}, {"replayed"})
        self.assertEqual(
            {path for path, _ in first},
            {str(self.store_target), str(self.execution_target)},
        )
        for path in (self.store_target, self.execution_target):
            after = path.stat()
            self.assertEqual(identities[path], (after.st_dev, after.st_ino))
            self.assertEqual(stat.S_IMODE(after.st_mode), 0o700)
            self.assertEqual(tuple(path.iterdir()), ())

    def test_existing_wrong_mode_is_refused_without_normalization(self) -> None:
        self.store_target.mkdir(mode=0o755)
        self.store_target.chmod(0o755)
        before = self.store_target.stat()
        with self.assertRaisesRegex(
            subject.Phase9DisposableStoreSubstrateError,
            "phase9_store_substrate_refused",
        ):
            self.bootstrap()
        after = self.store_target.stat()
        self.assertEqual(stat.S_IMODE(after.st_mode), 0o755)
        self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
        self.assertFalse(self.execution_target.exists())

    def test_exact_replay_does_not_chown_or_chmod_existing_directories(self) -> None:
        self.bootstrap()
        with (
            mock.patch.object(subject.os, "fchown") as chown,
            mock.patch.object(subject.os, "fchmod") as chmod,
        ):
            replay = self.bootstrap()
        self.assertEqual({state for _, state in replay}, {"replayed"})
        chown.assert_not_called()
        chmod.assert_not_called()

    def test_existing_file_or_symlink_is_refused(self) -> None:
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir(mode=0o700)
        for kind in ("file", "symlink"):
            with self.subTest(kind=kind):
                if self.store_target.exists() or self.store_target.is_symlink():
                    if self.store_target.is_dir() and not self.store_target.is_symlink():
                        self.store_target.rmdir()
                    else:
                        self.store_target.unlink()
                if kind == "file":
                    self.store_target.write_bytes(b"foreign")
                else:
                    self.store_target.symlink_to(outside, target_is_directory=True)
                with self.assertRaises(subject.Phase9DisposableStoreSubstrateError):
                    self.bootstrap()
                self.assertEqual(tuple(outside.iterdir()), ())
                self.assertFalse(self.execution_target.exists())

    def test_wrong_parent_mode_is_refused_before_either_leaf_is_created(self) -> None:
        self.store_parent.chmod(0o775)
        with self.assertRaises(subject.Phase9DisposableStoreSubstrateError):
            self.bootstrap()
        self.assertFalse(self.store_target.exists())
        self.assertFalse(self.execution_target.exists())
        self.assertEqual(stat.S_IMODE(self.store_parent.stat().st_mode), 0o775)

    def test_named_identity_substitution_is_refused(self) -> None:
        real_stat = subject.os.stat

        def substituted(path: object, *args: object, **kwargs: object) -> os.stat_result:
            observed = real_stat(path, *args, **kwargs)
            if path == self.store_target.name and kwargs.get("dir_fd") is not None:
                values = list(observed)
                values[1] += 1
                return os.stat_result(values)
            return observed

        with (
            mock.patch.object(subject.os, "stat", side_effect=substituted),
            self.assertRaises(subject.Phase9DisposableStoreSubstrateError),
        ):
            self.bootstrap()

    def test_existing_nonempty_directory_is_refused_before_other_creation(self) -> None:
        self.execution_target.mkdir(mode=0o700)
        (self.execution_target / "foreign").write_bytes(b"opaque")
        with self.assertRaises(subject.Phase9DisposableStoreSubstrateError):
            self.bootstrap()
        self.assertFalse(self.store_target.exists())
        self.assertEqual((self.execution_target / "foreign").read_bytes(), b"opaque")

    def test_each_leaf_and_parent_are_fsynced_on_create_and_replay(self) -> None:
        with mock.patch.object(subject.os, "fsync", wraps=os.fsync) as fsync:
            self.bootstrap()
            self.assertEqual(fsync.call_count, 4)
        with mock.patch.object(subject.os, "fsync", wraps=os.fsync) as fsync:
            self.bootstrap()
            self.assertEqual(fsync.call_count, 4)

    def test_isolation_root_and_arguments_are_mandatory(self) -> None:
        with (
            mock.patch.object(subject, "_ISOLATED_RUNTIME_AT_START", False),
            self.assertRaisesRegex(
                subject.Phase9DisposableStoreSubstrateError,
                "phase9_store_substrate_runtime_isolation_required",
            ),
        ):
            self.production_bootstrap()
        with (
            mock.patch.object(subject.os, "geteuid", return_value=os.getuid() + 1),
            mock.patch.object(subject.os, "getegid", return_value=os.getgid()),
            self.assertRaisesRegex(
                subject.Phase9DisposableStoreSubstrateError,
                "phase9_store_substrate_root_required",
            ),
        ):
            subject.bootstrap_phase9_disposable_store_substrate()
        with self.assertRaisesRegex(
            subject.Phase9DisposableStoreSubstrateError,
            "phase9_store_substrate_arguments_refused",
        ):
            subject.main(("--path", "/tmp/foreign"))

    def test_fixed_production_identity_cannot_be_parameterized(self) -> None:
        with (
            mock.patch.object(subject, "STORE_TARGET_LEAF", "foreign"),
            self.assertRaisesRegex(
                subject.Phase9DisposableStoreSubstrateError,
                "phase9_store_substrate_identity_invalid",
            ),
        ):
            self.production_bootstrap()

    def test_result_is_content_free_and_declares_exclusions(self) -> None:
        with (
            mock.patch.object(subject, "_verify_fixed_identity"),
            mock.patch.object(subject, "_fixed_directories", return_value=self.selected),
        ):
            result = self.production_bootstrap()
        self.assertEqual(result["result"], "created")
        self.assertEqual(
            result["exclusions"],
            {
                "activation_changed": False,
                "database_or_vector_store_access": False,
                "docker_access": False,
                "provider_calls": 0,
                "repository_imports": False,
                "secret_access": False,
                "service_changes": False,
            },
        )


if __name__ == "__main__":
    unittest.main()
