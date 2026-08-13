from __future__ import annotations

"""Production Linux primitives for controller runtime publication.

Policy remains in :mod:`runtime_publication_transport`.  This module exposes
only root-owned, no-follow filesystem operations below six fixed controller
roots, a bounded standalone-CPython subprocess runner, safe tar inspection and
regular-member copying, and Linux ``renameat2(RENAME_NOREPLACE)`` publication.
"""

import ctypes
from contextlib import contextmanager
from dataclasses import dataclass
import errno
import hashlib
import os
from pathlib import PurePosixPath
import re
import selectors
import stat
import subprocess
import sys
import tarfile
import time
from typing import Final, Mapping

from .runtime_publication_transport import (
    ArchiveExtraction,
    ArchiveMember,
    NodeObservation,
    ProcessResult,
    TreeMember,
    _REMOVE_BOOTSTRAP,
    _SUBSTRATE_CONFINEMENT_PROBE,
)
from tools.governed_memory_install.controller_runtime import _INVENTORY_PROBE


class LinuxRuntimePublicationPrimitiveError(RuntimeError):
    """Content-free refusal from the production publication substrate."""


_ALLOWED_ROOTS: Final = (
    PurePosixPath("/var/lib/governed-memory-controller/incoming/cpython"),
    PurePosixPath("/var/lib/governed-memory-controller/incoming/wheelhouses"),
    PurePosixPath("/var/lib/governed-memory-controller/build-staging"),
    PurePosixPath("/var/lib/governed-memory-controller/offline/cpython"),
    PurePosixPath("/var/lib/governed-memory-controller/offline/wheelhouses"),
    PurePosixPath("/var/lib/governed-memory-controller/runtime-receipts"),
    PurePosixPath("/opt/governed-memory-controller/runtimes"),
    PurePosixPath("/opt/governed-memory-controller/releases"),
)
FIXED_CONTROLLER_RUNTIME_PARENT_LAYOUT: Final = (
    (PurePosixPath("/var/lib/governed-memory-controller"), 0o700),
    (PurePosixPath("/var/lib/governed-memory-controller/incoming"), 0o700),
    (PurePosixPath("/var/lib/governed-memory-controller/incoming/cpython"), 0o700),
    (PurePosixPath("/var/lib/governed-memory-controller/incoming/wheelhouses"), 0o700),
    (PurePosixPath("/var/lib/governed-memory-controller/offline"), 0o700),
    (PurePosixPath("/var/lib/governed-memory-controller/offline/cpython"), 0o700),
    (PurePosixPath("/var/lib/governed-memory-controller/offline/wheelhouses"), 0o700),
    (PurePosixPath("/var/lib/governed-memory-controller/build-staging"), 0o700),
    (PurePosixPath("/var/lib/governed-memory-controller/runtime-receipts"), 0o700),
    (PurePosixPath("/opt/governed-memory-controller"), 0o755),
    (PurePosixPath("/opt/governed-memory-controller/runtimes"), 0o755),
    (PurePosixPath("/opt/governed-memory-controller/releases"), 0o755),
)
FIXED_CONTROLLER_RUNTIME_PARENT_ROOTS: Final = tuple(
    path for path, unused_mode in FIXED_CONTROLLER_RUNTIME_PARENT_LAYOUT
)
_HASH_RE: Final = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_STAGED_INTERPRETER_RE: Final = re.compile(
    r"/var/lib/governed-memory-controller/build-staging/"
    r"[0-9a-f]{64}/runtime/bin/python\Z",
    re.ASCII,
)
_FIXED_ENVIRONMENT: Final = {
    "HOME": "/nonexistent",
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin",
    "PIP_CONFIG_FILE": "/dev/null",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_NO_INDEX": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONNOUSERSITE": "1",
}
_MAX_PROCESS_OUTPUT: Final = 1024 * 1024
_PROCESS_TIMEOUT_SECONDS: Final = 300
_MAX_ARCHIVE_MEMBERS: Final = 100_000
_MAX_ARCHIVE_BYTES: Final = 2 * 1024 * 1024 * 1024
_RENAME_NOREPLACE: Final = 1
_AT_FDCWD: Final = -100


def _fail() -> None:
    raise LinuxRuntimePublicationPrimitiveError(
        "linux_runtime_publication_primitive_refused"
    )


