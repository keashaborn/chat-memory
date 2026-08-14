from __future__ import annotations

"""Durable create-once evidence that a stopped proof worker is armed to die.

The receipt is published inside the exact disposable execution directory while
the worker is stopped at a verified durable journal boundary.  It is retained
across controller-process death so recovery can distinguish an armed fault
injection from an unproved process exit.  Publication never replaces a name.
"""

import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Final, Mapping


EXECUTIONS_ROOT: Final = Path(
    "/var/lib/governed-memory-controller/executions-v4"
)
PROCESS_DEATH_ARM_RECEIPT_SCHEMA: Final = (
    "governed-memory-phase9-process-death-arm-receipt-v2"
)
PROCESS_DEATH_ARM_RECEIPT_RESULT: Final = (
    "exact_worker_cooperatively_stopped_after_durable_boundary_and_sigkill_armed"
)
PROCESS_DEATH_ARM_RECEIPT_STAGING_SUFFIX: Final = ".publishing"
MAX_PROCESS_DEATH_ARM_RECEIPT_BYTES: Final = 32 * 1024
AT_EMPTY_PATH: Final = 0x1000

INSTALL_BOUNDARY_KIND: Final = "install"
EMPTY_ROLLBACK_BOUNDARY_KIND: Final = "empty_rollback"
INSTALL_BOUNDARY_STEP_ID: Final = (
    "I04_WRITE_RESOLVED_STORE_SPEC_AND_GENERATE_FRESH_STORE_SECRETS"
)
EMPTY_ROLLBACK_BOUNDARY_STEP_ID: Final = (
    "R05_DISABLE_AND_REMOVE_STORES_SUPERVISOR"
)

_BOUNDARY_LAYOUT: Final = MappingProxyType(
    {
        INSTALL_BOUNDARY_KIND: (
            "install-process-death-arm-receipt.json",
            "journal.jsonl",
            INSTALL_BOUNDARY_STEP_ID,
            re.compile(r"install-[0-9a-f]{40}\Z", re.ASCII),
        ),
        EMPTY_ROLLBACK_BOUNDARY_KIND: (
            "rollback-process-death-arm-receipt.json",
            "rollback.jsonl",
            EMPTY_ROLLBACK_BOUNDARY_STEP_ID,
            re.compile(r"rollback-[0-9a-f]{40}\Z", re.ASCII),
        ),
    }
)
_HASH_RE: Final = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_COMMIT_RE: Final = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_RECEIPT_KEYS: Final = frozenset(
    {
        "schema_version",
        "result",
        "boundary_kind",
        "candidate_git_commit",
        "candidate_git_tree",
        "package_manifest_sha256",
        "controller_runtime_receipt_sha256",
        "recovery_capsule_sha256",
        "recovery_reservation_claim_sha256",
        "install_authority_claim_sha256",
        "execution_id",
        "journal_name",
        "journal_plan_sha256",
        "journal_attempt_id",
        "boundary_step_id",
        "boundary_event",
        "boundary_record_sequence",
        "boundary_record_sha256",
        "journal_sequence_at_arm",
        "journal_head_sha256_at_arm",
        "worker_identity_sha256",
        "worker_parent_death_sigkill_armed",
        "worker_cooperative_post_fsync_sigstop_observed",
        "worker_sigstop_observed",
        "worker_child_process_set_empty_at_arm",
        "global_execution_lock_exclusion_observed",
        "sigkill_required_before_resume",
        "receipt_sha256",
    }
)
_HASH_FIELDS: Final = (
    "package_manifest_sha256",
    "controller_runtime_receipt_sha256",
    "recovery_capsule_sha256",
    "recovery_reservation_claim_sha256",
    "install_authority_claim_sha256",
    "execution_id",
    "journal_plan_sha256",
    "boundary_record_sha256",
    "journal_head_sha256_at_arm",
    "worker_identity_sha256",
    "receipt_sha256",
)
_REQUIRED_TRUE: Final = (
    "worker_parent_death_sigkill_armed",
    "worker_cooperative_post_fsync_sigstop_observed",
    "worker_sigstop_observed",
    "worker_child_process_set_empty_at_arm",
    "global_execution_lock_exclusion_observed",
    "sigkill_required_before_resume",
)


