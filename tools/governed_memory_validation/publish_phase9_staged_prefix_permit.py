#!/usr/bin/env python3
from __future__ import annotations

"""Publish the one root-owned Phase 9 staged-prefix successor permit."""

import os
import sys


_PREIMPORT_CONTROLLER_RUNTIME_RECEIPT_SHA256 = (
    "9884fa9db3be81039b691a25866994d6033c5cf9175030ea82de401a2bbe6334"
)
_PREIMPORT_CONTROLLER_PYTHON = (
    "/opt/governed-memory-controller/runtimes/"
    + _PREIMPORT_CONTROLLER_RUNTIME_RECEIPT_SHA256
    + "/bin/python"
)
_PREIMPORT_MANAGER_PID_KEY = "GOVERNED_MEMORY_PHASE9_MANAGER_PID"
_PREIMPORT_MANAGER_RELATIVE = (
    "tools/governed_memory_validation/"
    "execute_phase9_disposable_live_proof_controller.py"
)
_PREIMPORT_LEASE_ID_KEYS = (
    "CHAT_MEMORY_LEASE_ID",
    "CODEX_TASK_ID",
    "CODEX_THREAD_ID",
)
_ISOLATED_RUNTIME_AT_START = bool(sys.flags.isolated)
_DONT_WRITE_BYTECODE_AT_START = bool(sys.dont_write_bytecode)
if __name__ == "__main__" and not (
    _ISOLATED_RUNTIME_AT_START
    and _DONT_WRITE_BYTECODE_AT_START
    and sys.platform == "linux"
    and sys.executable == _PREIMPORT_CONTROLLER_PYTHON
):
    sys.stderr.write("phase9_staged_prefix_permit_runtime_isolation_required\n")
    raise SystemExit(1)


def _preimport_identifier_valid(value: object) -> bool:
    return bool(
        type(value) is str
        and 1 <= len(value) <= 128
        and value.isascii()
        and value[0].isalnum()
        and all(
            character.isalnum() or character in "._:@-"
            for character in value
        )
    )


def _preimport_manager_lineage_valid(
    environment: object = None,
    *,
    observed_parent_pid: object = None,
    observed_parent_executable: object = None,
    observed_parent_command_line: object = None,
) -> bool:
    """Bind direct publication to the exact Phase 9J manager process."""

    try:
        selected_environment = os.environ if environment is None else environment
        manager_pid_raw = selected_environment.get(_PREIMPORT_MANAGER_PID_KEY)
        if (
            type(manager_pid_raw) is not str
            or not manager_pid_raw.isascii()
            or not manager_pid_raw.isdecimal()
            or manager_pid_raw.startswith("0")
        ):
            return False
        manager_pid = int(manager_pid_raw)
        parent_pid = (
            os.getppid()
            if observed_parent_pid is None
            else observed_parent_pid
        )
        if (
            type(parent_pid) is not int
            or parent_pid <= 1
            or manager_pid != parent_pid
            or str(manager_pid) != manager_pid_raw
            or any(
                not _preimport_identifier_valid(selected_environment.get(key))
                for key in _PREIMPORT_LEASE_ID_KEYS
            )
        ):
            return False
        executable = observed_parent_executable
        if executable is None:
            executable = os.readlink(f"/proc/{manager_pid}/exe")
        if (
            type(executable) is not str
            or os.path.realpath(executable)
            != os.path.realpath(_PREIMPORT_CONTROLLER_PYTHON)
        ):
            return False
        command_line = observed_parent_command_line
        if command_line is None:
            with open(f"/proc/{manager_pid}/cmdline", "rb") as stream:
                command_line = stream.read(4097)
        publisher_path = os.path.realpath(__file__)
        repository_root = os.path.dirname(
            os.path.dirname(os.path.dirname(publisher_path))
        )
        manager_path = os.path.join(
            repository_root, _PREIMPORT_MANAGER_RELATIVE
        )
        expected_command_line = b"\0".join(
            value.encode("utf-8")
            for value in (
                _PREIMPORT_CONTROLLER_PYTHON,
                "-I",
                "-B",
                manager_path,
            )
        ) + b"\0"
        return (
            type(command_line) is bytes
            and len(command_line) <= 4096
            and command_line == expected_command_line
        )
    except (OSError, ValueError, UnicodeError, AttributeError, TypeError):
        return False