def _allowed_path(value: str) -> PurePosixPath:
    if type(value) is not str or not value or "\x00" in value:
        _fail()
    path = PurePosixPath(value)
    if not path.is_absolute() or str(path) != value or ".." in path.parts:
        _fail()
    if not any(path == root or root in path.parents for root in _ALLOWED_ROOTS):
        _fail()
    return path


def _root_required() -> None:
    if sys.platform != "linux" or os.geteuid() != 0 or os.getegid() != 0:
        _fail()


@dataclass(frozen=True, slots=True)
class ControllerRuntimeParentRootBootstrapObservation:
    roots: tuple[str, ...]
    created_roots: tuple[str, ...]
    normalized_roots: tuple[str, ...]
    root_owned: bool
    nonwritable_by_group_or_world: bool
    opened_no_follow: bool
    directories_fsynced: bool
    parents_fsynced: bool


def _managed_parent_root_is_safe(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == 0
        and metadata.st_gid == 0
        and stat.S_IMODE(metadata.st_mode) in {0o700, 0o755}
    )


def bootstrap_controller_runtime_parent_roots(
) -> ControllerRuntimeParentRootBootstrapObservation:
    """Create or verify only the fixed controller publication parent roots.

    The function has no caller-selected path or mode.  It is idempotent and
    refuses symlinks, non-directories, non-root ownership, and writable
    group/world modes at every managed root.  It creates no runtime, release,
    receipt, store, service, credential, or activation artifact.
    """

    _root_required()
    created: list[str] = []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    normalized: list[str] = []
    for selected, expected_mode in FIXED_CONTROLLER_RUNTIME_PARENT_LAYOUT:
        parent, name = _open_parent(selected)
        child = -1
        try:
            try:
                os.mkdir(name, mode=expected_mode, dir_fd=parent)
            except FileExistsError:
                pass
            else:
                created.append(str(selected))
            child = os.open(name, flags, dir_fd=parent)
            metadata = os.fstat(child)
            if not _managed_parent_root_is_safe(metadata):
                _fail()
            if stat.S_IMODE(metadata.st_mode) != expected_mode:
                os.fchmod(child, expected_mode)
                normalized.append(str(selected))
                metadata = os.fstat(child)
            if (
                not _managed_parent_root_is_safe(metadata)
                or stat.S_IMODE(metadata.st_mode) != expected_mode
            ):
                _fail()
            os.fsync(child)
            os.fsync(parent)
        except OSError:
            _fail()
        finally:
            if child >= 0:
                os.close(child)
            os.close(parent)
    return ControllerRuntimeParentRootBootstrapObservation(
        roots=tuple(str(path) for path in FIXED_CONTROLLER_RUNTIME_PARENT_ROOTS),
        created_roots=tuple(created),
        normalized_roots=tuple(normalized),
        root_owned=True,
        nonwritable_by_group_or_world=True,
        opened_no_follow=True,
        directories_fsynced=True,
        parents_fsynced=True,
    )


def _open_directory(path: PurePosixPath) -> int:
    """Open an existing absolute directory one no-follow component at a time."""

    if not path.is_absolute():
        _fail()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            metadata = os.fstat(child)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != 0
                or metadata.st_gid != 0
                or metadata.st_mode & 0o022
            ):
                os.close(child)
                _fail()
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_parent(path: PurePosixPath) -> tuple[int, str]:
    if path.name in {"", ".", ".."}:
        _fail()
    return _open_directory(path.parent), path.name


def _node_from_stat(metadata: os.stat_result, *, sha256: str | None = None) -> NodeObservation:
    kind = (
        "file"
        if stat.S_ISREG(metadata.st_mode)
        else "directory"
        if stat.S_ISDIR(metadata.st_mode)
        else "symlink"
        if stat.S_ISLNK(metadata.st_mode)
        else "special"
    )
    return NodeObservation(
        kind,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        sha256,
        metadata.st_nlink,
    )


def _hash_open_regular(descriptor: int, maximum_bytes: int | None = None) -> tuple[bytes, str]:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink != 1
        or metadata.st_size < 0
        or (maximum_bytes is not None and metadata.st_size > maximum_bytes)
    ):
        _fail()
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    while True:
        block = os.read(descriptor, 1024 * 1024)
        if not block:
            break
        total += len(block)
        if maximum_bytes is not None and total > maximum_bytes:
            _fail()
        digest.update(block)
        if maximum_bytes is not None:
            chunks.append(block)
    if total != metadata.st_size:
        _fail()
    return b"".join(chunks), digest.hexdigest()