class ProcessDeathArmReceiptError(RuntimeError):
    """Content-free refusal for arm-receipt drift or publication failure."""


def _error(reason: str) -> ProcessDeathArmReceiptError:
    return ProcessDeathArmReceiptError(f"phase9_process_death_arm_receipt_{reason}")


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
        raise _error("invalid") from error


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _error("invalid")
        result[key] = value
    return result


def _layout(boundary_kind: str) -> tuple[str, str, str, re.Pattern[str]]:
    if type(boundary_kind) is not str:
        raise _error("boundary_invalid")
    value = _BOUNDARY_LAYOUT.get(boundary_kind)
    if value is None:
        raise _error("boundary_invalid")
    return value


def process_death_arm_receipt_path(
    *, execution_id: str, boundary_kind: str
) -> Path:
    """Return the only production path allowed for this execution and role."""

    if type(execution_id) is not str or _HASH_RE.fullmatch(execution_id) is None:
        raise _error("execution_id_invalid")
    filename, _, _, _ = _layout(boundary_kind)
    return EXECUTIONS_ROOT / execution_id / filename


def verify_process_death_arm_receipt(
    receipt: Mapping[str, object],
    *,
    expected_execution_id: str | None = None,
    expected_boundary_kind: str | None = None,
) -> Mapping[str, object]:
    """Strictly verify one canonical arm receipt and its self hash."""

    document = dict(receipt) if isinstance(receipt, Mapping) else {}
    boundary_kind = document.get("boundary_kind")
    try:
        _, journal_name, boundary_step_id, attempt_pattern = _layout(
            str(boundary_kind) if type(boundary_kind) is str else boundary_kind
        )
    except ProcessDeathArmReceiptError:
        raise _error("invalid") from None
    boundary_sequence = document.get("boundary_record_sequence")
    journal_sequence = document.get("journal_sequence_at_arm")
    if (
        not isinstance(receipt, Mapping)
        or set(document) != _RECEIPT_KEYS
        or document.get("schema_version") != PROCESS_DEATH_ARM_RECEIPT_SCHEMA
        or document.get("result") != PROCESS_DEATH_ARM_RECEIPT_RESULT
        or type(document.get("candidate_git_commit")) is not str
        or _COMMIT_RE.fullmatch(str(document["candidate_git_commit"])) is None
        or type(document.get("candidate_git_tree")) is not str
        or _COMMIT_RE.fullmatch(str(document["candidate_git_tree"])) is None
        or any(
            type(document.get(key)) is not str
            or _HASH_RE.fullmatch(str(document[key])) is None
            for key in _HASH_FIELDS
        )
        or document.get("journal_name") != journal_name
        or document.get("boundary_step_id") != boundary_step_id
        or document.get("boundary_event") != "applied"
        or type(document.get("journal_attempt_id")) is not str
        or attempt_pattern.fullmatch(str(document["journal_attempt_id"])) is None
        or type(boundary_sequence) is not int
        or boundary_sequence < 1
        or type(journal_sequence) is not int
        or journal_sequence < 1
        or boundary_sequence != journal_sequence
        or document.get("boundary_record_sha256")
        != document.get("journal_head_sha256_at_arm")
        or any(document.get(key) is not True for key in _REQUIRED_TRUE)
        or (
            expected_execution_id is not None
            and document.get("execution_id") != expected_execution_id
        )
        or (
            expected_boundary_kind is not None
            and boundary_kind != expected_boundary_kind
        )
    ):
        raise _error("invalid")
    if (
        expected_execution_id is not None
        and (
            type(expected_execution_id) is not str
            or _HASH_RE.fullmatch(expected_execution_id) is None
        )
    ):
        raise _error("binding_invalid")
    if expected_boundary_kind is not None:
        try:
            _layout(expected_boundary_kind)
        except ProcessDeathArmReceiptError:
            raise _error("binding_invalid") from None
    unsigned = {
        key: value for key, value in document.items() if key != "receipt_sha256"
    }
    if document["receipt_sha256"] != _sha(_canonical(unsigned)):
        raise _error("invalid")
    return MappingProxyType(document)


