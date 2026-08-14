#!/usr/bin/env python3
from __future__ import annotations

"""Create or verify the two fixed retained Phase 9 substrate directories."""

import sys


_ISOLATED_RUNTIME_AT_START = bool(sys.flags.isolated)
_DONT_WRITE_BYTECODE_AT_START = bool(sys.dont_write_bytecode)
if __name__ == "__main__" and not (
    _ISOLATED_RUNTIME_AT_START and _DONT_WRITE_BYTECODE_AT_START
):
    sys.stderr.write("phase9_store_substrate_runtime_isolation_required\n")
    raise SystemExit(1)

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Final


STORE_PARENT_PATH: Final = Path("/etc/governed-memory-stores")
STORE_TARGET_LEAF: Final = "9a54cf123493-000002"
STORE_TARGET_PATH: Final = STORE_PARENT_PATH / STORE_TARGET_LEAF
EXECUTION_PARENT_PATH: Final = Path("/var/lib/governed-memory-controller")
EXECUTION_TARGET_LEAF: Final = "executions-v2"
EXECUTION_TARGET_PATH: Final = EXECUTION_PARENT_PATH / EXECUTION_TARGET_LEAF
ROOT_UID: Final = 0
ROOT_GID: Final = 0
STORE_PARENT_MODE: Final = 0o755
EXECUTION_PARENT_MODE: Final = 0o700
TARGET_MODE: Final = 0o700
RESULT_SCHEMA: Final = "governed-memory-phase9-disposable-store-substrate-v1"


class Phase9DisposableStoreSubstrateError(RuntimeError):
    """Content-free refusal from the fixed substrate bootstrap."""


@dataclass(frozen=True, slots=True)
class _FixedDirectory:
    parent: Path
    parent_mode: int
    leaf: str
    target: Path


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as error:
        raise Phase9DisposableStoreSubstrateError(
            "phase9_store_substrate_result_invalid"
        ) from error


def _directory_is_exact(
    opened: os.stat_result,
    named: os.stat_result,
    *,
    mode: int,
) -> bool:
    return (
        stat.S_ISDIR(opened.st_mode)
        and stat.S_IMODE(opened.st_mode) == mode
        and opened.st_uid == ROOT_UID
        and opened.st_gid == ROOT_GID
        and (opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino)
    )


def _directory_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if nofollow == 0 or directory == 0:
        raise Phase9DisposableStoreSubstrateError(
            "phase9_store_substrate_platform_unsupported"
        )
    return os.O_RDONLY | os.O_CLOEXEC | nofollow | directory


def _directory_is_empty(descriptor: int, flags: int) -> bool:
    """Read through a fresh open description so replay checks never reuse offsets."""

    probe = -1
    try:
        probe = os.open(".", flags, dir_fd=descriptor)
        opened = os.fstat(descriptor)
        observed = os.fstat(probe)
        return (
            (opened.st_dev, opened.st_ino) == (observed.st_dev, observed.st_ino)
            and os.listdir(probe) == []
        )
    finally:
        if probe >= 0:
            os.close(probe)


def _verify_fixed_identity() -> None:
    store_parent = PurePosixPath("/etc/governed-memory-stores")
    store_target = store_parent / "9a54cf123493-000002"
    execution_parent = PurePosixPath("/var/lib/governed-memory-controller")
    execution_target = execution_parent / "executions-v2"
    if (
        PurePosixPath(STORE_PARENT_PATH) != store_parent
        or STORE_TARGET_LEAF != store_target.name
        or PurePosixPath(STORE_TARGET_PATH) != store_target
        or STORE_TARGET_PATH.parent != STORE_PARENT_PATH
        or STORE_TARGET_PATH.name != STORE_TARGET_LEAF
        or STORE_PARENT_MODE != 0o755
        or PurePosixPath(EXECUTION_PARENT_PATH) != execution_parent
        or EXECUTION_TARGET_LEAF != execution_target.name
        or PurePosixPath(EXECUTION_TARGET_PATH) != execution_target
        or EXECUTION_TARGET_PATH.parent != EXECUTION_PARENT_PATH
        or EXECUTION_TARGET_PATH.name != EXECUTION_TARGET_LEAF
        or EXECUTION_PARENT_MODE != 0o700
        or TARGET_MODE != 0o700
    ):
        raise Phase9DisposableStoreSubstrateError(
            "phase9_store_substrate_identity_invalid"
        )


def _fixed_directories() -> tuple[_FixedDirectory, _FixedDirectory]:
    return (
        _FixedDirectory(
            STORE_PARENT_PATH,
            STORE_PARENT_MODE,
            STORE_TARGET_LEAF,
            STORE_TARGET_PATH,
        ),
        _FixedDirectory(
            EXECUTION_PARENT_PATH,
            EXECUTION_PARENT_MODE,
            EXECUTION_TARGET_LEAF,
            EXECUTION_TARGET_PATH,
        ),
    )


