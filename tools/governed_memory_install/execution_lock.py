from __future__ import annotations

"""Secure, nonblocking process lock for a future installation executor.

This module owns no command-line entry point and performs no installation
work.  It only serializes a caller that has already established authority.
"""

import errno
import fcntl
import os
from pathlib import Path
import stat


class ExecutionLockError(RuntimeError):
    """Base class for content-free lock failures."""


class ExecutionLockSecurityError(ExecutionLockError):
    """The lock directory, path, or open file is unsafe."""


class ExecutionLockBusyError(ExecutionLockError):
    """Another process currently owns the global execution lock."""


_HELD_CAPABILITY_TOKEN = object()


class HeldExecutionLockCapability:
    """Opaque proof that one exact :class:`GlobalExecutionLock` is still held.

    Construction is restricted to ``GlobalExecutionLock.held_capability``.
    Consumers must call :func:`validate_held_execution_lock` immediately before
    every protected state or journal access; retaining this object after the
    owning lock closes does not retain authority.
    """

    __slots__ = ("_owner", "_token")

    def __init__(self, owner: GlobalExecutionLock, token: object) -> None:
        if token is not _HELD_CAPABILITY_TOKEN:
            raise ExecutionLockSecurityError("execution_lock_capability_invalid")
        self._owner = owner
        self._token = token

    def __repr__(self) -> str:
        return "HeldExecutionLockCapability(<validated-on-use>)"


def validate_held_execution_lock(value: object) -> None:
    """Validate an opaque held-lock capability without accepting duck types."""

    if (
        type(value) is not HeldExecutionLockCapability
        or value._token is not _HELD_CAPABILITY_TOKEN
        or type(value._owner) is not GlobalExecutionLock
    ):
        raise ExecutionLockSecurityError("execution_lock_capability_invalid")
    value._owner.validate()