def _parse_and_verify(
    raw: bytes,
    *,
    expected_execution_id: str,
    expected_boundary_kind: str,
) -> Mapping[str, object]:
    if not 1 <= len(raw) <= MAX_PROCESS_DEATH_ARM_RECEIPT_BYTES:
        raise _error("invalid")
    try:
        value = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda unused: (_ for _ in ()).throw(_error("invalid")),
        )
    except ProcessDeathArmReceiptError:
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise _error("invalid") from error
    if type(value) is not dict or _canonical(value) != raw:
        raise _error("invalid")
    return verify_process_death_arm_receipt(
        value,
        expected_execution_id=expected_execution_id,
        expected_boundary_kind=expected_boundary_kind,
    )


def _nofollow() -> int:
    value = getattr(os, "O_NOFOLLOW", 0)
    if type(value) is not int or value == 0:
        raise _error("nofollow_unavailable")
    return value


def _directory_flags() -> int:
    value = getattr(os, "O_DIRECTORY", 0)
    if type(value) is not int or value == 0:
        raise _error("directory_open_unavailable")
    return os.O_RDONLY | os.O_CLOEXEC | _nofollow() | value


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _verify_open_named_directory(
    *,
    descriptor: int,
    path: Path,
    expected_uid: int,
    expected_gid: int,
    exact_mode: int,
) -> os.stat_result:
    try:
        opened = os.fstat(descriptor)
        named = path.stat(follow_symlinks=False)
    except OSError as error:
        raise _error("parent_invalid") from error
    if (
        not stat.S_ISDIR(opened.st_mode)
        or stat.S_IMODE(opened.st_mode) != exact_mode
        or opened.st_uid != expected_uid
        or opened.st_gid != expected_gid
        or _identity(opened) != _identity(named)
    ):
        raise _error("parent_invalid")
    return opened


def _open_execution_directory_at_root(
    *,
    executions_root: Path,
    execution_id: str,
    expected_uid: int,
    expected_gid: int,
) -> tuple[int, Path]:
    """Testable opener retaining exact root and execution-directory identity."""

    if (
        not isinstance(executions_root, Path)
        or not executions_root.is_absolute()
        or type(execution_id) is not str
        or _HASH_RE.fullmatch(execution_id) is None
        or type(expected_uid) is not int
        or expected_uid < 0
        or type(expected_gid) is not int
        or expected_gid < 0
    ):
        raise _error("parent_invalid")
    root_fd = execution_fd = -1
    execution_path = executions_root / execution_id
    try:
        root_fd = os.open(executions_root, _directory_flags())
        root_before = _verify_open_named_directory(
            descriptor=root_fd,
            path=executions_root,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            exact_mode=0o700,
        )
        execution_fd = os.open(execution_id, _directory_flags(), dir_fd=root_fd)
        _verify_open_named_directory(
            descriptor=execution_fd,
            path=execution_path,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            exact_mode=0o700,
        )
        root_after = os.fstat(root_fd)
        named_root_after = executions_root.stat(follow_symlinks=False)
        if (
            _identity(root_after) != _identity(root_before)
            or _identity(named_root_after) != _identity(root_before)
        ):
            raise _error("parent_invalid")
        os.close(root_fd)
        root_fd = -1
        result = execution_fd
        execution_fd = -1
        return result, execution_path
    except ProcessDeathArmReceiptError:
        raise
    except OSError as error:
        raise _error("parent_invalid") from error
    finally:
        for descriptor in (execution_fd, root_fd):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _open_production_execution_directory(execution_id: str) -> tuple[int, Path]:
    if os.geteuid() != 0:
        raise _error("root_required")
    if type(execution_id) is not str or _HASH_RE.fullmatch(execution_id) is None:
        raise _error("execution_id_invalid")

    descriptor = -1
    execution_path = EXECUTIONS_ROOT / execution_id
    parts = EXECUTIONS_ROOT.parts[1:] + (execution_id,)
    try:
        descriptor = os.open("/", _directory_flags())
        current = Path("/")
        for index, part in enumerate(parts):
            child = os.open(part, _directory_flags(), dir_fd=descriptor)
            current /= part
            opened = os.fstat(child)
            named = current.stat(follow_symlinks=False)
            exact_private = index >= len(parts) - 3
            if (
                not stat.S_ISDIR(opened.st_mode)
                or opened.st_uid != 0
                or opened.st_gid != 0
                or _identity(opened) != _identity(named)
                or (
                    stat.S_IMODE(opened.st_mode) != 0o700
                    if exact_private
                    else stat.S_IMODE(opened.st_mode) & 0o022 != 0
                )
            ):
                os.close(child)
                raise _error("parent_invalid")
            os.close(descriptor)
            descriptor = child
        return descriptor, execution_path
    except ProcessDeathArmReceiptError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise _error("parent_invalid") from error


