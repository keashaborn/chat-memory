from __future__ import annotations

"""Read one root authority file through stable no-follow descriptors."""

import os
from pathlib import Path
import stat
from dataclasses import dataclass
import re
from typing import Protocol


MAX_AUTHORITY_FILE_BYTES = 4 * 1024 * 1024


class SecureFileError(RuntimeError):
    """Content-free refusal for unsafe root-owned authority input."""


_SECRET_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{2,95}\Z", re.ASCII)
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


@dataclass(frozen=True, slots=True)
class SecretWriteRequest:
    """Exact destination metadata; secret bytes are deliberately separate."""

    secret_name: str
    destination: Path
    expected_parent_mode: int = 0o700
    expected_file_mode: int = 0o600
    expected_uid: int = 0

    def __post_init__(self) -> None:
        if (
            _SECRET_NAME_RE.fullmatch(self.secret_name) is None
            or not self.destination.is_absolute()
            or self.destination.name in {"", ".", ".."}
            or self.expected_parent_mode != 0o700
            or self.expected_file_mode != 0o600
            or type(self.expected_uid) is not int
            or self.expected_uid < 0
        ):
            raise SecureFileError("secret_write_request_invalid")


@dataclass(frozen=True, slots=True)
class SecretReadiness:
    secret_name: str
    destination_sha256: str
    content_sha256: str
    root_owned: bool
    parent_mode: int
    file_mode: int

    def __post_init__(self) -> None:
        if (
            _SECRET_NAME_RE.fullmatch(self.secret_name) is None
            or _HASH_RE.fullmatch(self.destination_sha256) is None
            or _HASH_RE.fullmatch(self.content_sha256) is None
            or self.root_owned is not True
            or self.parent_mode != 0o700
            or self.file_mode != 0o600
        ):
            raise SecureFileError("secret_readiness_invalid")

class SecretMaterialSource(Protocol):
    def generate(self, secret_name: str) -> bytes: ...


class SecretFileWriter(Protocol):
    def write(
        self, request: SecretWriteRequest, secret_value: bytes
    ) -> SecretReadiness: ...


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
