from __future__ import annotations

"""Isolated-path launcher for the exact dormant-store supervisor.

The systemd unit invokes this file by its immutable release path with Python
``-I -B``.  Only the launcher's own verified release root is added to the
module path; the working directory and environment never become imports.
"""

from pathlib import Path
import re
import sys
from typing import Final, Sequence


_HASH_RE: Final = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_RELEASES: Final = Path("/opt/governed-memory-controller/releases")
_RELATIVE: Final = Path(
    "tools/governed_memory_install/store_supervisor_launcher.py"
)


class StoreSupervisorLauncherError(RuntimeError):
    """Refuse an unexpected launcher location or release identity."""


def _verified_release_root(expected_package_sha256: str) -> Path:
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
    if invoked != resolved or resolved != expected_launcher:
        raise StoreSupervisorLauncherError("launcher_release_identity_mismatch")
    return expected_root


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if (
        len(arguments) < 3
        or arguments[0] != "--package-manifest-sha256"
    ):
        raise StoreSupervisorLauncherError("launcher_arguments_invalid")
    release_root = _verified_release_root(arguments[1])
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