def _read_all(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    extra = os.read(descriptor, 1)
    raw = b"".join(chunks)
    if remaining or extra:
        raise _error("file_invalid")
    return raw


def _stable(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_member(
    *,
    parent_fd: int,
    parent_path: Path,
    name: str,
    expected_uid: int,
    expected_gid: int,
    allowed_links: frozenset[int],
) -> tuple[bytes, os.stat_result]:
    descriptor = -1
    try:
        parent_before = _verify_open_named_directory(
            descriptor=parent_fd,
            path=parent_path,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            exact_mode=0o700,
        )
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | _nofollow(),
            dir_fd=parent_fd,
        )
        opened = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o400
            or opened.st_uid != expected_uid
            or opened.st_gid != expected_gid
            or opened.st_nlink not in allowed_links
            or not 1 <= opened.st_size <= MAX_PROCESS_DEATH_ARM_RECEIPT_BYTES
            or _identity(opened) != _identity(named)
        ):
            raise _error("file_invalid")
        raw = _read_all(descriptor, opened.st_size)
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        named_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        parent_after = _verify_open_named_directory(
            descriptor=parent_fd,
            path=parent_path,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            exact_mode=0o700,
        )
        if (
            _stable(after) != _stable(opened)
            or _stable(named_after) != _stable(opened)
            or _identity(parent_after) != _identity(parent_before)
        ):
            raise _error("file_invalid")
        return raw, after
    except FileNotFoundError as error:
        raise _error("absent") from error
    except ProcessDeathArmReceiptError:
        raise
    except OSError as error:
        raise _error("file_invalid") from error
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _read_optional_member(
    *,
    parent_fd: int,
    parent_path: Path,
    name: str,
    expected_uid: int,
    expected_gid: int,
    allowed_links: frozenset[int],
) -> tuple[bytes, os.stat_result] | None:
    try:
        return _read_member(
            parent_fd=parent_fd,
            parent_path=parent_path,
            name=name,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            allowed_links=allowed_links,
        )
    except ProcessDeathArmReceiptError as error:
        if str(error) != "phase9_process_death_arm_receipt_absent":
            raise
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as observed:
            raise _error("file_invalid") from observed
        raise _error("appeared")


