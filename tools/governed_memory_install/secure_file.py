from __future__ import annotations

"""Read one root authority file through stable no-follow descriptors."""

import os
from pathlib import Path
import stat


MAX_AUTHORITY_FILE_BYTES = 4 * 1024 * 1024


class SecureFileError(RuntimeError):
    """Content-free refusal for unsafe root-owned authority input."""


def _nofollow() -> int:
    value = getattr(os, "O_NOFOLLOW", 0)
    if value == 0:
        raise SecureFileError("secure_file_nofollow_unavailable")
    return value


def read_root_owned_regular_file(
    path: Path,
    *,
    expected_file_mode: int,
    expected_parent_mode: int,
    expected_uid: int = 0,
    max_bytes: int = MAX_AUTHORITY_FILE_BYTES,
) -> bytes:
    path = Path(path)
    if not path.is_absolute() or not path.name or path.name in {".", ".."}:
        raise SecureFileError("secure_file_path_invalid")
    directory_fd = file_fd = -1
    try:
        flags = os.O_RDONLY | os.O_CLOEXEC | _nofollow()
        directory_fd = os.open(
            path.parent,
            flags | getattr(os, "O_DIRECTORY", 0),
        )
        directory = os.fstat(directory_fd)
        named_directory = path.parent.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(directory.st_mode)
            or not stat.S_ISDIR(named_directory.st_mode)
            or stat.S_IMODE(directory.st_mode) != expected_parent_mode
            or directory.st_uid != expected_uid
            or (directory.st_dev, directory.st_ino)
            != (named_directory.st_dev, named_directory.st_ino)
        ):
            raise SecureFileError("secure_file_parent_invalid")
        file_fd = os.open(path.name, flags, dir_fd=directory_fd)
        before = os.fstat(file_fd)
        named = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or stat.S_IMODE(before.st_mode) != expected_file_mode
            or before.st_uid != expected_uid
            or before.st_nlink != 1
            or before.st_size > max_bytes
            or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise SecureFileError("secure_file_invalid")
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(file_fd, min(64 * 1024, max_bytes + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > max_bytes:
                raise SecureFileError("secure_file_too_large")
        after = os.fstat(file_fd)
        named_after = os.stat(
            path.name, dir_fd=directory_fd, follow_symlinks=False
        )
        if (
            total != before.st_size
            or (after.st_dev, after.st_ino, after.st_size)
            != (before.st_dev, before.st_ino, before.st_size)
            or (named_after.st_dev, named_after.st_ino)
            != (before.st_dev, before.st_ino)
        ):
            raise SecureFileError("secure_file_changed_during_read")
        return b"".join(chunks)
    except OSError as error:
        raise SecureFileError("secure_file_open_failed") from error
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if directory_fd >= 0:
            os.close(directory_fd)
