from __future__ import annotations

"""Offline inspector for the one selected standalone CPython archive."""

import argparse
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import tarfile
import tempfile

# Direct execution under the controller's isolated interpreter intentionally
# removes the working directory from sys.path.  Bind imports to the exact
# release root containing this already package-hashed script.
_RELEASE_ROOT = Path(__file__).resolve().parents[2]
if str(_RELEASE_ROOT) not in sys.path:
    sys.path.insert(0, str(_RELEASE_ROOT))

from tools.governed_memory_install.authority import canonical_json_bytes
from tools.governed_memory_release.controller_runtime_builder import (
    ControllerRuntimeBuildError,
    SELECTED_CPYTHON_ARCHIVE_NAME,
    SELECTED_CPYTHON_ARCHIVE_SHA256,
    SELECTED_CPYTHON_VERSION,
    SUBSTRATE_SCHEMA,
    SUBSTRATE_STATE,
)
from tools.governed_memory_release.runtime_publication_transport import (
    ArchiveMember,
    _expanded_archive_members,
)


class StandaloneCPythonInspectionError(RuntimeError):
    """Content-free refusal from independent offline archive inspection."""


_MAX_ARCHIVE_MEMBERS = 100_000
_MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
_PAYLOAD_TREE_SCHEMA = "governed-memory-standalone-cpython-expanded-payload-tree-v1"
_SELECTED_INCOMING_ARCHIVE = PurePosixPath(
    "/var/lib/governed-memory-controller/incoming/cpython"
) / SELECTED_CPYTHON_ARCHIVE_SHA256 / SELECTED_CPYTHON_ARCHIVE_NAME