def _open_complete_anonymous_inode(
    *,
    parent_fd: int,
    raw: bytes,
    expected_uid: int,
    expected_gid: int,
) -> tuple[int, tuple[int, int]]:
    descriptor = -1
    completed = False
    try:
        tmpfile = getattr(os, "O_TMPFILE", 0)
        if type(tmpfile) is not int or tmpfile == 0:
            raise _error("anonymous_tmpfile_unavailable")
        descriptor = os.open(
            ".",
            os.O_RDWR | os.O_CLOEXEC | tmpfile,
            0o400,
            dir_fd=parent_fd,
        )
        created = os.fstat(descriptor)
        inode = _identity(created)
        if (
            not stat.S_ISREG(created.st_mode)
            or created.st_nlink != 0
            or created.st_size != 0
        ):
            raise _error("anonymous_inode_invalid")
        os.fchown(descriptor, expected_uid, expected_gid)
        os.fchmod(descriptor, 0o400)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if type(written) is not int or written <= 0 or written > len(view):
                raise _error("write_failed")
            view = view[written:]
        os.fsync(descriptor)
        complete = os.fstat(descriptor)
        if (
            not stat.S_ISREG(complete.st_mode)
            or stat.S_IMODE(complete.st_mode) != 0o400
            or complete.st_uid != expected_uid
            or complete.st_gid != expected_gid
            or complete.st_nlink != 0
            or complete.st_size != len(raw)
            or _identity(complete) != inode
        ):
            raise _error("anonymous_inode_invalid")
        if os.lseek(descriptor, 0, os.SEEK_SET) != 0:
            raise _error("anonymous_inode_invalid")
        observed = _read_all(descriptor, len(raw))
        after = os.fstat(descriptor)
        if observed != raw or _stable(after) != _stable(complete):
            raise _error("anonymous_inode_invalid")
        completed = True
        return descriptor, inode
    except ProcessDeathArmReceiptError:
        raise
    except OSError as error:
        raise _error("write_failed") from error
    finally:
        if descriptor >= 0 and not completed:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _link_anonymous_inode(
    *,
    descriptor: int,
    parent_fd: int,
    name: str,
    inode: tuple[int, int],
    expected_uid: int,
    expected_gid: int,
) -> None:
    if (
        type(descriptor) is not int
        or descriptor < 0
        or type(name) is not str
        or not name
        or "/" in name
        or "\x00" in name
    ):
        raise _error("publication_invalid")
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        linkat = libc.linkat
        linkat.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
        )
        linkat.restype = ctypes.c_int
        result = linkat(
            descriptor,
            ctypes.c_char_p(b""),
            parent_fd,
            ctypes.c_char_p(name.encode("ascii")),
            AT_EMPTY_PATH,
        )
        if result != 0:
            code = ctypes.get_errno()
            if code == errno.EEXIST:
                raise _error("staging_exists")
            raise OSError(code, os.strerror(code))
        opened = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o400
            or opened.st_uid != expected_uid
            or opened.st_gid != expected_gid
            or opened.st_nlink != 1
            or _identity(opened) != inode
            or _identity(named) != inode
        ):
            raise _error("publication_invalid")
        os.fsync(parent_fd)
    except ProcessDeathArmReceiptError:
        raise
    except (AttributeError, UnicodeError, OSError) as error:
        raise _error("publication_failed") from error