class GlobalExecutionLock:
    """Exclusive, nonblocking flock with strict filesystem invariants.

    Production callers should use a root-owned ``0700`` directory and leave
    ``expected_uid`` unset while running as root.  Tests can use the same
    invariants under a temporary directory owned by their effective UID.
    The lock file is always an empty regular file with mode ``0600`` and one
    hard link.  The open descriptor and named path must retain the same inode.
    """

    def __init__(
        self,
        path: Path,
        *,
        expected_uid: int | None = None,
        expected_gid: int | None = None,
    ) -> None:
        self.path = Path(path)
        self.expected_uid = os.geteuid() if expected_uid is None else expected_uid
        self.expected_gid = os.getegid() if expected_gid is None else expected_gid
        if (
            not isinstance(self.expected_uid, int)
            or isinstance(self.expected_uid, bool)
            or self.expected_uid < 0
            or not isinstance(self.expected_gid, int)
            or isinstance(self.expected_gid, bool)
            or self.expected_gid < 0
        ):
            raise ExecutionLockSecurityError("execution_lock_uid_invalid")
        if not self.path.name or self.path.name in {".", ".."}:
            raise ExecutionLockSecurityError("execution_lock_path_invalid")
        self._directory_fd = -1
        self._fd = -1
        self._inode: tuple[int, int] | None = None
        self._unlock_on_close = True
        self._acquire()

    @staticmethod
    def _nofollow_flag() -> int:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if nofollow == 0:
            raise ExecutionLockSecurityError("execution_lock_nofollow_unavailable")
        return nofollow

    def _open_directory(self) -> None:
        flags = os.O_RDONLY | os.O_CLOEXEC | self._nofollow_flag()
        flags |= getattr(os, "O_DIRECTORY", 0)
        try:
            self._directory_fd = os.open(self.path.parent, flags)
        except OSError as error:
            raise ExecutionLockSecurityError(
                "execution_lock_directory_invalid"
            ) from error
        try:
            opened = os.fstat(self._directory_fd)
            named = self.path.parent.stat(follow_symlinks=False)
        except OSError as error:
            raise ExecutionLockSecurityError(
                "execution_lock_directory_invalid"
            ) from error
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o700
            or stat.S_IMODE(named.st_mode) != 0o700
            or opened.st_uid != self.expected_uid
            or named.st_uid != self.expected_uid
            or opened.st_gid != self.expected_gid
            or named.st_gid != self.expected_gid
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise ExecutionLockSecurityError("execution_lock_directory_invalid")

    def _acquire(self) -> None:
        try:
            self._open_directory()
            flags = os.O_RDWR | os.O_CLOEXEC | self._nofollow_flag()
            created = False
            try:
                self._fd = os.open(
                    self.path.name,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=self._directory_fd,
                )
                created = True
            except FileExistsError:
                try:
                    self._fd = os.open(
                        self.path.name,
                        flags,
                        dir_fd=self._directory_fd,
                    )
                except OSError as error:
                    raise ExecutionLockSecurityError(
                        "execution_lock_file_invalid"
                    ) from error
            except OSError as error:
                raise ExecutionLockSecurityError(
                    "execution_lock_file_invalid"
                ) from error

            if created:
                os.fchown(self._fd, self.expected_uid, self.expected_gid)
                os.fchmod(self._fd, 0o600)
                os.fsync(self._fd)
                os.fsync(self._directory_fd)

            self._validate_open_file()
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN}:
                    raise ExecutionLockBusyError("execution_lock_busy") from error
                raise ExecutionLockSecurityError(
                    "execution_lock_acquire_failed"
                ) from error
            self._validate_open_file()
        except BaseException:
            self.close()
            raise

    def _validate_open_file(self) -> None:
        if self._fd < 0 or self._directory_fd < 0:
            raise ExecutionLockSecurityError("execution_lock_not_open")
        try:
            opened = os.fstat(self._fd)
            named = os.stat(
                self.path.name,
                dir_fd=self._directory_fd,
                follow_symlinks=False,
            )
            directory = os.fstat(self._directory_fd)
            named_directory = self.path.parent.stat(follow_symlinks=False)
        except OSError as error:
            raise ExecutionLockSecurityError(
                "execution_lock_file_invalid"
            ) from error
        if (
            not stat.S_ISDIR(directory.st_mode)
            or not stat.S_ISDIR(named_directory.st_mode)
            or stat.S_IMODE(directory.st_mode) != 0o700
            or directory.st_uid != self.expected_uid
            or directory.st_gid != self.expected_gid
            or stat.S_IMODE(named_directory.st_mode) != 0o700
            or named_directory.st_uid != self.expected_uid
            or named_directory.st_gid != self.expected_gid
            or (directory.st_dev, directory.st_ino)
            != (named_directory.st_dev, named_directory.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_uid != self.expected_uid
            or opened.st_gid != self.expected_gid
            or stat.S_IMODE(named.st_mode) != 0o600
            or named.st_uid != self.expected_uid
            or named.st_gid != self.expected_gid
            or opened.st_nlink != 1
            or opened.st_size != 0
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise ExecutionLockSecurityError("execution_lock_file_invalid")
        inode = (opened.st_dev, opened.st_ino)
        if self._inode is not None and inode != self._inode:
            raise ExecutionLockSecurityError("execution_lock_path_replaced")
        self._inode = inode

    def validate(self) -> None:
        """Fail closed if the directory or named lock changed after acquire."""

        self._validate_open_file()

    def held_capability(self) -> HeldExecutionLockCapability:
        """Return an opaque capability whose validity follows this lock."""

        self.validate()
        return HeldExecutionLockCapability(self, _HELD_CAPABILITY_TOKEN)

    @property
    def descriptor(self) -> int:
        """Return the validated open lock descriptor for fixed-FD inheritance."""

        self.validate()
        return self._fd

    def retain_across_inherited_processes(self) -> None:
        """Release by last close, never by an explicit unlock from this owner.

        A supervisor calls this before sharing the open file description with
        a child process group.  If the supervisor exits while a child remains,
        its close cannot unlock the shared flock out from under that child.
        """

        self.validate()
        self._unlock_on_close = False

    @classmethod
    def from_inherited_descriptor(
        cls,
        path: Path,
        descriptor: int,
        *,
        expected_uid: int | None = None,
        expected_gid: int | None = None,
    ) -> GlobalExecutionLock:
        """Adopt an already locked open file description without relocking it."""

        if type(descriptor) is not int or descriptor < 0:
            raise ExecutionLockSecurityError(
                "execution_lock_inherited_descriptor_invalid"
            )
        selected = object.__new__(cls)
        selected.path = Path(path)
        selected.expected_uid = (
            os.geteuid() if expected_uid is None else expected_uid
        )
        selected.expected_gid = (
            os.getegid() if expected_gid is None else expected_gid
        )
        if (
            not isinstance(selected.expected_uid, int)
            or isinstance(selected.expected_uid, bool)
            or selected.expected_uid < 0
            or not isinstance(selected.expected_gid, int)
            or isinstance(selected.expected_gid, bool)
            or selected.expected_gid < 0
        ):
            raise ExecutionLockSecurityError("execution_lock_uid_invalid")
        selected._directory_fd = -1
        selected._fd = -1
        selected._inode = None
        selected._unlock_on_close = False
        try:
            selected._open_directory()
            selected._fd = os.dup(descriptor)
            selected._validate_open_file()
            independent = -1
            try:
                independent = os.open(
                    selected.path.name,
                    os.O_RDWR
                    | os.O_CLOEXEC
                    | selected._nofollow_flag(),
                    dir_fd=selected._directory_fd,
                )
                try:
                    fcntl.flock(
                        independent,
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                except BlockingIOError:
                    pass
                else:
                    fcntl.flock(independent, fcntl.LOCK_UN)
                    raise ExecutionLockSecurityError(
                        "execution_lock_inherited_descriptor_not_locked"
                    )
                try:
                    fcntl.flock(
                        selected._fd,
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                except (BlockingIOError, OSError) as error:
                    raise ExecutionLockSecurityError(
                        "execution_lock_inherited_descriptor_not_locked"
                    ) from error
            except ExecutionLockSecurityError:
                raise
            except OSError as error:
                raise ExecutionLockSecurityError(
                    "execution_lock_inherited_descriptor_not_locked"
                ) from error
            finally:
                if independent >= 0:
                    os.close(independent)
            selected._validate_open_file()
            return selected
        except BaseException:
            selected.close()
            raise

    def close(self) -> None:
        if self._fd >= 0:
            try:
                if self._unlock_on_close:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = -1
        if self._directory_fd >= 0:
            os.close(self._directory_fd)
            self._directory_fd = -1

    def __enter__(self) -> GlobalExecutionLock:
        return self

    def __exit__(self, *unused: object) -> None:
        self.close()