if __name__ == "__main__" and not _preimport_manager_lineage_valid():
    sys.stderr.write("phase9_staged_prefix_permit_manager_lineage_required\n")
    raise SystemExit(1)

from collections.abc import Mapping, Sequence
import errno
import hashlib
import json
from pathlib import Path
import re
import stat
import subprocess
from typing import Final


_PUBLISHER_PATH: Final = Path(__file__).resolve(strict=True)
_REPOSITORY_ROOT: Final = _PUBLISHER_PATH.parents[2]
_PUBLISHER_RELATIVE: Final = (
    "tools/governed_memory_validation/publish_phase9_staged_prefix_permit.py"
)
_WRAPPER_RELATIVE: Final = (
    "tools/governed_memory_validation/execute_phase9_staged_prefix_disposition.py"
)
_CONTROLLER_RELATIVE: Final = (
    "tools/governed_memory_validation/staged_prefix_disposition.py"
)
_CONTRACT_GENERATOR_RELATIVE: Final = (
    "tools/governed_memory_validation/"
    "generate_staged_prefix_disposition_contract.py"
)
_PERMITTED_CANDIDATE_RELATIVE: Final = (
    "tools/governed_memory_validation/phase9_permitted_candidate.py"
)
_ISSUER_RELATIVE: Final = (
    "tools/governed_memory_validation/issue_disposable_installation_live_proof.py"
)
_BOOTSTRAP_RELATIVE: Final = (
    "tools/governed_memory_validation/bootstrap_phase9_disposable_store_substrate.py"
)
_MANAGER_RELATIVE: Final = (
    "tools/governed_memory_validation/execute_phase9_disposable_live_proof_controller.py"
)
_CONTRACT_RELATIVE: Final = (
    "ops/governed_memory/staged_prefix_disposition_contract.json"
)
_SOURCE_PATHS: Final = (
    _PUBLISHER_RELATIVE, _WRAPPER_RELATIVE, _CONTROLLER_RELATIVE,
    _CONTRACT_GENERATOR_RELATIVE,
    _PERMITTED_CANDIDATE_RELATIVE, _ISSUER_RELATIVE, _BOOTSTRAP_RELATIVE,
    _MANAGER_RELATIVE,
    _CONTRACT_RELATIVE,
)
_EXPECTED_CANDIDATE_REF: Final = (
    "refs/tags/governed-memory-phase9j-pre-effect-disposition-000006"
)
if _PUBLISHER_PATH.relative_to(_REPOSITORY_ROOT).as_posix() != _PUBLISHER_RELATIVE:
    raise SystemExit("phase9_staged_prefix_permit_invocation_invalid")

BASE_CANDIDATE_COMMIT: Final = "7b00e321970952818cfeb3534baa8525226f1432"
BASE_CANDIDATE_TREE: Final = "2f75a1efadfe04d934a7d9d1e78f07ad35aa2642"
PACKAGE_MANIFEST_SHA256: Final = (
    "c4f6e657d864a3fc60271dc8e875c12c193b91bb6fc5815314c7dd0182c4af8e"
)
CONTROLLER_RUNTIME_RECEIPT_SHA256: Final = (
    "9884fa9db3be81039b691a25866994d6033c5cf9175030ea82de401a2bbe6334"
)
CONTRACT_SHA256: Final = (
    "50c86899b801dcbfd972c1e09a76c2b501d96015c4df0d12a3790e08c6cd0eb8"
)
PREDECESSOR_ATTEMPT_IDENTITY_SHA256: Final = "d03dd69ec940943e0c7d1d4ac46fed59624ec3bd521f5ba32669108e127fde3c"
SUCCESSOR_ATTEMPT_IDENTITY_SHA256: Final = (
    "3832d4f3568501a2f5961de76fda76efd05a6715afb37457200cb2fd81704c87"
)
AUTHORIZATION_TEXT_SHA256: Final = (
    "063891fc0189b3c4a0fe393200ae50f59b04f5488800dc27a5ce3020880f3e44"
)
THREAD_ID: Final = "019fe927-8367-7f52-86f2-e2b5b43a2390"
PERMIT_SCHEMA: Final = "governed-memory-phase9j-staged-prefix-permit-v2"
PERMIT_RESULT: Final = (
    "exact_staged_prefix_disposition_and_disposable_proof_permitted"
)
STATE_ROOT: Final = Path("/var/lib/governed-memory-controller")
PERMIT_PATH: Final = STATE_ROOT / "phase9j-pre-effect-disposition-permit-000006.json"
PERMIT_STAGING_NAME: Final = "." + PERMIT_PATH.name + ".publishing"
RUNTIME_RECEIPT_PATH: Final = STATE_ROOT / "runtime-receipts" / (CONTROLLER_RUNTIME_RECEIPT_SHA256 + ".json")
ROOT_UID: Final = 0
ROOT_GID: Final = 0
MAX_DOCUMENT_BYTES: Final = 256 * 1024
MAX_GIT_OUTPUT_BYTES: Final = 1024 * 1024
GIT_BINARY: Final = "/usr/bin/git"
_HASH_RE: Final = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_GIT_RE: Final = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_SAFE_ENVIRONMENT: Final = {
    "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
    "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
}