def _write_complete_staging_from_anonymous(
    *,
    parent_fd: int,
    parent_path: Path,
    name: str,
    raw: bytes,
    expected_uid: int,
    expected_gid: int,
) -> tuple[int, int]:
    descriptor = -1
    try:
        descriptor, inode = _open_complete_anonymous_inode(
            parent_fd=parent_fd,
            raw=raw,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        )
        _link_anonymous_inode(
            descriptor=descriptor,
            parent_fd=parent_fd,
            name=name,
            inode=inode,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        )
        observed_raw, observed = _read_member(
            parent_fd=parent_fd,
            parent_path=parent_path,
            name=name,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            allowed_links=frozenset({1}),
        )
        if observed_raw != raw or _identity(observed) != inode:
            raise _error("staging_invalid")
        try:
            os.close(descriptor)
        except OSError as error:
            descriptor = -1
            raise _error("close_failed") from error
        descriptor = -1
        return inode
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _publish_staging(
    *,
    parent_fd: int,
    parent_path: Path,
    staging_name: str,
    final_name: str,
    inode: tuple[int, int],
    raw: bytes,
    expected_uid: int,
    expected_gid: int,
) -> None:
    try:
        os.link(
            staging_name,
            final_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
        staging = os.stat(staging_name, dir_fd=parent_fd, follow_symlinks=False)
        final = os.stat(final_name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _identity(staging) != inode
            or _identity(final) != inode
            or staging.st_nlink != 2
            or final.st_nlink != 2
        ):
            raise _error("publication_invalid")
        os.fsync(parent_fd)
        current = os.stat(staging_name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(current) != inode or current.st_nlink != 2:
            raise _error("publication_invalid")
        os.unlink(staging_name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        final_raw, final = _read_member(
            parent_fd=parent_fd,
            parent_path=parent_path,
            name=final_name,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            allowed_links=frozenset({1}),
        )
        if final_raw != raw or _identity(final) != inode:
            raise _error("publication_invalid")
        os.fsync(parent_fd)
    except FileExistsError as error:
        raise _error("final_exists") from error
    except ProcessDeathArmReceiptError:
        raise
    except OSError as error:
        raise _error("publication_failed") from error


def _reconcile_at_open_execution_directory(
    *,
    parent_fd: int,
    parent_path: Path,
    expected_uid: int,
    expected_gid: int,
    execution_id: str,
    boundary_kind: str,
    expected_raw: bytes | None,
) -> Mapping[str, object] | None:
    final_name, _, _, _ = _layout(boundary_kind)
    staging_name = final_name + PROCESS_DEATH_ARM_RECEIPT_STAGING_SUFFIX
    final_member = _read_optional_member(
        parent_fd=parent_fd,
        parent_path=parent_path,
        name=final_name,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        allowed_links=frozenset({1, 2}),
    )
    staging_member = _read_optional_member(
        parent_fd=parent_fd,
        parent_path=parent_path,
        name=staging_name,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        allowed_links=frozenset({1, 2}),
    )
    if final_member is None and staging_member is None:
        return None

    final_verified = staging_verified = None
    if final_member is not None:
        final_verified = _parse_and_verify(
            final_member[0],
            expected_execution_id=execution_id,
            expected_boundary_kind=boundary_kind,
        )
    if staging_member is not None:
        staging_verified = _parse_and_verify(
            staging_member[0],
            expected_execution_id=execution_id,
            expected_boundary_kind=boundary_kind,
        )
    for member in (final_member, staging_member):
        if (
            member is not None
            and expected_raw is not None
            and member[0] != expected_raw
        ):
            raise _error("conflict")

    if final_member is not None and staging_member is not None:
        final_raw, final = final_member
        staging_raw, staging = staging_member
        if (
            final_raw != staging_raw
            or _identity(final) != _identity(staging)
            or final.st_nlink != 2
            or staging.st_nlink != 2
        ):
            raise _error("conflict")
        os.fsync(parent_fd)
        current = os.stat(staging_name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(current) != _identity(staging) or current.st_nlink != 2:
            raise _error("conflict")
        os.unlink(staging_name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        result = final_verified
    elif staging_member is not None:
        staging_raw, staging = staging_member
        if staging.st_nlink != 1:
            raise _error("conflict")
        _publish_staging(
            parent_fd=parent_fd,
            parent_path=parent_path,
            staging_name=staging_name,
            final_name=final_name,
            inode=_identity(staging),
            raw=staging_raw,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        )
        result = staging_verified
    else:
        assert final_member is not None
        if final_member[1].st_nlink != 1:
            raise _error("conflict")
        os.fsync(parent_fd)
        result = final_verified

    if result is None:
        raise _error("invalid")
    final_raw, final = _read_member(
        parent_fd=parent_fd,
        parent_path=parent_path,
        name=final_name,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        allowed_links=frozenset({1}),
    )
    if expected_raw is not None and final_raw != expected_raw:
        raise _error("conflict")
    verified = _parse_and_verify(
        final_raw,
        expected_execution_id=execution_id,
        expected_boundary_kind=boundary_kind,
    )
    if dict(verified) != dict(result) or final.st_nlink != 1:
        raise _error("publication_invalid")
    os.fsync(parent_fd)
    return verified


def _persist_at_open_execution_directory(
    *,
    parent_fd: int,
    parent_path: Path,
    expected_uid: int,
    expected_gid: int,
    receipt: Mapping[str, object],
) -> Mapping[str, object]:
    verified = verify_process_death_arm_receipt(receipt)
    execution_id = str(verified["execution_id"])
    boundary_kind = str(verified["boundary_kind"])
    if parent_path.name != execution_id:
        raise _error("binding_invalid")
    raw = _canonical(dict(verified))
    existing = _reconcile_at_open_execution_directory(
        parent_fd=parent_fd,
        parent_path=parent_path,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        execution_id=execution_id,
        boundary_kind=boundary_kind,
        expected_raw=raw,
    )
    if existing is not None:
        return existing
    final_name, _, _, _ = _layout(boundary_kind)
    staging_name = final_name + PROCESS_DEATH_ARM_RECEIPT_STAGING_SUFFIX
    inode = _write_complete_staging_from_anonymous(
        parent_fd=parent_fd,
        parent_path=parent_path,
        name=staging_name,
        raw=raw,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    _publish_staging(
        parent_fd=parent_fd,
        parent_path=parent_path,
        staging_name=staging_name,
        final_name=final_name,
        inode=inode,
        raw=raw,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    result = _reconcile_at_open_execution_directory(
        parent_fd=parent_fd,
        parent_path=parent_path,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        execution_id=execution_id,
        boundary_kind=boundary_kind,
        expected_raw=raw,
    )
    if result is None:
        raise _error("publication_invalid")
    return result


def _persist_for_test(
    *,
    executions_root: Path,
    expected_uid: int,
    expected_gid: int,
    receipt: Mapping[str, object],
) -> Mapping[str, object]:
    """Exercise production storage against an exact temporary root."""

    verified = verify_process_death_arm_receipt(receipt)
    descriptor, path = _open_execution_directory_at_root(
        executions_root=executions_root,
        execution_id=str(verified["execution_id"]),
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    try:
        return _persist_at_open_execution_directory(
            parent_fd=descriptor,
            parent_path=path,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            receipt=verified,
        )
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _read_for_test(
    *,
    executions_root: Path,
    expected_uid: int,
    expected_gid: int,
    execution_id: str,
    boundary_kind: str,
) -> Mapping[str, object] | None:
    """Read/reconcile production-format storage under a temporary root."""

    descriptor, path = _open_execution_directory_at_root(
        executions_root=executions_root,
        execution_id=execution_id,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    try:
        return _reconcile_at_open_execution_directory(
            parent_fd=descriptor,
            parent_path=path,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            execution_id=execution_id,
            boundary_kind=boundary_kind,
            expected_raw=None,
        )
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def persist_process_death_arm_receipt(
    receipt: Mapping[str, object],
) -> Mapping[str, object]:
    """Persist one root-owned, create-once production arm receipt."""

    verified = verify_process_death_arm_receipt(receipt)
    descriptor, path = _open_production_execution_directory(
        str(verified["execution_id"])
    )
    try:
        return _persist_at_open_execution_directory(
            parent_fd=descriptor,
            parent_path=path,
            expected_uid=0,
            expected_gid=0,
            receipt=verified,
        )
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def read_process_death_arm_receipt_if_present(
    *, execution_id: str, boundary_kind: str
) -> Mapping[str, object] | None:
    """Read or finish an exact prior arm-receipt publication."""

    process_death_arm_receipt_path(
        execution_id=execution_id, boundary_kind=boundary_kind
    )
    descriptor, path = _open_production_execution_directory(execution_id)
    try:
        return _reconcile_at_open_execution_directory(
            parent_fd=descriptor,
            parent_path=path,
            expected_uid=0,
            expected_gid=0,
            execution_id=execution_id,
            boundary_kind=boundary_kind,
            expected_raw=None,
        )
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