def _bootstrap_directories(
    selected: tuple[_FixedDirectory, ...],
) -> tuple[tuple[str, str], ...]:
    """Open all parents and preflight all existing leaves before creation."""

    if (
        not selected
        or len({item.target for item in selected}) != len(selected)
        or any(
            type(item) is not _FixedDirectory
            or item.target.parent != item.parent
            or item.target.name != item.leaf
            or PurePosixPath(item.leaf).name != item.leaf
            or item.parent_mode not in {0o700, 0o755}
            for item in selected
        )
    ):
        raise Phase9DisposableStoreSubstrateError(
            "phase9_store_substrate_identity_invalid"
        )
    flags = _directory_flags()
    parents: list[tuple[_FixedDirectory, int, os.stat_result]] = []
    targets: list[tuple[_FixedDirectory, int, bool, os.stat_result]] = []
    results: list[tuple[str, str]] = []
    untracked_target_fd = -1
    try:
        # Do not create either leaf until both parents and all existing leaves pass.
        for item in selected:
            parent_fd = os.open(item.parent, flags)
            parent_opened = os.fstat(parent_fd)
            parent_named = item.parent.stat(follow_symlinks=False)
            if not _directory_is_exact(
                parent_opened, parent_named, mode=item.parent_mode
            ):
                raise OSError(errno.EPERM, "parent")
            parents.append((item, parent_fd, parent_opened))
            try:
                untracked_target_fd = os.open(
                    item.leaf, flags, dir_fd=parent_fd
                )
            except FileNotFoundError:
                continue
            target_opened = os.fstat(untracked_target_fd)
            target_named = os.stat(
                item.leaf, dir_fd=parent_fd, follow_symlinks=False
            )
            if (
                not _directory_is_exact(
                    target_opened, target_named, mode=TARGET_MODE
                )
                or not _directory_is_empty(untracked_target_fd, flags)
            ):
                raise OSError(errno.EPERM, "target")
            targets.append((item, untracked_target_fd, False, target_opened))
            untracked_target_fd = -1

        open_target_paths = {item.target for item, _, _, _ in targets}
        for item, parent_fd, _ in parents:
            if item.target in open_target_paths:
                continue
            try:
                os.mkdir(item.leaf, TARGET_MODE, dir_fd=parent_fd)
            except FileExistsError:
                created = False
            else:
                created = True
            untracked_target_fd = os.open(
                item.leaf, flags, dir_fd=parent_fd
            )
            if created:
                os.fchown(untracked_target_fd, ROOT_UID, ROOT_GID)
                os.fchmod(untracked_target_fd, TARGET_MODE)
            target_opened = os.fstat(untracked_target_fd)
            target_named = os.stat(
                item.leaf, dir_fd=parent_fd, follow_symlinks=False
            )
            if (
                not _directory_is_exact(
                    target_opened, target_named, mode=TARGET_MODE
                )
                or not _directory_is_empty(untracked_target_fd, flags)
            ):
                raise OSError(errno.EPERM, "target")
            targets.append((item, untracked_target_fd, created, target_opened))
            untracked_target_fd = -1

        target_by_path = {
            item.target: (target_fd, created, opened)
            for item, target_fd, created, opened in targets
        }
        for item, parent_fd, parent_before in parents:
            target_fd, created, target_before = target_by_path[item.target]
            os.fsync(target_fd)
            os.fsync(parent_fd)
            target_after = os.fstat(target_fd)
            target_named_after = os.stat(
                item.leaf, dir_fd=parent_fd, follow_symlinks=False
            )
            parent_after = os.fstat(parent_fd)
            parent_named_after = item.parent.stat(follow_symlinks=False)
            if (
                not _directory_is_exact(
                    target_after, target_named_after, mode=TARGET_MODE
                )
                or not _directory_is_exact(
                    parent_after, parent_named_after, mode=item.parent_mode
                )
                or not _directory_is_empty(target_fd, flags)
                or (target_after.st_dev, target_after.st_ino)
                != (target_before.st_dev, target_before.st_ino)
                or (parent_after.st_dev, parent_after.st_ino)
                != (parent_before.st_dev, parent_before.st_ino)
            ):
                raise OSError(errno.EIO, "identity")
            results.append(
                (str(item.target), "created" if created else "replayed")
            )
    except Phase9DisposableStoreSubstrateError:
        raise
    except OSError as error:
        raise Phase9DisposableStoreSubstrateError(
            "phase9_store_substrate_refused"
        ) from error
    finally:
        if untracked_target_fd >= 0:
            os.close(untracked_target_fd)
        for _, target_fd, _, _ in reversed(targets):
            os.close(target_fd)
        for _, parent_fd, _ in reversed(parents):
            os.close(parent_fd)
    return tuple(results)


def bootstrap_phase9_disposable_store_substrate() -> Mapping[str, object]:
    """Create the two fixed leaves, or verify exact empty existing leaves."""

    if (
        not _ISOLATED_RUNTIME_AT_START
        or not _DONT_WRITE_BYTECODE_AT_START
        or sys.platform != "linux"
    ):
        raise Phase9DisposableStoreSubstrateError(
            "phase9_store_substrate_runtime_isolation_required"
        )
    if os.geteuid() != ROOT_UID or os.getegid() != ROOT_GID:
        raise Phase9DisposableStoreSubstrateError(
            "phase9_store_substrate_root_required"
        )
    _verify_fixed_identity()
    outcomes = _bootstrap_directories(_fixed_directories())
    states = {state for _, state in outcomes}
    overall = next(iter(states)) if len(states) == 1 else "created_and_replayed"
    return {
        "schema_version": RESULT_SCHEMA,
        "result": overall,
        "directories": [
            {"path": path, "result": state} for path, state in outcomes
        ],
        "retained_empty_substrates": True,
        "exclusions": {
            "activation_changed": False,
            "database_or_vector_store_access": False,
            "docker_access": False,
            "provider_calls": 0,
            "repository_imports": False,
            "secret_access": False,
            "service_changes": False,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    if tuple(sys.argv[1:] if argv is None else argv):
        raise Phase9DisposableStoreSubstrateError(
            "phase9_store_substrate_arguments_refused"
        )
    result = bootstrap_phase9_disposable_store_substrate()
    sys.stdout.buffer.write(_canonical(result) + b"\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Phase9DisposableStoreSubstrateError as error:
        sys.stderr.write(str(error) + "\n")
        raise SystemExit(1) from None
