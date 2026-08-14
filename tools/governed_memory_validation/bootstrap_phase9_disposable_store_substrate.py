#!/usr/bin/env python3
from __future__ import annotations

"""Create or verify the two fixed retained Phase 9 substrate directories."""

import sys


_ISOLATED_RUNTIME_AT_START = bool(sys.flags.isolated)
_DONT_WRITE_BYTECODE_AT_START = bool(sys.dont_write_bytecode)
_PINNED_PHASE9J_RUNTIME_RECEIPT_SHA256 = (
    "ed0b3518484eec292f35f8b996bccefd01e13038105634016b4253a8c2a732a7"
)
_PINNED_PHASE9J_INTERPRETER = (
    "/opt/governed-memory-controller/runtimes/"
    + _PINNED_PHASE9J_RUNTIME_RECEIPT_SHA256
    + "/bin/python"
)
if __name__ == "__main__" and not (
    _ISOLATED_RUNTIME_AT_START and _DONT_WRITE_BYTECODE_AT_START
):
    sys.stderr.write("phase9_store_substrate_runtime_isolation_required\n")
    raise SystemExit(1)
if __name__ == "__main__" and (
    sys.platform != "linux" or sys.executable != _PINNED_PHASE9J_INTERPRETER
):
    sys.stderr.write("phase9_store_substrate_pinned_runtime_required\n")
    raise SystemExit(1)

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import errno
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import select
import stat
import subprocess
from typing import Final


_BOOTSTRAP_PATH: Final = Path(__file__).resolve(strict=True)
_REPOSITORY_ROOT: Final = _BOOTSTRAP_PATH.parents[2]
_BOOTSTRAP_RELATIVE: Final = (
    "tools/governed_memory_validation/"
    "bootstrap_phase9_disposable_store_substrate.py"
)
if (
    _BOOTSTRAP_PATH.relative_to(_REPOSITORY_ROOT).as_posix()
    != _BOOTSTRAP_RELATIVE
):
    raise SystemExit("phase9_store_substrate_invocation_invalid")
sys.path.insert(0, str(_REPOSITORY_ROOT))

from tools.governed_memory_validation import phase9_permitted_candidate
from tools.governed_memory_validation import staged_prefix_disposition
from tools.governed_memory_install.execution_lock import (
    ExecutionLockError,
    HeldExecutionLockCapability,
    validate_held_execution_lock,
)


STORE_PARENT_PATH: Final = Path("/etc/governed-memory-stores")
STORE_TARGET_LEAF: Final = "9a54cf123493-000004"
STORE_TARGET_PATH: Final = STORE_PARENT_PATH / STORE_TARGET_LEAF
EXECUTION_PARENT_PATH: Final = Path("/var/lib/governed-memory-controller")
EXECUTION_TARGET_LEAF: Final = "executions-v4"
EXECUTION_TARGET_PATH: Final = EXECUTION_PARENT_PATH / EXECUTION_TARGET_LEAF
ROOT_UID: Final = 0
ROOT_GID: Final = 0
STORE_PARENT_MODE: Final = 0o755
EXECUTION_PARENT_MODE: Final = 0o700
TARGET_MODE: Final = 0o700
RESULT_SCHEMA: Final = "governed-memory-phase9-disposable-store-substrate-v1"
LIVE_PROOF_GUARD_PATH: Final = Path(
    "/run/lock/governed-memory-controller/phase9-disposable-live-proof.lock"
)
MANAGER_CONTROL_FD: Final = 198
MANAGER_PID_ENVIRONMENT_KEY: Final = "GOVERNED_MEMORY_PHASE9_MANAGER_PID"
MANAGER_RELATIVE: Final = (
    "tools/governed_memory_validation/"
    "execute_phase9_disposable_live_proof_controller.py"
)
ISSUER_RELATIVE: Final = (
    "tools/governed_memory_validation/"
    "issue_disposable_installation_live_proof.py"
)
LEASE_GUARD_PYTHON: Final = "/usr/bin/python3.12"
LEASE_GUARD: Final = (
    "/var/lib/chat-memory-change-leases-v1/control/bin/"
    "chat_memory_lease_guard.py"
)
LEASE_GUARD_UID: Final = 1000
LEASE_GUARD_GID: Final = 1000
LEASE_ID_KEYS: Final = (
    "CHAT_MEMORY_LEASE_ID",
    "CODEX_TASK_ID",
    "CODEX_THREAD_ID",
)


class Phase9DisposableStoreSubstrateError(RuntimeError):
    """Content-free refusal from the fixed substrate bootstrap."""


@dataclass(frozen=True, slots=True)
class _FixedDirectory:
    parent: Path
    parent_mode: int
    leaf: str
    target: Path


def _identifier_valid(value: object) -> bool:
    return bool(
        type(value) is str
        and 1 <= len(value) <= 128
        and value.isascii()
        and value[0].isalnum()
        and all(character.isalnum() or character in "._:@-" for character in value)
    )


def _read_exact_cmdline(pid: str) -> bytes:
    with open(f"/proc/{pid}/cmdline", "rb") as stream:
        value = stream.read(4097)
    if len(value) > 4096:
        raise OSError("command line")
    return value


