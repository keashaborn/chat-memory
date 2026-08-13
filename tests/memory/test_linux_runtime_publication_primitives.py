from __future__ import annotations

from pathlib import PurePosixPath
import inspect
import stat
from types import SimpleNamespace
import unittest

from tools.governed_memory_install.controller_runtime import _INVENTORY_PROBE
from tools.governed_memory_release.linux_runtime_publication_primitives import (
    LinuxRuntimePublicationPrimitiveError,
    LinuxRuntimePublicationPrimitives,
    FIXED_CONTROLLER_RUNTIME_PARENT_LAYOUT,
    FIXED_CONTROLLER_RUNTIME_PARENT_ROOTS,
    _allowed_path,
    _managed_parent_root_is_safe,
    bootstrap_controller_runtime_parent_roots,
)


PLAN = "1" * 64
PYTHON = (
    "/var/lib/governed-memory-controller/build-staging/"
    + PLAN
    + "/runtime/bin/python"
)


class LinuxRuntimePublicationPrimitiveTests(unittest.TestCase):
    def test_parent_root_bootstrap_is_parameterless_and_fixed(self):
        self.assertEqual(
            tuple(inspect.signature(bootstrap_controller_runtime_parent_roots).parameters),
            (),
        )
        self.assertEqual(
            FIXED_CONTROLLER_RUNTIME_PARENT_ROOTS,
            (
                PurePosixPath("/var/lib/governed-memory-controller"),
                PurePosixPath("/var/lib/governed-memory-controller/incoming"),
                PurePosixPath("/var/lib/governed-memory-controller/incoming/cpython"),
                PurePosixPath("/var/lib/governed-memory-controller/incoming/wheelhouses"),
                PurePosixPath("/var/lib/governed-memory-controller/offline"),
                PurePosixPath("/var/lib/governed-memory-controller/offline/cpython"),
                PurePosixPath("/var/lib/governed-memory-controller/offline/wheelhouses"),
                PurePosixPath("/var/lib/governed-memory-controller/build-staging"),
                PurePosixPath("/var/lib/governed-memory-controller/runtime-receipts"),
                PurePosixPath("/opt/governed-memory-controller"),
                PurePosixPath("/opt/governed-memory-controller/runtimes"),
                PurePosixPath("/opt/governed-memory-controller/releases"),
            ),
        )
        self.assertEqual(
            dict(FIXED_CONTROLLER_RUNTIME_PARENT_LAYOUT),
            {
                **{
                    path: 0o700
                    for path in FIXED_CONTROLLER_RUNTIME_PARENT_ROOTS[:9]
                },
                **{
                    path: 0o755
                    for path in FIXED_CONTROLLER_RUNTIME_PARENT_ROOTS[9:]
                },
            },
        )

    def test_parent_root_modes_are_closed(self):
        directory = stat.S_IFDIR | 0o755
        self.assertTrue(
            _managed_parent_root_is_safe(
                SimpleNamespace(st_mode=directory, st_uid=0, st_gid=0)
            )
        )
        for mode, uid, gid in (
            (stat.S_IFDIR | 0o775, 0, 0),
            (stat.S_IFDIR | 0o755, 1000, 0),
            (stat.S_IFDIR | 0o755, 0, 1000),
            (stat.S_IFREG | 0o755, 0, 0),
        ):
            with self.subTest(mode=mode, uid=uid, gid=gid):
                self.assertFalse(
                    _managed_parent_root_is_safe(
                        SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=gid)
                    )
                )

    def test_path_boundary_is_fixed_and_canonical(self):
        self.assertEqual(
            _allowed_path(
                "/opt/governed-memory-controller/runtimes/" + "2" * 64
            ),
            PurePosixPath(
                "/opt/governed-memory-controller/runtimes/" + "2" * 64
            ),
        )
        for path in (
            "/tmp/controller-runtime",
            "/opt/governed-memory-controller/runtimes/../escape",
            "relative/path",
        ):
            with self.subTest(path=path), self.assertRaises(
                LinuxRuntimePublicationPrimitiveError
            ):
                _allowed_path(path)

    def test_subprocess_surface_accepts_only_fixed_build_shapes(self):
        allowed = LinuxRuntimePublicationPrimitives._argv_allowed
        self.assertTrue(
            allowed(
                (
                    PYTHON,
                    "-I",
                    "-B",
                    "-c",
                    _INVENTORY_PROBE,
                    "/var/lib/governed-memory-controller/build-staging/"
                    + PLAN
                    + "/runtime",
                )
            )
        )
        wheelhouse = (
            "/var/lib/governed-memory-controller/offline/wheelhouses/"
            + "3" * 64
        )
        lock = (
            "/var/lib/governed-memory-controller/build-staging/"
            + PLAN
            + "/.controller-requirements.lock"
        )
        pip = (
            PYTHON,
            "-I",
            "-B",
            "-m",
            "pip",
            "install",
            "--no-index",
            "--disable-pip-version-check",
            "--no-compile",
            "--no-deps",
            "--require-hashes",
            "--only-binary=:all:",
            "--find-links",
            wheelhouse,
            "--requirement",
            lock,
        )
        self.assertTrue(allowed(pip))
        self.assertFalse(allowed((*pip[:-1], "/tmp/requirements.lock")))
        self.assertFalse(allowed(("/usr/bin/python3", *pip[1:])))
        self.assertFalse(allowed((*pip, "https://example.invalid")))

    def test_rename_surface_is_pair_closed(self):
        instance = object.__new__(LinuxRuntimePublicationPrimitives)
        with self.assertRaises(LinuxRuntimePublicationPrimitiveError):
            instance.rename_no_replace(
                "/var/lib/governed-memory-controller/build-staging/"
                + PLAN
                + "/runtime",
                "/opt/governed-memory-controller/releases/" + "2" * 64,
            )


if __name__ == "__main__":
    unittest.main()