@contextmanager
def _tar_archive(path: PurePosixPath):
    parent, name = _open_parent(path)
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o444
        ):
            _fail()
        with os.fdopen(descriptor, "rb", closefd=True) as file_object:
            descriptor = -1
            with tarfile.open(fileobj=file_object, mode="r:gz") as archive:
                yield archive
    except (OSError, tarfile.TarError):
        _fail()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


class LinuxRuntimePublicationPrimitives:
    """Concrete fail-closed implementation of ``RuntimePublicationPrimitives``."""

    def __init__(self) -> None:
        _root_required()

    def bootstrap_fixed_root_directories(
        self,
    ) -> ControllerRuntimeParentRootBootstrapObservation:
        return bootstrap_controller_runtime_parent_roots()

    def observe_no_follow(self, path: str) -> NodeObservation:
        selected = _allowed_path(path)
        try:
            parent, name = _open_parent(selected)
        except FileNotFoundError:
            return NodeObservation("absent")
        try:
            try:
                metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return NodeObservation("absent")
            digest = None
            if stat.S_ISREG(metadata.st_mode):
                flags = os.O_RDONLY | os.O_CLOEXEC
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(name, flags, dir_fd=parent)
                try:
                    opened = os.fstat(descriptor)
                    if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                        _fail()
                    unused, digest = _hash_open_regular(descriptor)
                finally:
                    os.close(descriptor)
            return _node_from_stat(metadata, sha256=digest)
        finally:
            os.close(parent)

    def read_regular_no_follow(self, path: str, maximum_bytes: int) -> bytes:
        selected = _allowed_path(path)
        if type(maximum_bytes) is not int or maximum_bytes < 1:
            _fail()
        parent, name = _open_parent(selected)
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(name, flags, dir_fd=parent)
            try:
                raw, unused = _hash_open_regular(descriptor, maximum_bytes)
                return raw
            finally:
                os.close(descriptor)
        finally:
            os.close(parent)

    def make_directory_no_replace(self, path: str, mode: int) -> None:
        selected = _allowed_path(path)
        if type(mode) is not int or mode not in {0o700}:
            _fail()
        parent, name = _open_parent(selected)
        try:
            os.mkdir(name, mode=mode, dir_fd=parent)
            metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_gid != 0:
                _fail()
        finally:
            os.close(parent)

    def make_parent_directories_no_follow(self, path: str, mode: int) -> None:
        selected = _allowed_path(path)
        if type(mode) is not int or mode != 0o700:
            _fail()
        root = next(root for root in _ALLOWED_ROOTS if selected == root or root in selected.parents)
        descriptor = _open_directory(root)
        try:
            relative = selected.relative_to(root)
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            for part in relative.parts:
                try:
                    child = os.open(part, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    os.mkdir(part, mode=mode, dir_fd=descriptor)
                    child = os.open(part, flags, dir_fd=descriptor)
                metadata = os.fstat(child)
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid != 0
                    or metadata.st_gid != 0
                    or stat.S_IMODE(metadata.st_mode) != mode
                ):
                    os.close(child)
                    _fail()
                os.close(descriptor)
                descriptor = child
        finally:
            os.close(descriptor)

    def write_regular_no_replace(self, path: str, raw: bytes, mode: int) -> None:
        selected = _allowed_path(path)
        if type(raw) is not bytes or type(mode) is not int or mode not in {0o400, 0o500}:
            _fail()
        parent, name = _open_parent(selected)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(name, flags, mode, dir_fd=parent)
            try:
                view = memoryview(raw)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        _fail()
                    view = view[written:]
                os.fchmod(descriptor, mode)
                metadata = os.fstat(descriptor)
                if metadata.st_uid != 0 or metadata.st_gid != 0 or metadata.st_nlink != 1:
                    _fail()
            finally:
                os.close(descriptor)
        finally:
            os.close(parent)

    def copy_regular_no_replace(
        self, source: str, destination: str, expected_sha256: str, expected_size: int, mode: int
    ) -> None:
        source_path = _allowed_path(source)
        destination_path = _allowed_path(destination)
        if (
            type(expected_sha256) is not str
            or _HASH_RE.fullmatch(expected_sha256) is None
            or type(expected_size) is not int
            or expected_size < 0
            or mode != 0o400
        ):
            _fail()
        source_parent, source_name = _open_parent(source_path)
        destination_parent, destination_name = _open_parent(destination_path)
        read_flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        write_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            reader = os.open(source_name, read_flags, dir_fd=source_parent)
            try:
                source_metadata = os.fstat(reader)
                if (
                    not stat.S_ISREG(source_metadata.st_mode)
                    or source_metadata.st_uid != 0
                    or source_metadata.st_gid != 0
                    or source_metadata.st_nlink != 1
                    or source_metadata.st_size != expected_size
                    or stat.S_IMODE(source_metadata.st_mode) != 0o444
                ):
                    _fail()
                writer = os.open(destination_name, write_flags, mode, dir_fd=destination_parent)
                digest = hashlib.sha256()
                total = 0
                try:
                    while True:
                        block = os.read(reader, 1024 * 1024)
                        if not block:
                            break
                        total += len(block)
                        digest.update(block)
                        view = memoryview(block)
                        while view:
                            written = os.write(writer, view)
                            if written <= 0:
                                _fail()
                            view = view[written:]
                    if total != expected_size or digest.hexdigest() != expected_sha256:
                        _fail()
                    os.fchmod(writer, mode)
                    os.fsync(writer)
                finally:
                    os.close(writer)
            finally:
                os.close(reader)
        finally:
            os.close(source_parent)
            os.close(destination_parent)

    def _remove_owned(self, selected: PurePosixPath) -> None:
        node = self.observe_no_follow(str(selected))
        if node.kind == "absent":
            _fail()
        if node.uid != 0 or node.gid != 0 or node.kind not in {"file", "directory"}:
            _fail()
        if node.kind == "file":
            if node.nlink != 1:
                _fail()
            parent, name = _open_parent(selected)
            try:
                os.unlink(name, dir_fd=parent)
            finally:
                os.close(parent)
            return
        descriptor = _open_directory(selected)
        try:
            entries = sorted(os.scandir(descriptor), key=lambda item: item.name)
        finally:
            os.close(descriptor)
        for entry in entries:
            self._remove_owned(selected / entry.name)
        parent, name = _open_parent(selected)
        try:
            os.rmdir(name, dir_fd=parent)
        finally:
            os.close(parent)

    def remove_owned_tree_no_follow(self, path: str) -> None:
        selected = _allowed_path(path)
        staging = PurePosixPath(
            "/var/lib/governed-memory-controller/build-staging"
        )
        relative = str(selected.relative_to(staging)) if staging in selected.parents else ""
        first = relative.split("/", 1)[0] if relative else ""
        stage_hash = first.removesuffix(".create-intent.json")
        build_stage = (
            _HASH_RE.fullmatch(stage_hash) is not None
            and (
                selected == staging / stage_hash
                or selected == staging / (stage_hash + ".create-intent.json")
            )
        )
        offline_stage = (
            re.fullmatch(
                r"/var/lib/governed-memory-controller/offline/"
                r"(?:cpython|wheelhouses)/\.[0-9a-f]{64}\.staging",
                str(selected),
                re.ASCII,
            )
            is not None
        )
        if not (build_stage or offline_stage):
            _fail()
        self._remove_owned(selected)

    def unlink_regular_no_follow(self, path: str, expected_sha256: str) -> None:
        selected = _allowed_path(path)
        if type(expected_sha256) is not str or _HASH_RE.fullmatch(expected_sha256) is None:
            _fail()
        parent, name = _open_parent(selected)
        flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=parent)
            try:
                metadata = os.fstat(descriptor)
                unused, digest = _hash_open_regular(descriptor)
                if (
                    digest != expected_sha256
                    or metadata.st_uid != 0
                    or metadata.st_gid != 0
                    or metadata.st_nlink != 1
                    or stat.S_IMODE(metadata.st_mode) != 0o400
                ):
                    _fail()
                before = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (before.st_dev, before.st_ino) != (metadata.st_dev, metadata.st_ino):
                    _fail()
                os.unlink(name, dir_fd=parent)
            finally:
                os.close(descriptor)
        finally:
            os.close(parent)

    def remove_empty_directory_no_follow(self, path: str) -> None:
        selected = _allowed_path(path)
        parent, name = _open_parent(selected)
        try:
            metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != 0
                or metadata.st_gid != 0
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                _fail()
            os.rmdir(name, dir_fd=parent)
        finally:
            os.close(parent)

    def directory_device_no_follow(self, path: str) -> int:
        descriptor = _open_directory(_allowed_path(path))
        try:
            return os.fstat(descriptor).st_dev
        finally:
            os.close(descriptor)

    def list_archive_members(self, archive_path: str) -> tuple[ArchiveMember, ...]:
        selected = _allowed_path(archive_path)
        if not selected.name.endswith(".tar.gz"):
            _fail()
        members: list[ArchiveMember] = []
        total = 0
        try:
            with _tar_archive(selected) as archive:
                for member in archive:
                    if len(members) >= _MAX_ARCHIVE_MEMBERS:
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
                    members.append(
                        ArchiveMember(
                            member.name.rstrip("/") if member.isdir() else member.name,
                            kind,
                            member.mode & 0o777,
                            size,
                            member.linkname if kind == "symlink" else None,
                        )
                    )
        except (tarfile.TarError, OSError):
            _fail()
        return tuple(members)

    def extract_regular_members_no_follow(
        self,
        archive_path: str,
        extractions: tuple[ArchiveExtraction, ...],
    ) -> None:
        archive_selected = _allowed_path(archive_path)
        if (
            type(extractions) is not tuple
            or not extractions
            or len(extractions) > _MAX_ARCHIVE_MEMBERS
            or any(type(item) is not ArchiveExtraction for item in extractions)
        ):
            _fail()
        destinations: set[str] = set()
        selected_extractions: list[tuple[ArchiveExtraction, PurePosixPath]] = []
        for item in extractions:
            destination = _allowed_path(item.destination_path)
            if (
                type(item.source_member_path) is not str
                or not item.source_member_path
                or "\x00" in item.source_member_path
                or item.mode not in {0o600, 0o700}
                or item.destination_path in destinations
            ):
                _fail()
            destinations.add(item.destination_path)
            selected_extractions.append((item, destination))
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0)
        )
        with _tar_archive(archive_selected) as archive:
            members: dict[str, tarfile.TarInfo] = {}
            for member in archive:
                if member.name in members:
                    _fail()
                members[member.name] = member
            ordered: list[tuple[tarfile.TarInfo, ArchiveExtraction, PurePosixPath]] = []
            for extraction, destination in selected_extractions:
                source_member = members.get(extraction.source_member_path)
                if source_member is None or not source_member.isreg():
                    _fail()
                ordered.append((source_member, extraction, destination))
            ordered.sort(key=lambda item: (item[0].offset_data, item[2].as_posix()))
            for source_member, extraction, destination in ordered:
                parent, name = _open_parent(destination)
                try:
                    descriptor = os.open(
                        name, flags, extraction.mode, dir_fd=parent
                    )
                    try:
                        source = archive.extractfile(source_member)
                        if source is None:
                            _fail()
                        total = 0
                        while True:
                            block = source.read(1024 * 1024)
                            if not block:
                                break
                            total += len(block)
                            if total > source_member.size:
                                _fail()
                            view = memoryview(block)
                            while view:
                                written = os.write(descriptor, view)
                                if written <= 0:
                                    _fail()
                                view = view[written:]
                        if total != source_member.size:
                            _fail()
                        os.fchmod(descriptor, extraction.mode)
                    finally:
                        os.close(descriptor)
                finally:
                    os.close(parent)

    def observe_tree_no_follow(self, root: str) -> tuple[TreeMember, ...]:
        selected = _allowed_path(root)
        descriptor = _open_directory(selected)
        os.close(descriptor)
        result: list[TreeMember] = []

        def walk(directory: PurePosixPath, relative: PurePosixPath) -> None:
            directory_fd = _open_directory(directory)
            try:
                entries = sorted(os.scandir(directory_fd), key=lambda item: item.name)
            finally:
                os.close(directory_fd)
            for entry in entries:
                path = directory / entry.name
                rendered = str(relative / entry.name)
                metadata = os.lstat(path)
                kind = (
                    "directory"
                    if stat.S_ISDIR(metadata.st_mode)
                    else "file"
                    if stat.S_ISREG(metadata.st_mode)
                    else "symlink"
                    if stat.S_ISLNK(metadata.st_mode)
                    else "special"
                )
                digest = None
                if kind == "file":
                    parent, name = _open_parent(path)
                    try:
                        file_fd = os.open(
                            name,
                            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                            dir_fd=parent,
                        )
                        try:
                            opened = os.fstat(file_fd)
                            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                                _fail()
                            unused, digest = _hash_open_regular(file_fd)
                        finally:
                            os.close(file_fd)
                    finally:
                        os.close(parent)
                result.append(
                    TreeMember(
                        rendered,
                        kind,
                        stat.S_IMODE(metadata.st_mode),
                        metadata.st_uid,
                        metadata.st_gid,
                        metadata.st_size if kind == "file" else 0,
                        digest,
                        metadata.st_nlink,
                    )
                )
                if kind == "directory":
                    walk(path, relative / entry.name)

        walk(selected, PurePosixPath())
        return tuple(result)

    def seal_tree_root_owned(
        self, root: str, directory_mode: int, regular_mode: int, executable_mode: int
    ) -> None:
        selected = _allowed_path(root)
        if (directory_mode, regular_mode, executable_mode) != (0o555, 0o444, 0o555):
            _fail()
        members = self.observe_tree_no_follow(str(selected))
        for member in sorted(
            members,
            key=lambda item: len(PurePosixPath(item.path).parts),
            reverse=True,
        ):
            if member.kind not in {"directory", "file"} or member.uid != 0 or member.gid != 0:
                _fail()
            if member.kind == "file" and member.nlink != 1:
                _fail()
            target = selected / member.path
            os.chmod(
                target,
                directory_mode
                if member.kind == "directory"
                else executable_mode
                if member.mode & 0o100
                else regular_mode,
                follow_symlinks=False,
            )
        os.chmod(selected, directory_mode, follow_symlinks=False)

    def fsync_tree(self, root: str) -> None:
        selected = _allowed_path(root)
        members = self.observe_tree_no_follow(str(selected))
        for member in members:
            target = selected / member.path
            flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
            if member.kind == "directory":
                flags |= os.O_DIRECTORY
            elif member.kind != "file":
                _fail()
            descriptor = os.open(target, flags)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        descriptor = _open_directory(selected)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _argv_allowed(argv: tuple[str, ...]) -> bool:
        if (
            type(argv) is not tuple
            or len(argv) < 6
            or any(type(value) is not str or not value or "\x00" in value for value in argv)
            or _STAGED_INTERPRETER_RE.fullmatch(argv[0]) is None
            or argv[1:3] != ("-I", "-B")
        ):
            return False
        if argv[3:5] == ("-m", "pip"):
            expected_prefix = (
                "install",
                "--no-index",
                "--disable-pip-version-check",
                "--no-compile",
                "--no-deps",
                "--require-hashes",
                "--only-binary=:all:",
                "--find-links",
            )
            if len(argv) != 16 or argv[5:13] != expected_prefix or argv[14] != "--requirement":
                return False
            plan_hash = PurePosixPath(argv[0]).parts[-4]
            return (
                PurePosixPath(argv[13]).parent
                == PurePosixPath(
                    "/var/lib/governed-memory-controller/offline/wheelhouses"
                )
                and _HASH_RE.fullmatch(PurePosixPath(argv[13]).name) is not None
                and argv[15]
                == (
                    "/var/lib/governed-memory-controller/build-staging/"
                    f"{plan_hash}/.controller-requirements.lock"
                )
            )
        if argv[3] == "-c":
            return (
                len(argv) == 6
                and argv[4]
                in {
                    _REMOVE_BOOTSTRAP,
                    _SUBSTRATE_CONFINEMENT_PROBE,
                    _INVENTORY_PROBE,
                }
                and argv[5]
                == str(PurePosixPath(argv[0]).parents[1])
            )
        launcher_match = re.fullmatch(
            r"/var/lib/governed-memory-controller/build-staging/"
            r"([0-9a-f]{64})/release/tools/governed_memory_install/"
            r"store_supervisor_launcher\.py",
            argv[3],
            re.ASCII,
        ) if len(argv) >= 4 else None
        return (
            len(argv) == 7
            and launcher_match is not None
            and launcher_match.group(1) == PurePosixPath(argv[0]).parts[-4]
            and argv[4] == "--package-manifest-sha256"
            and _HASH_RE.fullmatch(argv[5]) is not None
            and argv[6] == "--help"
        )

    def run_exact(
        self, argv: tuple[str, ...], environment: Mapping[str, str], cwd: str
    ) -> ProcessResult:
        if dict(environment) != _FIXED_ENVIRONMENT or cwd != "/" or not self._argv_allowed(argv):
            _fail()
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
        )
        if process.stdout is None or process.stderr is None:
            process.kill()
            process.wait()
            _fail()
        selector = selectors.DefaultSelector()
        streams = {"stdout": bytearray(), "stderr": bytearray()}
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        deadline = time.monotonic() + _PROCESS_TIMEOUT_SECONDS
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _fail()
                events = selector.select(remaining)
                if not events:
                    _fail()
                for key, unused in events:
                    block = os.read(key.fileobj.fileno(), 8192)
                    if not block:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    streams[key.data].extend(block)
                    if sum(len(value) for value in streams.values()) > _MAX_PROCESS_OUTPUT:
                        _fail()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _fail()
            try:
                returncode = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                _fail()
            return ProcessResult(returncode, bytes(streams["stdout"]), bytes(streams["stderr"]))
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait()
            raise
        finally:
            selector.close()

    def rename_no_replace(self, source: str, destination: str) -> None:
        source_path = _allowed_path(source)
        destination_path = _allowed_path(destination)
        source_text = str(source_path)
        destination_text = str(destination_path)
        runtime_pair = (
            re.fullmatch(
                r"/var/lib/governed-memory-controller/build-staging/"
                r"[0-9a-f]{64}/runtime",
                source_text,
                re.ASCII,
            )
            is not None
            and re.fullmatch(
                r"/opt/governed-memory-controller/runtimes/[0-9a-f]{64}",
                destination_text,
                re.ASCII,
            )
            is not None
        )
        release_pair = (
            re.fullmatch(
                r"/var/lib/governed-memory-controller/build-staging/"
                r"[0-9a-f]{64}/release",
                source_text,
                re.ASCII,
            )
            is not None
            and re.fullmatch(
                r"/opt/governed-memory-controller/releases/[0-9a-f]{64}",
                destination_text,
                re.ASCII,
            )
            is not None
        )
        offline_pair = (
            re.fullmatch(
                r"/var/lib/governed-memory-controller/offline/(?:cpython|wheelhouses)/"
                r"\.[0-9a-f]{64}\.staging",
                source_text,
                re.ASCII,
            )
            is not None
            and re.fullmatch(
                r"/var/lib/governed-memory-controller/offline/(?:cpython|wheelhouses)/"
                r"[0-9a-f]{64}",
                destination_text,
                re.ASCII,
            )
            is not None
            and source_path.parent == destination_path.parent
            and source_path.name == f".{destination_path.name}.staging"
        )
        if not (runtime_pair or release_pair or offline_pair):
            _fail()
        source_parent, source_name = _open_parent(source_path)
        destination_parent, destination_name = _open_parent(destination_path)
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            renameat2 = getattr(libc, "renameat2", None)
            if renameat2 is None:
                _fail()
            renameat2.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renameat2.restype = ctypes.c_int
            result = renameat2(
                source_parent,
                os.fsencode(source_name),
                destination_parent,
                os.fsencode(destination_name),
                _RENAME_NOREPLACE,
            )
            if result != 0:
                error_number = ctypes.get_errno()
                if error_number == errno.EEXIST:
                    raise FileExistsError(
                        error_number,
                        os.strerror(error_number),
                        str(destination_path),
                    )
                raise OSError(error_number, os.strerror(error_number))
        finally:
            os.close(source_parent)
            os.close(destination_parent)

    def fsync_file(self, path: str) -> None:
        selected = _allowed_path(path)
        parent, name = _open_parent(selected)
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent,
            )
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != 0
                    or metadata.st_gid != 0
                    or metadata.st_nlink != 1
                ):
                    _fail()
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            os.close(parent)

    def fsync_parent(self, path: str) -> None:
        selected = _allowed_path(path)
        descriptor = _open_directory(selected.parent)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


__all__ = [
    "ControllerRuntimeParentRootBootstrapObservation",
    "FIXED_CONTROLLER_RUNTIME_PARENT_LAYOUT",
    "FIXED_CONTROLLER_RUNTIME_PARENT_ROOTS",
    "LinuxRuntimePublicationPrimitiveError",
    "LinuxRuntimePublicationPrimitives",
    "bootstrap_controller_runtime_parent_roots",
]
