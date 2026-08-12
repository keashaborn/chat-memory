#!/usr/bin/env python3
from __future__ import annotations

"""Run exactly one guarded dormant-store installation synthetic disposable proof.

There is no live, Docker, systemd, image, secret, install, rollback, network,
or activation option.  The import audit fence is installed before any local
package import; the harness applies a second runtime fence during the proof.
"""

import collections
import collections.abc
import contextlib
import dataclasses
import enum
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import typing


sys.dont_write_bytecode = True
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


class RunnerRefusal(RuntimeError):
    """Content-free refusal from the synthetic-only runner."""


class _ForbiddenImportEnvironment(collections.abc.MutableMapping):
    def _deny(self) -> None:
        raise RunnerRefusal("dormant_store_install_import_environment_access_forbidden")

    def __getitem__(self, key: object) -> object:
        self._deny()

    def __setitem__(self, key: object, value: object) -> None:
        self._deny()

    def __delitem__(self, key: object) -> None:
        self._deny()

    def __iter__(self) -> typing.Iterator[object]:
        self._deny()

    def __len__(self) -> int:
        self._deny()


_IMPORT_GUARD_ACTIVE = True
_IMPORT_GUARD_BLOCKED_EFFECT_COUNT = 0


def _repository_read_allowed(path_value: object) -> bool:
    if isinstance(path_value, int):
        return False
    if isinstance(path_value, os.PathLike):
        try:
            path_value = os.fspath(path_value)
        except TypeError:
            return False
    if isinstance(path_value, bytes):
        try:
            path_value = path_value.decode(sys.getfilesystemencoding())
        except UnicodeError:
            return False
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        return False
    try:
        Path(path_value).resolve(strict=False).relative_to(REPOSITORY_ROOT)
    except (OSError, ValueError):
        return False
    return True


def _block_import_effect(code: str) -> None:
    global _IMPORT_GUARD_BLOCKED_EFFECT_COUNT
    _IMPORT_GUARD_BLOCKED_EFFECT_COUNT += 1
    raise RunnerRefusal(code)


def _import_audit_hook(event: str, args: tuple[object, ...]) -> None:
    if not _IMPORT_GUARD_ACTIVE:
        return
    if (
        event
        in {
            "subprocess.Popen",
            "os.system",
            "os.fork",
            "os.forkpty",
            "pty.spawn",
        }
        or event.startswith("os.exec")
        or event.startswith("os.spawn")
        or event.startswith("os.posix_spawn")
    ):
        _block_import_effect("dormant_store_install_import_process_effect_forbidden")
    if (
        event
        in {
            "socket.__new__",
            "socket.bind",
            "socket.connect",
            "socket.connect_ex",
            "socket.getaddrinfo",
            "socket.gethostbyaddr",
            "socket.gethostbyname",
            "socket.gethostbyname_ex",
            "socket.getnameinfo",
            "socket.sendto",
        }
        or event.startswith("http.client")
        or event.startswith("urllib")
    ):
        _block_import_effect("dormant_store_install_import_network_effect_forbidden")
    if event.startswith("ctypes.") or event in {"os.putenv", "os.unsetenv"}:
        _block_import_effect("dormant_store_install_import_dynamic_effect_forbidden")
    if event == "open" and args:
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        write_mask = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        write = (
            isinstance(mode, str) and any(token in mode for token in "wax+")
        ) or (isinstance(flags, int) and bool(flags & write_mask))
        if write:
            _block_import_effect("dormant_store_install_import_filesystem_write_forbidden")
        if not _repository_read_allowed(args[0]):
            _block_import_effect("dormant_store_install_import_filesystem_read_forbidden")
    if event in {
        "os.chmod",
        "os.chown",
        "os.link",
        "os.mkdir",
        "os.mkfifo",
        "os.mknod",
        "os.remove",
        "os.removexattr",
        "os.rename",
        "os.replace",
        "os.rmdir",
        "os.setxattr",
        "os.symlink",
        "os.truncate",
        "os.utime",
    }:
        _block_import_effect("dormant_store_install_import_filesystem_write_forbidden")


sys.addaudithook(_import_audit_hook)
_ORIGINAL_ENVIRON = os.environ
_ORIGINAL_ENVIRONB = getattr(os, "environb", None)
_FORBIDDEN_IMPORT_ENVIRONMENT = _ForbiddenImportEnvironment()
os.environ = _FORBIDDEN_IMPORT_ENVIRONMENT  # type: ignore[assignment]
if _ORIGINAL_ENVIRONB is not None:
    os.environb = _FORBIDDEN_IMPORT_ENVIRONMENT  # type: ignore[assignment]

try:
    from tools.governed_memory_install.disposable_proof_harness import (
        DisposableProofError,
        run_disposable_proof,
    )
finally:
    os.environ = _ORIGINAL_ENVIRON
    if _ORIGINAL_ENVIRONB is not None:
        os.environb = _ORIGINAL_ENVIRONB  # type: ignore[assignment]
    _IMPORT_GUARD_ACTIVE = False


if _IMPORT_GUARD_BLOCKED_EFFECT_COUNT != 0:
    raise RunnerRefusal("dormant_store_install_import_guard_not_clean")

IMPORT_PHASE_GUARD_COMPLETED = True


def run_synthetic_proof() -> dict[str, object]:
    """Create one private disposable root and run the sole synthetic proof."""

    with tempfile.TemporaryDirectory(
        prefix="dormant_store_install-disposable-proof-"
    ) as temporary:
        root = Path(temporary).resolve(strict=True)
        os.chmod(root, 0o700)
        return run_disposable_proof(disposable_root=root)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        raise RunnerRefusal("dormant_store_install_runner_accepts_no_arguments")
    receipt = run_synthetic_proof()
    sys.stdout.buffer.write(
        json.dumps(
            receipt,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        + b"\n"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DisposableProofError, RunnerRefusal) as error:
        sys.stderr.write(str(error) + "\n")
        raise SystemExit(1) from None