def _bindings_sealed() -> bool:
    return bool(
        PACKAGE_MANIFEST_SHA256 != "0" * 64
        and CONTROLLER_RUNTIME_RECEIPT_SHA256 != "0" * 64
        and CONTRACT_SHA256 != "0" * 64
        and SUCCESSOR_ATTEMPT_IDENTITY_SHA256 != "0" * 64
        and AUTHORIZATION_TEXT_SHA256 != "0" * 64
        and BASE_CANDIDATE_COMMIT != "0" * 40
        and BASE_CANDIDATE_TREE != "0" * 40
    )


class Phase9StagedPrefixPermitPublicationError(RuntimeError):
    """Content-free refusal from permit publication."""


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as error:
        raise Phase9StagedPrefixPermitPublicationError("phase9_staged_prefix_permit_document_invalid") from error


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def _document(raw: bytes, code: str) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_unique)
    except (ValueError, UnicodeError) as error:
        raise Phase9StagedPrefixPermitPublicationError(code) from error
    if type(value) is not dict or _canonical(value) != raw:
        raise Phase9StagedPrefixPermitPublicationError(code)
    return value


def _git(*arguments: str, allow_status: int = 0) -> bytes:
    if not arguments or allow_status not in {0, 1}:
        raise Phase9StagedPrefixPermitPublicationError("phase9_staged_prefix_permit_git_failed")
    try:
        completed = subprocess.run(
            (GIT_BINARY, "-c", "safe.directory=" + str(_REPOSITORY_ROOT),
             "-C", str(_REPOSITORY_ROOT), *arguments),
            cwd="/", env=dict(_SAFE_ENVIRONMENT), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise Phase9StagedPrefixPermitPublicationError("phase9_staged_prefix_permit_git_failed") from error
    if completed.returncode != allow_status or completed.stderr != b"" or len(completed.stdout) > MAX_GIT_OUTPUT_BYTES:
        raise Phase9StagedPrefixPermitPublicationError("phase9_staged_prefix_permit_git_failed")
    return bytes(completed.stdout)


def _git_object(raw: bytes) -> str:
    try:
        value = raw.decode("ascii").rstrip("\n")
    except UnicodeError as error:
        raise Phase9StagedPrefixPermitPublicationError("phase9_staged_prefix_permit_candidate_invalid") from error
    if raw != (value + "\n").encode("ascii") or _GIT_RE.fullmatch(value) is None:
        raise Phase9StagedPrefixPermitPublicationError("phase9_staged_prefix_permit_candidate_invalid")
    return value


def _verify_selected_candidate() -> tuple[str, str, dict[str, str]]:
    try:
        top = _git("rev-parse", "--show-toplevel").decode("utf-8").rstrip("\n")
    except UnicodeError as error:
        raise Phase9StagedPrefixPermitPublicationError("phase9_staged_prefix_permit_candidate_invalid") from error
    if top != str(_REPOSITORY_ROOT) or _git("status", "--porcelain=v1", "--untracked-files=all") != b"":
        raise Phase9StagedPrefixPermitPublicationError("phase9_staged_prefix_permit_candidate_not_clean")
    _git("ls-files", "--error-unmatch", "--stage", "--", *_SOURCE_PATHS)
    tag_commit = _git_object(_git("rev-parse", "--verify", _EXPECTED_CANDIDATE_REF + "^{commit}"))
    tag_tree = _git_object(_git("rev-parse", "--verify", _EXPECTED_CANDIDATE_REF + "^{tree}"))
    commit = _git_object(_git("rev-parse", "--verify", "HEAD^{commit}"))
    tree = _git_object(_git("rev-parse", "--verify", "HEAD^{tree}"))
    base_tree = _git_object(_git("rev-parse", "--verify", BASE_CANDIDATE_COMMIT + "^{tree}"))
    _git("merge-base", "--is-ancestor", BASE_CANDIDATE_COMMIT, "HEAD")
    if commit != tag_commit or tree != tag_tree or base_tree != BASE_CANDIDATE_TREE:
        raise Phase9StagedPrefixPermitPublicationError("phase9_staged_prefix_permit_candidate_invalid")
    blobs: dict[str, str] = {}
    for relative in _SOURCE_PATHS:
        committed = _git_object(_git("rev-parse", "HEAD:" + relative))
        observed = _git_object(_git("hash-object", str(_REPOSITORY_ROOT / relative)))
        if committed != observed:
            raise Phase9StagedPrefixPermitPublicationError("phase9_staged_prefix_permit_candidate_not_clean")
        blobs[relative] = committed
    contract_sha256 = CONTRACT_SHA256
    if type(contract_sha256) is not str or _HASH_RE.fullmatch(contract_sha256) is None:
        raise Phase9StagedPrefixPermitPublicationError("phase9_staged_prefix_permit_binding_not_sealed")
    if _sha((_REPOSITORY_ROOT / _CONTRACT_RELATIVE).read_bytes()) != contract_sha256:
        raise Phase9StagedPrefixPermitPublicationError("phase9_staged_prefix_permit_contract_invalid")
    return commit, tree, blobs


def _read_root_file(path: Path, code: str) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        named = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode) or stat.S_IMODE(opened.st_mode) != 0o400
            or opened.st_uid != ROOT_UID or opened.st_gid != ROOT_GID or opened.st_nlink != 1
            or opened.st_size <= 0 or opened.st_size > MAX_DOCUMENT_BYTES
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise OSError(errno.EPERM, "metadata")
        raw = os.read(descriptor, MAX_DOCUMENT_BYTES + 1)
        after = os.fstat(descriptor)
        if len(raw) != opened.st_size or (opened.st_dev, opened.st_ino, opened.st_mtime_ns, opened.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns):
            raise OSError(errno.EIO, "changed")
        return raw
    except OSError as error:
        raise Phase9StagedPrefixPermitPublicationError(code) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _verify_runtime_receipt() -> None:
    raw = _read_root_file(RUNTIME_RECEIPT_PATH, "phase9_staged_prefix_permit_runtime_receipt_invalid")
    document = _document(raw, "phase9_staged_prefix_permit_runtime_receipt_invalid")
    if (
        _sha(raw) != CONTROLLER_RUNTIME_RECEIPT_SHA256
        or document.get("package_manifest_sha256") != PACKAGE_MANIFEST_SHA256
        or document.get("active_production_state_changed") is not False
        or document.get("production_data_read") is not False
        or document.get("provider_calls") != 0
    ):
        raise Phase9StagedPrefixPermitPublicationError("phase9_staged_prefix_permit_runtime_receipt_invalid")