def _fail() -> None:
    raise StandaloneCPythonInspectionError(
        "standalone_cpython_archive_inspection_refused"
    )


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as source:
        while True:
            block = source.read(1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > _MAX_ARCHIVE_BYTES:
                _fail()
            digest.update(block)
    return total, digest.hexdigest()


def _archive_members(archive: tarfile.TarFile) -> tuple[ArchiveMember, ...]:
    result: list[ArchiveMember] = []
    total = 0
    for member in archive:
        if len(result) >= _MAX_ARCHIVE_MEMBERS:
            _fail()
        kind = (
            "directory"
            if member.isdir()
            else "file"
            if member.isreg()
            else "symlink"
            if member.issym()
            else "hardlink"
            if member.islnk()
            else "special"
        )
        size = member.size if kind == "file" else 0
        total += size
        if total > _MAX_ARCHIVE_BYTES:
            _fail()
        result.append(
            ArchiveMember(
                member.name.rstrip("/") if member.isdir() else member.name,
                kind,
                member.mode & 0o777,
                size,
                member.linkname if kind == "symlink" else None,
            )
        )
    return tuple(result)


def _inspect_archive(
    archive_path: Path,
    *,
    expected_archive_sha256: str,
    expected_symlink_count: int,
) -> bytes:
    if (
        not isinstance(archive_path, Path)
        or type(expected_archive_sha256) is not str
        or len(expected_archive_sha256) != 64
        or type(expected_symlink_count) is not int
        or expected_symlink_count < 0
        or not archive_path.is_absolute()
        or archive_path.is_symlink()
        or not archive_path.is_file()
    ):
        _fail()
    unused_size, archive_sha256 = _hash_file(archive_path)
    if archive_sha256 != expected_archive_sha256:
        _fail()
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            members = _archive_members(archive)
    except (OSError, tarfile.TarError):
        _fail()
    if sum(member.kind == "symlink" for member in members) != expected_symlink_count:
        _fail()
    try:
        expanded, mapping_sha256 = _expanded_archive_members(members)
    except ControllerRuntimeBuildError:
        _fail()

    with tempfile.TemporaryDirectory(
        prefix="governed-memory-cpython-inspect-"
    ) as temporary:
        root = Path(temporary)
        try:
            with tarfile.open(archive_path, mode="r:gz") as archive:
                by_name = {member.name: member for member in archive}
                for member in expanded:
                    destination = root / member.path
                    if member.kind == "directory":
                        destination.mkdir(parents=True, exist_ok=False)
                        destination.chmod(0o700)
                        continue
                    if member.source_member_path is None:
                        _fail()
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    source_member = by_name.get(member.source_member_path)
                    if source_member is None or not source_member.isreg():
                        _fail()
                    source = archive.extractfile(source_member)
                    if source is None:
                        _fail()
                    digest = hashlib.sha256()
                    total = 0
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                    descriptor = os.open(destination, flags, 0o600)
                    try:
                        while True:
                            block = source.read(1024 * 1024)
                            if not block:
                                break
                            total += len(block)
                            if total > source_member.size:
                                _fail()
                            digest.update(block)
                            view = memoryview(block)
                            while view:
                                written = os.write(descriptor, view)
                                if written <= 0:
                                    _fail()
                                view = view[written:]
                        if total != source_member.size:
                            _fail()
                        os.fchmod(descriptor, 0o700 if member.mode == 0o555 else 0o600)
                    finally:
                        os.close(descriptor)
        except (OSError, tarfile.TarError):
            _fail()

        entries: list[dict[str, object]] = []
        for path in sorted(root.rglob("*"), key=lambda value: value.relative_to(root).as_posix()):
            metadata = path.lstat()
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(metadata.st_mode):
                _fail()
            if stat.S_ISDIR(metadata.st_mode):
                entries.append(
                    {"mode": 0o555, "path": relative + "/", "sha256": "directory"}
                )
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink != 1:
                    _fail()
                unused, digest = _hash_file(path)
                entries.append(
                    {
                        "mode": 0o555 if metadata.st_mode & 0o100 else 0o444,
                        "path": relative,
                        "sha256": digest,
                    }
                )
            else:
                _fail()
    payload_tree_sha256 = hashlib.sha256(
        canonical_json_bytes(
            {"schema_version": _PAYLOAD_TREE_SCHEMA, "entries": entries}
        )
    ).hexdigest()
    return canonical_json_bytes(
        {
            "schema_version": SUBSTRATE_SCHEMA,
            "state": SUBSTRATE_STATE,
            "implementation": "CPython",
            "python_version": SELECTED_CPYTHON_VERSION,
            "platform_os": "linux",
            "platform_architecture": "x86_64",
            "distribution_kind": "standalone-cpython-install-only",
            "archive_name": SELECTED_CPYTHON_ARCHIVE_NAME,
            "archive_sha256": archive_sha256,
            "payload_root": "python",
            "interpreter_relative_path": "bin/python",
            "archive_member_policy": (
                "directories-regular-files-and-relative-in-payload-symlinks-only"
            ),
            "archive_hardlinks_or_special_files_present": False,
            "archive_symlink_count": expected_symlink_count,
            "archive_symlinks_absolute_escape_dangling_or_cyclic": False,
            "symlink_expansion_policy": (
                "relative-in-payload-links-expanded-to-independent-regular-file-"
                "or-directory-copies"
            ),
            "symlink_expansion_mapping_sha256": mapping_sha256,
            "expanded_payload_tree_schema": _PAYLOAD_TREE_SCHEMA,
            "expanded_payload_tree_sha256": payload_tree_sha256,
            "expanded_payload_is_symlink_hardlink_special_free": True,
            "runtime_is_not_venv": True,
            "network_calls": 0,
        }
    )


def inspect_selected_standalone_archive(archive_path: str) -> bytes:
    if (
        type(archive_path) is not str
        or PurePosixPath(archive_path) != _SELECTED_INCOMING_ARCHIVE
        or str(PurePosixPath(archive_path)) != archive_path
    ):
        _fail()
    selected = Path(archive_path)
    try:
        if selected.resolve(strict=True) != selected:
            _fail()
        metadata = selected.lstat()
    except OSError:
        _fail()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o444
    ):
        _fail()
    return _inspect_archive(
        selected,
        expected_archive_sha256=SELECTED_CPYTHON_ARCHIVE_SHA256,
        expected_symlink_count=1048,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True)
    args = parser.parse_args(argv)
    try:
        raw = inspect_selected_standalone_archive(args.archive)
    except StandaloneCPythonInspectionError:
        return 2
    sys.stdout.buffer.write(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "StandaloneCPythonInspectionError",
    "inspect_selected_standalone_archive",
    "main",
]