def _issuer_control_snapshot() -> tuple[int, tuple[int, int]]:
    """Reprove the exact issuer, manager, and inherited live control pipe."""

    environment = os.environ
    manager_raw = environment.get(MANAGER_PID_ENVIRONMENT_KEY)
    if (
        type(manager_raw) is not str
        or not manager_raw.isascii()
        or not manager_raw.isdecimal()
        or manager_raw.startswith("0")
    ):
        raise OSError("manager")
    manager_pid = int(manager_raw)
    if (
        manager_pid <= 1
        or str(manager_pid) != manager_raw
        or manager_pid != os.getppid()
        or any(not _identifier_valid(environment.get(key)) for key in LEASE_ID_KEYS)
    ):
        raise OSError("manager")
    opened = os.fstat(MANAGER_CONTROL_FD)
    flags = fcntl.fcntl(MANAGER_CONTROL_FD, fcntl.F_GETFL)
    ready, unused_write, unused_exception = select.select(
        [MANAGER_CONTROL_FD], [], [], 0
    )
    if (
        not stat.S_ISFIFO(opened.st_mode)
        or flags & os.O_ACCMODE != os.O_RDONLY
        or ready
        or unused_write
        or unused_exception
    ):
        raise OSError("manager control")
    issuer_path = str(_REPOSITORY_ROOT / ISSUER_RELATIVE)
    manager_path = str(_REPOSITORY_ROOT / MANAGER_RELATIVE)
    expected_issuer = b"\0".join(
        value.encode("utf-8")
        for value in (_PINNED_PHASE9J_INTERPRETER, "-I", "-B", issuer_path)
    ) + b"\0"
    expected_manager = b"\0".join(
        value.encode("utf-8")
        for value in (_PINNED_PHASE9J_INTERPRETER, "-I", "-B", manager_path)
    ) + b"\0"
    if (
        os.path.realpath(os.readlink("/proc/self/exe"))
        != os.path.realpath(_PINNED_PHASE9J_INTERPRETER)
        or os.path.realpath(os.readlink(f"/proc/{manager_pid}/exe"))
        != os.path.realpath(_PINNED_PHASE9J_INTERPRETER)
        or _read_exact_cmdline("self") != expected_issuer
        or _read_exact_cmdline(manager_raw) != expected_manager
    ):
        raise OSError("lineage")
    return manager_pid, (opened.st_dev, opened.st_ino)


def _require_issuer_invocation_authority() -> None:
    """Require the exact Phase 9J issuer, manager pipe, and active lease."""

    if (
        not _ISOLATED_RUNTIME_AT_START
        or not _DONT_WRITE_BYTECODE_AT_START
        or sys.platform != "linux"
        or sys.executable != _PINNED_PHASE9J_INTERPRETER
    ):
        raise Phase9DisposableStoreSubstrateError(
            "phase9_store_substrate_issuer_authority_required"
        )
    try:
        before = _issuer_control_snapshot()
        guard_environment = {
            key: str(os.environ[key]) for key in LEASE_ID_KEYS
        }
        guard_environment.update(
            {
                "PATH": "/usr/bin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
            }
        )
        completed = subprocess.run(
            (
                LEASE_GUARD_PYTHON,
                LEASE_GUARD,
                "--operation",
                "production-write",
                "--worktree",
                str(_REPOSITORY_ROOT),
            ),
            cwd="/",
            env=guard_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=90,
            check=False,
            user=LEASE_GUARD_UID,
            group=LEASE_GUARD_GID,
            extra_groups=(),
        )
        if (
            completed.returncode != 0
            or completed.stdout != b""
            or completed.stderr != b""
            or _issuer_control_snapshot() != before
        ):
            raise OSError("lease")
    except (OSError, subprocess.SubprocessError, KeyError, ValueError, TypeError) as error:
        raise Phase9DisposableStoreSubstrateError(
            "phase9_store_substrate_issuer_authority_required"
        ) from error


def _require_exact_held_lock(held_lock: HeldExecutionLockCapability) -> None:
    try:
        validate_held_execution_lock(held_lock)
        owner = held_lock._owner
        if (
            owner.path != LIVE_PROOF_GUARD_PATH
            or owner.expected_uid != ROOT_UID
            or owner.expected_gid != ROOT_GID
        ):
            raise ExecutionLockError("execution_lock_capability_invalid")
    except (ExecutionLockError, AttributeError, TypeError) as error:
        raise Phase9DisposableStoreSubstrateError(
            "phase9_store_substrate_held_lock_required"
        ) from error


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
    store_target = store_parent / "9a54cf123493-000004"
    execution_parent = PurePosixPath("/var/lib/governed-memory-controller")
    execution_target = execution_parent / "executions-v4"
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