def _permit(commit: str, tree: str, blobs: Mapping[str, str]) -> bytes:
    contract_sha256 = CONTRACT_SHA256
    if type(contract_sha256) is not str:
        raise Phase9StagedPrefixPermitPublicationError("phase9_staged_prefix_permit_binding_not_sealed")
    unsigned: dict[str, object] = {
        "schema_version": PERMIT_SCHEMA, "result": PERMIT_RESULT,
        "candidate_git_commit": commit, "candidate_git_tree": tree,
        "base_candidate_git_commit": BASE_CANDIDATE_COMMIT,
        "base_candidate_git_tree": BASE_CANDIDATE_TREE,
        "package_manifest_sha256": PACKAGE_MANIFEST_SHA256,
        "controller_runtime_receipt_sha256": CONTROLLER_RUNTIME_RECEIPT_SHA256,
        "contract_sha256": contract_sha256,
        "predecessor_attempt_identity_sha256": PREDECESSOR_ATTEMPT_IDENTITY_SHA256,
        "successor_attempt_identity_sha256": SUCCESSOR_ATTEMPT_IDENTITY_SHA256,
        "authorization_text_sha256": AUTHORIZATION_TEXT_SHA256,
        "thread_id": THREAD_ID, "source_blobs": dict(blobs),
        "authorized_action": (
            "execute_staged_prefix_disposition_and_disposable_live_proof_only"
        ),
        "activation_performed": False, "provider_calls": 0,
        "production_data_read": False, "deletion_performed": False,
    }
    return _canonical({**unsigned, "permit_sha256": _sha(_canonical(unsigned))})


