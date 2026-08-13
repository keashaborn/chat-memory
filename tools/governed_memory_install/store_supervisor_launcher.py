from __future__ import annotations

"""Isolated-path launcher for the exact dormant-store supervisor.

The systemd unit invokes this file by its immutable release path with Python
``-I -B``.  Only the launcher's own verified release root is added to the
module path; the working directory and environment never become imports.
"""

from pathlib import Path
import hashlib
import os
import re
import stat
import sys
from typing import Final, Sequence


_HASH_RE: Final = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_RELEASES: Final = Path("/opt/governed-memory-controller/releases")
_BUILD_STAGING: Final = Path(
    "/var/lib/governed-memory-controller/build-staging"
)
_RELATIVE: Final = Path(
    "tools/governed_memory_install/store_supervisor_launcher.py"
)
_MANIFEST_RELATIVE: Final = Path(
    "ops/governed_memory/installation/current/package_manifest.json"
)
_ROOT_UID: Final = 0
_ROOT_GID: Final = 0
_MAX_MANIFEST_BYTES: Final = 1024 * 1024


class StoreSupervisorLauncherError(RuntimeError):
    """Refuse an unexpected launcher location or release identity."""


def _verified_staged_manifest(
    release_root: Path, expected_package_sha256: str
) -> None:
    manifest = release_root / _MANIFEST_RELATIVE
    descriptor = -1
    try:
        if manifest.resolve(strict=True) != manifest:
            raise StoreSupervisorLauncherError(
                "launcher_staged_manifest_invalid"
            )
        descriptor = os.open(
            manifest,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        named = os.stat(manifest, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o400
            or before.st_uid != _ROOT_UID
            or before.st_gid != _ROOT_GID
            or before.st_nlink != 1
            or not 1 <= before.st_size <= _MAX_MANIFEST_BYTES
            or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise StoreSupervisorLauncherError(
                "launcher_staged_manifest_invalid"
            )
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            block = os.read(descriptor, min(65536, remaining))
            if not block:
                raise StoreSupervisorLauncherError(
                    "launcher_staged_manifest_invalid"
                )
            digest.update(block)
            remaining -= len(block)
        after = os.fstat(descriptor)
        stable = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            any(getattr(before, key) != getattr(after, key) for key in stable)
            or digest.hexdigest() != expected_package_sha256
        ):
            raise StoreSupervisorLauncherError(
                "launcher_staged_manifest_invalid"
            )
    except StoreSupervisorLauncherError:
        raise
    except OSError as error:
        raise StoreSupervisorLauncherError(
            "launcher_staged_manifest_invalid"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _verified_release_root(
    expected_package_sha256: str, *, staged_help_probe: bool = False
) -> Path:
    if _HASH_RE.fullmatch(expected_package_sha256) is None:
        raise StoreSupervisorLauncherError("launcher_package_identity_invalid")
    invoked = Path(__file__)
    if not invoked.is_absolute():
        raise StoreSupervisorLauncherError("launcher_path_not_absolute")
    try:
        resolved = invoked.resolve(strict=True)
    except OSError as error:
        raise StoreSupervisorLauncherError("launcher_path_invalid") from error
    expected_root = _RELEASES / expected_package_sha256
    expected_launcher = expected_root / _RELATIVE
    if invoked != resolved:
        raise StoreSupervisorLauncherError("launcher_release_identity_mismatch")
    if resolved == expected_launcher:
        return expected_root
    if staged_help_probe:
        try:
            relative = resolved.relative_to(_BUILD_STAGING)
        except ValueError:
            pass
        else:
            parts = relative.parts
            if (
                len(parts) == 2 + len(_RELATIVE.parts)
                and _HASH_RE.fullmatch(parts[0]) is not None
                and parts[1] == "release"
                and Path(*parts[2:]) == _RELATIVE
            ):
                staged_root = _BUILD_STAGING / parts[0] / "release"
                _verified_staged_manifest(
                    staged_root, expected_package_sha256
                )
                return staged_root
    raise StoreSupervisorLauncherError("launcher_release_identity_mismatch")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if (
        len(arguments) < 3
        or arguments[0] != "--package-manifest-sha256"
    ):
        raise StoreSupervisorLauncherError("launcher_arguments_invalid")
    release_root = _verified_release_root(
        arguments[1], staged_help_probe=arguments[2:] == ["--help"]
    )
    sys.path.insert(0, str(release_root))
    try:
        from tools.governed_memory_install.store_supervisor import (
            main as supervisor_main,
        )
        return supervisor_main(arguments[2:])
    finally:
        if sys.path[0] == str(release_root):
            del sys.path[0]


if __name__ == "__main__":
    raise SystemExit(main())