def _require_exact_staged_prefix_disposition() -> Mapping[str, object]:
    """Require the exact failed-prefix fence before creating 000004 paths."""

    try:
        candidate = (
            phase9_permitted_candidate.require_exact_permitted_candidate()
        )
        expectation = staged_prefix_disposition.ReviewedStagedPrefixExpectation(
            contract_sha256=candidate.contract_sha256,
            disposition_id=(
                staged_prefix_disposition.PRODUCTION_DISPOSITION_ID
            ),
            old_tag_ref=staged_prefix_disposition.PRODUCTION_OLD_TAG_REF,
            old_tag_commit=staged_prefix_disposition.PRODUCTION_OLD_TAG_COMMIT,
            old_tag_tree=staged_prefix_disposition.PRODUCTION_OLD_TAG_TREE,
            failed_package_manifest_sha256=(
                staged_prefix_disposition.PRODUCTION_FAILED_PACKAGE_MANIFEST_SHA256
            ),
            failed_controller_runtime_receipt_sha256=(
                staged_prefix_disposition.PRODUCTION_FAILED_RUNTIME_RECEIPT_SHA256
            ),
            corrected_generation=(
                staged_prefix_disposition.PRODUCTION_CORRECTED_GENERATION
            ),
            corrected_package_manifest_sha256=(
                candidate.package_manifest_sha256
            ),
            corrected_controller_runtime_receipt_sha256=(
                candidate.controller_runtime_receipt_sha256
            ),
        )
        receipt = staged_prefix_disposition.verify_staged_prefix_tombstone(
            staged_prefix_disposition.production_disposition_paths(),
            expectation,
        )
        successor = (
            staged_prefix_disposition.production_corrected_attempt_identity_sha256(
                package_manifest_sha256=candidate.package_manifest_sha256,
                controller_runtime_receipt_sha256=(
                    candidate.controller_runtime_receipt_sha256
                ),
            )
        )
    except (
        phase9_permitted_candidate.Phase9PermittedCandidateError,
        staged_prefix_disposition.StagedPrefixDispositionError,
    ) as error:
        raise Phase9DisposableStoreSubstrateError(
            "phase9_store_substrate_disposition_required"
        ) from error
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("schema_version")
        != staged_prefix_disposition.TOMBSTONE_SCHEMA
        or receipt.get("result") != staged_prefix_disposition.RESULT
        or receipt.get("contract_sha256") != candidate.contract_sha256
        or receipt.get("failed_prefix_identity_sha256")
        != staged_prefix_disposition.production_failed_prefix_identity_sha256()
        or receipt.get("corrected_attempt_identity_sha256") != successor
        or receipt.get("corrected_generation")
        != staged_prefix_disposition.PRODUCTION_CORRECTED_GENERATION
        or receipt.get("corrected_package_manifest_sha256")
        != candidate.package_manifest_sha256
        or receipt.get("corrected_controller_runtime_receipt_sha256")
        != candidate.controller_runtime_receipt_sha256
        or receipt.get("failed_evidence_preserved_in_place") is not True
        or receipt.get("no_store_or_service_effects_proven") is not True
        or receipt.get("deletion_performed") is not False
        or receipt.get("provider_calls") != 0
        or receipt.get("production_data_read") is not False
        or receipt.get("activation_performed") is not False
    ):
        raise Phase9DisposableStoreSubstrateError(
            "phase9_store_substrate_disposition_required"
        )
    return receipt


def _bootstrap_directories(
    selected: tuple[_FixedDirectory, ...],
    *,
    held_lock: HeldExecutionLockCapability,
) -> tuple[tuple[str, str], ...]:
    """Open all parents and preflight all existing leaves before creation."""

    _require_issuer_invocation_authority()
    _require_exact_held_lock(held_lock)

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


def bootstrap_phase9_disposable_store_substrate(
    *,
    held_lock: HeldExecutionLockCapability,
) -> Mapping[str, object]:
    """Create the two fixed leaves, or verify exact empty existing leaves."""

    if (
        not _ISOLATED_RUNTIME_AT_START
        or not _DONT_WRITE_BYTECODE_AT_START
        or sys.platform != "linux"
        or sys.executable != _PINNED_PHASE9J_INTERPRETER
    ):
        raise Phase9DisposableStoreSubstrateError(
            "phase9_store_substrate_runtime_isolation_required"
        )
    if os.geteuid() != ROOT_UID or os.getegid() != ROOT_GID:
        raise Phase9DisposableStoreSubstrateError(
            "phase9_store_substrate_root_required"
        )
    _require_exact_held_lock(held_lock)
    _require_exact_staged_prefix_disposition()
    _verify_fixed_identity()
    outcomes = _bootstrap_directories(
        _fixed_directories(), held_lock=held_lock
    )
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
            "fixed_candidate_authority_imports": True,
            "secret_access": False,
            "service_changes": False,
            "untrusted_repository_imports": False,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    if tuple(sys.argv[1:] if argv is None else argv):
        raise Phase9DisposableStoreSubstrateError(
            "phase9_store_substrate_arguments_refused"
        )
    raise Phase9DisposableStoreSubstrateError(
        "phase9_store_substrate_manager_required"
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Phase9DisposableStoreSubstrateError as error:
        sys.stderr.write(str(error) + "\n")
        raise SystemExit(1) from None