def _write_all(descriptor: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError(errno.EIO, "write")
        view = view[written:]


def _read_publish_member(
    parent_fd: int,
    name: str,
    *,
    allowed_link_counts: frozenset[int],
) -> tuple[bytes, tuple[int, int]] | None:
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        raise Phase9StagedPrefixPermitPublicationError(
            "phase9_staged_prefix_permit_publication_conflict"
        ) from error
    try:
        before = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o400
            or before.st_uid != ROOT_UID
            or before.st_gid != ROOT_GID
            or before.st_nlink not in allowed_link_counts
            or before.st_size <= 0
            or before.st_size > MAX_DOCUMENT_BYTES
            or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise Phase9StagedPrefixPermitPublicationError(
                "phase9_staged_prefix_permit_publication_conflict"
            )
        raw = bytearray()
        while len(raw) <= MAX_DOCUMENT_BYTES:
            block = os.read(
                descriptor,
                min(65536, MAX_DOCUMENT_BYTES + 1 - len(raw)),
            )
            if not block:
                break
            raw.extend(block)
        after = os.fstat(descriptor)
        named_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        stable_before = (
            before.st_dev, before.st_ino, before.st_mode, before.st_uid,
            before.st_gid, before.st_nlink, before.st_size,
            before.st_mtime_ns, before.st_ctime_ns,
        )
        stable_after = (
            after.st_dev, after.st_ino, after.st_mode, after.st_uid,
            after.st_gid, after.st_nlink, after.st_size,
            after.st_mtime_ns, after.st_ctime_ns,
        )
        stable_named = (
            named_after.st_dev, named_after.st_ino, named_after.st_mode,
            named_after.st_uid, named_after.st_gid, named_after.st_nlink,
            named_after.st_size, named_after.st_mtime_ns,
            named_after.st_ctime_ns,
        )
        if (
            len(raw) != before.st_size
            or len(raw) > MAX_DOCUMENT_BYTES
            or stable_after != stable_before
            or stable_named != stable_before
        ):
            raise Phase9StagedPrefixPermitPublicationError(
                "phase9_staged_prefix_permit_publication_conflict"
            )
        return bytes(raw), (before.st_dev, before.st_ino)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _unlink_owned_staging(parent_fd: int, expected_inode: tuple[int, int]) -> None:
    try:
        named = os.stat(PERMIT_STAGING_NAME, dir_fd=parent_fd, follow_symlinks=False)
        if (named.st_dev, named.st_ino) != expected_inode:
            raise Phase9StagedPrefixPermitPublicationError(
                "phase9_staged_prefix_permit_publication_conflict"
            )
        os.unlink(PERMIT_STAGING_NAME, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except Phase9StagedPrefixPermitPublicationError:
        raise
    except OSError as error:
        raise Phase9StagedPrefixPermitPublicationError(
            "phase9_staged_prefix_permit_publication_failed"
        ) from error


def _fsync_publish_member(
    parent_fd: int,
    name: str,
    expected_inode: tuple[int, int],
    *,
    expected_link_count: int,
) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_fd,
        )
        before = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o400
            or before.st_uid != ROOT_UID
            or before.st_gid != ROOT_GID
            or before.st_nlink != expected_link_count
            or (before.st_dev, before.st_ino) != expected_inode
            or (named.st_dev, named.st_ino) != expected_inode
        ):
            raise Phase9StagedPrefixPermitPublicationError(
                "phase9_staged_prefix_permit_publication_conflict"
            )
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        named_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        stable_before = (
            before.st_dev, before.st_ino, before.st_mode, before.st_uid,
            before.st_gid, before.st_nlink, before.st_size,
            before.st_mtime_ns, before.st_ctime_ns,
        )
        stable_after = (
            after.st_dev, after.st_ino, after.st_mode, after.st_uid,
            after.st_gid, after.st_nlink, after.st_size,
            after.st_mtime_ns, after.st_ctime_ns,
        )
        stable_named = (
            named_after.st_dev, named_after.st_ino, named_after.st_mode,
            named_after.st_uid, named_after.st_gid, named_after.st_nlink,
            named_after.st_size, named_after.st_mtime_ns,
            named_after.st_ctime_ns,
        )
        if stable_after != stable_before or stable_named != stable_before:
            raise Phase9StagedPrefixPermitPublicationError(
                "phase9_staged_prefix_permit_publication_conflict"
            )
        closing_fd = descriptor
        descriptor = -1
        os.close(closing_fd)
    except Phase9StagedPrefixPermitPublicationError:
        raise
    except OSError as error:
        raise Phase9StagedPrefixPermitPublicationError(
            "phase9_staged_prefix_permit_publication_failed"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _publish_create_once(raw: bytes) -> None:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0)
    parent_fd = leaf_fd = -1
    try:
        parent_fd = os.open(STATE_ROOT, flags)
        parent = os.fstat(parent_fd)
        if not stat.S_ISDIR(parent.st_mode) or stat.S_IMODE(parent.st_mode) != 0o700 or parent.st_uid != ROOT_UID or parent.st_gid != ROOT_GID:
            raise OSError(errno.EPERM, "parent")
        final = _read_publish_member(
            parent_fd, PERMIT_PATH.name, allowed_link_counts=frozenset({1, 2})
        )
        staging = _read_publish_member(
            parent_fd,
            PERMIT_STAGING_NAME,
            allowed_link_counts=(
                frozenset({2}) if final is not None else frozenset({1})
            ),
        )
        if final is not None:
            final_raw, final_inode = final
            if final_raw != raw:
                raise Phase9StagedPrefixPermitPublicationError(
                    "phase9_staged_prefix_permit_replay_mismatch"
                )
            if staging is None:
                if os.stat(PERMIT_PATH.name, dir_fd=parent_fd, follow_symlinks=False).st_nlink != 1:
                    raise Phase9StagedPrefixPermitPublicationError(
                        "phase9_staged_prefix_permit_publication_conflict"
                    )
                return
            staging_raw, staging_inode = staging
            if staging_raw != raw or staging_inode != final_inode:
                raise Phase9StagedPrefixPermitPublicationError(
                    "phase9_staged_prefix_permit_publication_conflict"
                )
            _fsync_publish_member(
                parent_fd,
                PERMIT_PATH.name,
                final_inode,
                expected_link_count=2,
            )
            os.fsync(parent_fd)
            _unlink_owned_staging(parent_fd, staging_inode)
            if _read_publish_member(
                parent_fd, PERMIT_PATH.name, allowed_link_counts=frozenset({1})
            ) != (raw, final_inode):
                raise Phase9StagedPrefixPermitPublicationError(
                    "phase9_staged_prefix_permit_publication_failed"
                )
            return
        if staging is not None:
            staging_raw, staging_inode = staging
            if staging_raw != raw:
                raise Phase9StagedPrefixPermitPublicationError(
                    "phase9_staged_prefix_permit_publication_conflict"
                )
            _fsync_publish_member(
                parent_fd,
                PERMIT_STAGING_NAME,
                staging_inode,
                expected_link_count=1,
            )
            os.fsync(parent_fd)
            try:
                os.link(
                    PERMIT_STAGING_NAME,
                    PERMIT_PATH.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as error:
                raise Phase9StagedPrefixPermitPublicationError(
                    "phase9_staged_prefix_permit_publication_conflict"
                ) from error
            os.fsync(parent_fd)
            linked = _read_publish_member(
                parent_fd, PERMIT_PATH.name, allowed_link_counts=frozenset({2})
            )
            if linked != (raw, staging_inode):
                raise Phase9StagedPrefixPermitPublicationError(
                    "phase9_staged_prefix_permit_publication_failed"
                )
            _unlink_owned_staging(parent_fd, staging_inode)
            if _read_publish_member(
                parent_fd, PERMIT_PATH.name, allowed_link_counts=frozenset({1})
            ) != (raw, staging_inode):
                raise Phase9StagedPrefixPermitPublicationError(
                    "phase9_staged_prefix_permit_publication_failed"
                )
            return
        leaf_fd = os.open(
            PERMIT_STAGING_NAME,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            0o400,
            dir_fd=parent_fd,
        )
        os.fchown(leaf_fd, ROOT_UID, ROOT_GID)
        os.fchmod(leaf_fd, 0o400)
        _write_all(leaf_fd, raw)
        os.fsync(leaf_fd)
        staging_stat = os.fstat(leaf_fd)
        staging_inode = (staging_stat.st_dev, staging_stat.st_ino)
        closing_fd = leaf_fd
        leaf_fd = -1
        os.close(closing_fd)
        os.fsync(parent_fd)
        if _read_publish_member(
            parent_fd, PERMIT_STAGING_NAME, allowed_link_counts=frozenset({1})
        ) != (raw, staging_inode):
            raise Phase9StagedPrefixPermitPublicationError(
                "phase9_staged_prefix_permit_publication_failed"
            )
        try:
            os.link(
                PERMIT_STAGING_NAME,
                PERMIT_PATH.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError as error:
            raise Phase9StagedPrefixPermitPublicationError(
                "phase9_staged_prefix_permit_publication_conflict"
            ) from error
        os.fsync(parent_fd)
        if _read_publish_member(
            parent_fd, PERMIT_PATH.name, allowed_link_counts=frozenset({2})
        ) != (raw, staging_inode):
            raise Phase9StagedPrefixPermitPublicationError(
                "phase9_staged_prefix_permit_publication_failed"
            )
        _unlink_owned_staging(parent_fd, staging_inode)
        if _read_publish_member(
            parent_fd, PERMIT_PATH.name, allowed_link_counts=frozenset({1})
        ) != (raw, staging_inode):
            raise Phase9StagedPrefixPermitPublicationError(
                "phase9_staged_prefix_permit_publication_failed"
            )
    except Phase9StagedPrefixPermitPublicationError:
        raise
    except OSError as error:
        raise Phase9StagedPrefixPermitPublicationError("phase9_staged_prefix_permit_publication_failed") from error
    finally:
        if leaf_fd >= 0:
            os.close(leaf_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def publish_phase9_staged_prefix_permit() -> Mapping[str, object]:
    if (
        not _ISOLATED_RUNTIME_AT_START
        or not _DONT_WRITE_BYTECODE_AT_START
        or sys.platform != "linux"
        or sys.executable != _PREIMPORT_CONTROLLER_PYTHON
    ):
        raise Phase9StagedPrefixPermitPublicationError("phase9_staged_prefix_permit_runtime_isolation_required")
    if not _preimport_manager_lineage_valid():
        raise Phase9StagedPrefixPermitPublicationError(
            "phase9_staged_prefix_permit_manager_lineage_required"
        )
    if os.geteuid() != ROOT_UID or os.getegid() != ROOT_GID:
        raise Phase9StagedPrefixPermitPublicationError("phase9_staged_prefix_permit_root_required")
    if not _bindings_sealed():
        raise Phase9StagedPrefixPermitPublicationError(
            "phase9_staged_prefix_permit_binding_not_sealed"
        )
    commit, tree, blobs = _verify_selected_candidate()
    _verify_runtime_receipt()
    raw = _permit(commit, tree, blobs)
    # Reprove tag selection and source bytes immediately before publication.
    if _verify_selected_candidate() != (commit, tree, blobs):
        raise Phase9StagedPrefixPermitPublicationError("phase9_staged_prefix_permit_candidate_changed")
    _publish_create_once(raw)
    return _document(raw, "phase9_staged_prefix_permit_publication_failed")


def main(argv: Sequence[str] | None = None) -> int:
    if tuple(sys.argv[1:] if argv is None else argv):
        raise Phase9StagedPrefixPermitPublicationError("phase9_staged_prefix_permit_arguments_refused")
    value = publish_phase9_staged_prefix_permit()
    sys.stdout.buffer.write(_canonical(value) + b"\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Phase9StagedPrefixPermitPublicationError as error:
        sys.stderr.write(str(error) + "\n")
        raise SystemExit(1) from None
