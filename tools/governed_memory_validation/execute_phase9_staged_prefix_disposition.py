#!/usr/bin/env python3
from __future__ import annotations

"""Execute the exact Phase 9 v7 staged-prefix-to-v8 disposition."""

import os
import sys


_PREIMPORT_CONTROLLER_RUNTIME_RECEIPT_SHA256 = (
    "ed0b3518484eec292f35f8b996bccefd01e13038105634016b4253a8c2a732a7"
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
    sys.stderr.write("phase9_staged_prefix_disposition_runtime_isolation_required\n")
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
    """Bind direct disposition to the exact Phase 9J manager process."""

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
        entrypoint_path = os.path.realpath(__file__)
        repository_root = os.path.dirname(
            os.path.dirname(os.path.dirname(entrypoint_path))
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
    sys.stderr.write("phase9_staged_prefix_disposition_manager_lineage_required\n")
    raise SystemExit(1)

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import errno
import hashlib
import importlib
import json
from pathlib import Path
import re
import stat
import subprocess
from types import ModuleType
from typing import Final


_ENTRYPOINT_PATH: Final = Path(__file__).resolve(strict=True)
_REPOSITORY_ROOT: Final = _ENTRYPOINT_PATH.parents[2]
_ENTRYPOINT_RELATIVE: Final = (
    "tools/governed_memory_validation/execute_phase9_staged_prefix_disposition.py"
)
_PUBLISHER_RELATIVE: Final = (
    "tools/governed_memory_validation/publish_phase9_staged_prefix_permit.py"
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
_CONTRACT_RELATIVE: Final = "ops/governed_memory/staged_prefix_disposition_contract.json"
_SOURCE_PATHS: Final = (
    _PUBLISHER_RELATIVE,
    _ENTRYPOINT_RELATIVE,
    _CONTROLLER_RELATIVE,
    _CONTRACT_GENERATOR_RELATIVE,
    _PERMITTED_CANDIDATE_RELATIVE,
    _ISSUER_RELATIVE,
    _BOOTSTRAP_RELATIVE,
    _MANAGER_RELATIVE,
    _CONTRACT_RELATIVE,
)
if _ENTRYPOINT_PATH.relative_to(_REPOSITORY_ROOT).as_posix() != _ENTRYPOINT_RELATIVE:
    raise SystemExit("phase9_staged_prefix_disposition_invocation_invalid")

PACKAGE_MANIFEST_SHA256: Final = "0" * 64
CONTROLLER_RUNTIME_RECEIPT_SHA256: Final = "0" * 64
CONTRACT_SHA256: Final = "0" * 64
PREDECESSOR_ATTEMPT_IDENTITY_SHA256: Final = (
    "942d1d443617c16b161b254a033fc7b0273a0fb26842e5f4d7d93c2fbfda8156"
)
SUCCESSOR_ATTEMPT_IDENTITY_SHA256: Final = "0" * 64
AUTHORIZATION_TEXT_SHA256: Final = (
    "063891fc0189b3c4a0fe393200ae50f59b04f5488800dc27a5ce3020880f3e44"
)
THREAD_ID: Final = "019fe927-8367-7f52-86f2-e2b5b43a2390"
BASE_CANDIDATE_COMMIT: Final = "e93a5ff85629fc20e2bfb3cc81182974531570cc"
BASE_CANDIDATE_TREE: Final = "416d3c2c45d8fdf06d4fb78a99c1ad93651bae17"
PERMIT_SCHEMA: Final = "governed-memory-phase9j-staged-prefix-permit-v2"
PERMIT_RESULT: Final = (
    "exact_staged_prefix_disposition_and_disposable_proof_permitted"
)
EXPECTED_CANDIDATE_REF: Final = (
    "refs/tags/governed-memory-phase9j-pre-effect-disposition-000005"
)
PERMIT_PATH: Final = Path(
    "/var/lib/governed-memory-controller/phase9j-pre-effect-disposition-permit-000005.json"
)
STATE_ROOT: Final = Path("/var/lib/governed-memory-controller")
CONTRACT_ROOT: Final = STATE_ROOT / "pre-effect-dispositions"
CONTRACT_PATH: Final = (
    CONTRACT_ROOT / "phase9-v8-staged-to-v9-000005.contract.json"
)
CONTRACT_STAGING_NAME: Final = "." + CONTRACT_PATH.name + ".publishing"
DISPOSITION_ID: Final = "phase9-v8-staged-to-v9-000005"
FAILED_TAG_REF: Final = (
    "refs/tags/governed-memory-phase9j-pre-effect-disposition-000004"
)
FAILED_TAG_COMMIT: Final = "e93a5ff85629fc20e2bfb3cc81182974531570cc"
FAILED_TAG_TREE: Final = "416d3c2c45d8fdf06d4fb78a99c1ad93651bae17"
FAILED_PACKAGE_MANIFEST_SHA256: Final = (
    "634669dbca4f2ccfed929951bcdd0d555d19e53f9b736ee21b42217e8e8cd629"
)
FAILED_CONTROLLER_RUNTIME_RECEIPT_SHA256: Final = (
    "ed0b3518484eec292f35f8b996bccefd01e13038105634016b4253a8c2a732a7"
)
CORRECTED_GENERATION: Final = "000005"
ROOT_UID: Final = 0
ROOT_GID: Final = 0
MAX_DOCUMENT_BYTES: Final = 256 * 1024
MAX_GIT_OUTPUT_BYTES: Final = 1024 * 1024
GIT_BINARY: Final = "/usr/bin/git"
_HASH_RE: Final = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_GIT_RE: Final = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_SAFE_GIT_ENVIRONMENT: Final = {
    "PATH": "/usr/bin:/bin",
    "LANG": "C",
    "LC_ALL": "C",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONNOUSERSITE": "1",
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
_PERMIT_KEYS: Final = {
    "schema_version", "result", "candidate_git_commit", "candidate_git_tree",
    "base_candidate_git_commit", "base_candidate_git_tree",
    "package_manifest_sha256", "controller_runtime_receipt_sha256",
    "contract_sha256", "predecessor_attempt_identity_sha256",
    "successor_attempt_identity_sha256", "authorization_text_sha256", "thread_id",
    "source_blobs", "authorized_action", "activation_performed", "provider_calls",
    "production_data_read", "deletion_performed", "permit_sha256",
}


class Phase9StagedPrefixDispositionEntrypointError(RuntimeError):
    """Content-free refusal from the exact production entrypoint."""


@dataclass(frozen=True, slots=True)
class CandidateIdentity:
    commit: str
    tree: str
    source_blobs: tuple[tuple[str, str], ...]


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as error:
        raise Phase9StagedPrefixDispositionEntrypointError(
            "phase9_staged_prefix_disposition_document_invalid"
        ) from error


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def _document(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_unique)
    except (ValueError, UnicodeError) as error:
        raise Phase9StagedPrefixDispositionEntrypointError(
            "phase9_staged_prefix_disposition_permit_invalid"
        ) from error
    if type(value) is not dict or _canonical(value) != raw:
        raise Phase9StagedPrefixDispositionEntrypointError(
            "phase9_staged_prefix_disposition_permit_invalid"
        )
    return value


def _read_root_file(path: Path, *, maximum: int) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        )
        opened = os.fstat(descriptor)
        named = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o400
            or opened.st_uid != ROOT_UID or opened.st_gid != ROOT_GID
            or opened.st_nlink != 1 or opened.st_size <= 0
            or opened.st_size > maximum
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise OSError(errno.EPERM, "metadata")
        raw = bytearray()
        while len(raw) <= maximum:
            block = os.read(descriptor, min(65536, maximum + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
        after = os.fstat(descriptor)
        before_identity = (
            opened.st_dev, opened.st_ino, opened.st_mode, opened.st_uid,
            opened.st_gid, opened.st_nlink, opened.st_size, opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
        after_identity = (
            after.st_dev, after.st_ino, after.st_mode, after.st_uid,
            after.st_gid, after.st_nlink, after.st_size, after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if len(raw) != opened.st_size or len(raw) > maximum or before_identity != after_identity:
            raise OSError(errno.EIO, "changed")
        return bytes(raw)
    except OSError as error:
        raise Phase9StagedPrefixDispositionEntrypointError(
            "phase9_staged_prefix_disposition_permit_invalid"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_and_verify_permit() -> Mapping[str, object]:
    document = _document(_read_root_file(PERMIT_PATH, maximum=MAX_DOCUMENT_BYTES))
    if set(document) != _PERMIT_KEYS:
        raise Phase9StagedPrefixDispositionEntrypointError(
            "phase9_staged_prefix_disposition_permit_invalid"
        )
    unsigned = dict(document)
    permit_sha256 = unsigned.pop("permit_sha256", None)
    exact = {
        "schema_version": PERMIT_SCHEMA,
        "result": PERMIT_RESULT,
        "base_candidate_git_commit": BASE_CANDIDATE_COMMIT,
        "base_candidate_git_tree": BASE_CANDIDATE_TREE,
        "package_manifest_sha256": PACKAGE_MANIFEST_SHA256,
        "controller_runtime_receipt_sha256": CONTROLLER_RUNTIME_RECEIPT_SHA256,
        "contract_sha256": CONTRACT_SHA256,
        "predecessor_attempt_identity_sha256": PREDECESSOR_ATTEMPT_IDENTITY_SHA256,
        "successor_attempt_identity_sha256": SUCCESSOR_ATTEMPT_IDENTITY_SHA256,
        "authorization_text_sha256": AUTHORIZATION_TEXT_SHA256,
        "thread_id": THREAD_ID,
        "authorized_action": (
            "execute_staged_prefix_disposition_and_disposable_live_proof_only"
        ),
        "activation_performed": False,
        "provider_calls": 0,
        "production_data_read": False,
        "deletion_performed": False,
    }
    blobs = document.get("source_blobs")
    if (
        any(document.get(key) != value for key, value in exact.items())
        or type(document.get("candidate_git_commit")) is not str
        or _GIT_RE.fullmatch(str(document["candidate_git_commit"])) is None
        or type(document.get("candidate_git_tree")) is not str
        or _GIT_RE.fullmatch(str(document["candidate_git_tree"])) is None
        or type(blobs) is not dict or set(blobs) != set(_SOURCE_PATHS)
        or any(type(value) is not str or _GIT_RE.fullmatch(value) is None for value in blobs.values())
        or type(permit_sha256) is not str or _HASH_RE.fullmatch(permit_sha256) is None
        or permit_sha256 != _sha(_canonical(unsigned))
    ):
        raise Phase9StagedPrefixDispositionEntrypointError(
            "phase9_staged_prefix_disposition_permit_invalid"
        )
    return document


def _git(*arguments: str, maximum: int = MAX_GIT_OUTPUT_BYTES) -> bytes:
    if not arguments or any(type(value) is not str or not value for value in arguments):
        raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_git_failed")
    try:
        completed = subprocess.run(
            (GIT_BINARY, "-c", "safe.directory=" + str(_REPOSITORY_ROOT),
             "-C", str(_REPOSITORY_ROOT), *arguments),
            cwd="/", env=dict(_SAFE_GIT_ENVIRONMENT), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_git_failed") from error
    if (
        completed.returncode != 0 or completed.stderr != b""
        or type(completed.stdout) is not bytes or len(completed.stdout) > maximum
    ):
        raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_git_failed")
    return completed.stdout


def _git_object(raw: bytes) -> str:
    try:
        value = raw.decode("ascii").rstrip("\n")
    except UnicodeError as error:
        raise Phase9StagedPrefixDispositionEntrypointError(
            "phase9_staged_prefix_disposition_candidate_identity_invalid"
        ) from error
    if raw != (value + "\n").encode("ascii") or _GIT_RE.fullmatch(value) is None:
        raise Phase9StagedPrefixDispositionEntrypointError(
            "phase9_staged_prefix_disposition_candidate_identity_invalid"
        )
    return value


def _verified_candidate_identity(permit: Mapping[str, object]) -> CandidateIdentity:
    try:
        if _ENTRYPOINT_PATH.resolve(strict=True) != _ENTRYPOINT_PATH or _REPOSITORY_ROOT.resolve(strict=True) != _REPOSITORY_ROOT:
            raise OSError(errno.EPERM, "path")
        top = _git("rev-parse", "--show-toplevel").decode("utf-8").rstrip("\n")
    except (OSError, UnicodeError) as error:
        raise Phase9StagedPrefixDispositionEntrypointError(
            "phase9_staged_prefix_disposition_candidate_path_invalid"
        ) from error
    if top != str(_REPOSITORY_ROOT):
        raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_candidate_path_invalid")
    if _git("status", "--porcelain=v1", "--untracked-files=all") != b"":
        raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_candidate_not_clean")
    _git("ls-files", "--error-unmatch", "--stage", "--", *_SOURCE_PATHS)
    commit = _git_object(_git("rev-parse", "--verify", "HEAD^{commit}"))
    tree = _git_object(_git("rev-parse", "--verify", "HEAD^{tree}"))
    tag_commit = _git_object(
        _git("rev-parse", "--verify", EXPECTED_CANDIDATE_REF + "^{commit}")
    )
    tag_tree = _git_object(
        _git("rev-parse", "--verify", EXPECTED_CANDIDATE_REF + "^{tree}")
    )
    blobs = permit.get("source_blobs")
    if type(blobs) is not dict:
        raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_permit_invalid")
    observed: list[tuple[str, str]] = []
    for relative in _SOURCE_PATHS:
        committed = _git_object(_git("rev-parse", "HEAD:" + relative))
        filesystem = _git_object(_git("hash-object", str(_REPOSITORY_ROOT / relative)))
        if committed != filesystem or committed != blobs.get(relative):
            raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_candidate_identity_invalid")
        observed.append((relative, committed))
    if (
        commit != tag_commit
        or tree != tag_tree
        or commit != permit.get("candidate_git_commit")
        or tree != permit.get("candidate_git_tree")
    ):
        raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_candidate_identity_invalid")
    return CandidateIdentity(commit, tree, tuple(observed))


def _reverify_candidate(permit: Mapping[str, object], expected: CandidateIdentity) -> None:
    if type(expected) is not CandidateIdentity or _verified_candidate_identity(permit) != expected:
        raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_candidate_changed")


def _read_repository_contract() -> bytes:
    path = _REPOSITORY_ROOT / _CONTRACT_RELATIVE
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        named = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode) or opened.st_mode & 0o022
            or opened.st_nlink != 1 or opened.st_size <= 0
            or opened.st_size > MAX_DOCUMENT_BYTES
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise OSError(errno.EPERM, "metadata")
        raw = os.read(descriptor, MAX_DOCUMENT_BYTES + 1)
        if len(raw) != opened.st_size or _sha(raw) != CONTRACT_SHA256:
            raise OSError(errno.EIO, "content")
        return raw
    except OSError as error:
        raise Phase9StagedPrefixDispositionEntrypointError(
            "phase9_staged_prefix_disposition_repository_contract_invalid"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_all(descriptor: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError(errno.EIO, "write")
        view = view[written:]


def _read_contract_member(
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
        raise Phase9StagedPrefixDispositionEntrypointError(
            "phase9_staged_prefix_disposition_contract_publication_conflict"
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
            raise Phase9StagedPrefixDispositionEntrypointError(
                "phase9_staged_prefix_disposition_contract_publication_conflict"
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
        if len(raw) != before.st_size or stable_after != stable_before or stable_named != stable_before:
            raise Phase9StagedPrefixDispositionEntrypointError(
                "phase9_staged_prefix_disposition_contract_publication_conflict"
            )
        return bytes(raw), (before.st_dev, before.st_ino)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _unlink_contract_staging(parent_fd: int, expected_inode: tuple[int, int]) -> None:
    try:
        named = os.stat(CONTRACT_STAGING_NAME, dir_fd=parent_fd, follow_symlinks=False)
        if (named.st_dev, named.st_ino) != expected_inode:
            raise Phase9StagedPrefixDispositionEntrypointError(
                "phase9_staged_prefix_disposition_contract_publication_conflict"
            )
        os.unlink(CONTRACT_STAGING_NAME, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except Phase9StagedPrefixDispositionEntrypointError:
        raise
    except OSError as error:
        raise Phase9StagedPrefixDispositionEntrypointError(
            "phase9_staged_prefix_disposition_contract_install_invalid"
        ) from error


def _fsync_contract_member(
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
            raise Phase9StagedPrefixDispositionEntrypointError(
                "phase9_staged_prefix_disposition_contract_publication_conflict"
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
            raise Phase9StagedPrefixDispositionEntrypointError(
                "phase9_staged_prefix_disposition_contract_publication_conflict"
            )
        closing_fd = descriptor
        descriptor = -1
        os.close(closing_fd)
    except Phase9StagedPrefixDispositionEntrypointError:
        raise
    except OSError as error:
        raise Phase9StagedPrefixDispositionEntrypointError(
            "phase9_staged_prefix_disposition_contract_install_invalid"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _install_create_once(*, state_root: Path, contract_path: Path, raw: bytes,
                         expected_uid: int, expected_gid: int) -> None:
    contract_root = contract_path.parent
    if contract_root.parent != state_root or not raw or len(raw) > MAX_DOCUMENT_BYTES:
        raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_contract_install_invalid")
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0)
    state_fd = root_fd = leaf_fd = -1
    try:
        state_fd = os.open(state_root, flags)
        state = os.fstat(state_fd)
        if not stat.S_ISDIR(state.st_mode) or stat.S_IMODE(state.st_mode) != 0o700 or state.st_uid != expected_uid or state.st_gid != expected_gid:
            raise OSError(errno.EPERM, "state")
        try:
            os.mkdir(contract_root.name, 0o700, dir_fd=state_fd)
            os.fsync(state_fd)
        except FileExistsError:
            pass
        root_fd = os.open(contract_root.name, flags, dir_fd=state_fd)
        root = os.fstat(root_fd)
        if not stat.S_ISDIR(root.st_mode) or stat.S_IMODE(root.st_mode) != 0o700 or root.st_uid != expected_uid or root.st_gid != expected_gid:
            raise OSError(errno.EPERM, "root")
        final = _read_contract_member(
            root_fd, contract_path.name, allowed_link_counts=frozenset({1, 2})
        )
        staging = _read_contract_member(
            root_fd,
            CONTRACT_STAGING_NAME,
            allowed_link_counts=(
                frozenset({2}) if final is not None else frozenset({1})
            ),
        )
        if final is not None:
            final_raw, final_inode = final
            if final_raw != raw:
                raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_contract_replay_mismatch")
            if staging is None:
                if os.stat(contract_path.name, dir_fd=root_fd, follow_symlinks=False).st_nlink != 1:
                    raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_contract_publication_conflict")
                return
            staging_raw, staging_inode = staging
            if staging_raw != raw or staging_inode != final_inode:
                raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_contract_publication_conflict")
            _fsync_contract_member(
                root_fd,
                contract_path.name,
                final_inode,
                expected_link_count=2,
            )
            os.fsync(root_fd)
            _unlink_contract_staging(root_fd, staging_inode)
            if _read_contract_member(root_fd, contract_path.name, allowed_link_counts=frozenset({1})) != (raw, final_inode):
                raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_contract_install_invalid")
            return
        if staging is not None:
            staging_raw, staging_inode = staging
            if staging_raw != raw:
                raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_contract_publication_conflict")
            _fsync_contract_member(
                root_fd,
                CONTRACT_STAGING_NAME,
                staging_inode,
                expected_link_count=1,
            )
            os.fsync(root_fd)
            try:
                os.link(CONTRACT_STAGING_NAME, contract_path.name, src_dir_fd=root_fd,
                        dst_dir_fd=root_fd, follow_symlinks=False)
            except FileExistsError as error:
                raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_contract_publication_conflict") from error
            os.fsync(root_fd)
            if _read_contract_member(root_fd, contract_path.name, allowed_link_counts=frozenset({2})) != (raw, staging_inode):
                raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_contract_install_invalid")
            _unlink_contract_staging(root_fd, staging_inode)
            if _read_contract_member(root_fd, contract_path.name, allowed_link_counts=frozenset({1})) != (raw, staging_inode):
                raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_contract_install_invalid")
            return
        leaf_fd = os.open(
            CONTRACT_STAGING_NAME,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            0o400,
            dir_fd=root_fd,
        )
        os.fchown(leaf_fd, expected_uid, expected_gid)
        os.fchmod(leaf_fd, 0o400)
        _write_all(leaf_fd, raw)
        os.fsync(leaf_fd)
        staged = os.fstat(leaf_fd)
        staging_inode = (staged.st_dev, staged.st_ino)
        closing_fd = leaf_fd
        leaf_fd = -1
        os.close(closing_fd)
        os.fsync(root_fd)
        if _read_contract_member(root_fd, CONTRACT_STAGING_NAME, allowed_link_counts=frozenset({1})) != (raw, staging_inode):
            raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_contract_install_invalid")
        try:
            os.link(CONTRACT_STAGING_NAME, contract_path.name, src_dir_fd=root_fd,
                    dst_dir_fd=root_fd, follow_symlinks=False)
        except FileExistsError as error:
            raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_contract_publication_conflict") from error
        os.fsync(root_fd)
        if _read_contract_member(root_fd, contract_path.name, allowed_link_counts=frozenset({2})) != (raw, staging_inode):
            raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_contract_install_invalid")
        _unlink_contract_staging(root_fd, staging_inode)
        if _read_contract_member(root_fd, contract_path.name, allowed_link_counts=frozenset({1})) != (raw, staging_inode):
            raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_contract_install_invalid")
    except Phase9StagedPrefixDispositionEntrypointError:
        raise
    except OSError as error:
        raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_contract_install_invalid") from error
    finally:
        for descriptor in (leaf_fd, root_fd, state_fd):
            if descriptor >= 0:
                os.close(descriptor)


def _load_disposition() -> ModuleType:
    if str(_REPOSITORY_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPOSITORY_ROOT))
    try:
        return importlib.import_module("tools.governed_memory_validation.staged_prefix_disposition")
    except Exception as error:
        raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_controller_import_invalid") from error


def _expectation(disposition: ModuleType) -> object:
    if (
        disposition.PRODUCTION_REPOSITORY_ROOT != _REPOSITORY_ROOT
        or disposition.PRODUCTION_REPOSITORY_CONTRACT_SOURCE
        != _CONTRACT_RELATIVE
        or disposition.PRODUCTION_DURABLE_CONTRACT_PATH != CONTRACT_PATH
        or disposition.PRODUCTION_STATE_ROOT != STATE_ROOT
        or disposition.PRODUCTION_OLD_TAG_REF != FAILED_TAG_REF
        or disposition.PRODUCTION_FAILED_PACKAGE_MANIFEST_SHA256
        != FAILED_PACKAGE_MANIFEST_SHA256
        or disposition.PRODUCTION_FAILED_RUNTIME_RECEIPT_SHA256
        != FAILED_CONTROLLER_RUNTIME_RECEIPT_SHA256
        or disposition.PRODUCTION_CORRECTED_GENERATION != CORRECTED_GENERATION
        or disposition.production_failed_prefix_identity_sha256()
        != PREDECESSOR_ATTEMPT_IDENTITY_SHA256
        or disposition.production_corrected_attempt_identity_sha256(
            package_manifest_sha256=PACKAGE_MANIFEST_SHA256,
            controller_runtime_receipt_sha256=(
                CONTROLLER_RUNTIME_RECEIPT_SHA256
            ),
        )
        != SUCCESSOR_ATTEMPT_IDENTITY_SHA256
    ):
        raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_production_binding_not_sealed")
    return disposition.ReviewedStagedPrefixExpectation(
        contract_sha256=CONTRACT_SHA256,
        disposition_id=DISPOSITION_ID,
        old_tag_ref=FAILED_TAG_REF,
        old_tag_commit=FAILED_TAG_COMMIT,
        old_tag_tree=FAILED_TAG_TREE,
        failed_package_manifest_sha256=FAILED_PACKAGE_MANIFEST_SHA256,
        failed_controller_runtime_receipt_sha256=(
            FAILED_CONTROLLER_RUNTIME_RECEIPT_SHA256
        ),
        corrected_generation=CORRECTED_GENERATION,
        corrected_package_manifest_sha256=PACKAGE_MANIFEST_SHA256,
        corrected_controller_runtime_receipt_sha256=(
            CONTROLLER_RUNTIME_RECEIPT_SHA256
        ),
    )


def execute_exact_production_disposition() -> Mapping[str, object]:
    if (
        not _ISOLATED_RUNTIME_AT_START
        or not _DONT_WRITE_BYTECODE_AT_START
        or sys.platform != "linux"
        or sys.executable != _PREIMPORT_CONTROLLER_PYTHON
    ):
        raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_runtime_isolation_required")
    if not _preimport_manager_lineage_valid():
        raise Phase9StagedPrefixDispositionEntrypointError(
            "phase9_staged_prefix_disposition_manager_lineage_required"
        )
    if os.geteuid() != ROOT_UID or os.getegid() != ROOT_GID:
        raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_root_required")
    if not _bindings_sealed():
        raise Phase9StagedPrefixDispositionEntrypointError(
            "phase9_staged_prefix_disposition_binding_not_sealed"
        )
    permit = _read_and_verify_permit()  # Authority precedes controller import.
    candidate = _verified_candidate_identity(permit)
    disposition = _load_disposition()
    expectation = _expectation(disposition)
    if (
        disposition.PRODUCTION_DURABLE_CONTRACT_PATH != CONTRACT_PATH
        or disposition.PRODUCTION_STATE_ROOT != STATE_ROOT
    ):
        raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_production_path_invalid")
    raw = _read_repository_contract()
    _reverify_candidate(permit, candidate)
    _install_create_once(state_root=STATE_ROOT, contract_path=CONTRACT_PATH, raw=raw,
                         expected_uid=ROOT_UID, expected_gid=ROOT_GID)
    _reverify_candidate(permit, candidate)
    try:
        receipt = disposition.execute_staged_prefix_disposition(
            disposition.production_disposition_paths(), expectation
        )
    except disposition.StagedPrefixDispositionError as error:
        raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_refused") from error
    _reverify_candidate(permit, candidate)
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    if tuple(sys.argv[1:] if argv is None else argv):
        raise Phase9StagedPrefixDispositionEntrypointError("phase9_staged_prefix_disposition_arguments_refused")
    receipt = execute_exact_production_disposition()
    sys.stdout.buffer.write(_canonical({
        "contract_sha256": receipt.get("contract_sha256"),
        "corrected_controller_runtime_receipt_sha256": receipt.get(
            "corrected_controller_runtime_receipt_sha256"
        ),
        "corrected_generation": receipt.get("corrected_generation"),
        "corrected_package_manifest_sha256": receipt.get(
            "corrected_package_manifest_sha256"
        ),
        "result": receipt.get("result"),
        "tombstone_sha256": receipt.get("tombstone_sha256"),
    }) + b"\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Phase9StagedPrefixDispositionEntrypointError as error:
        sys.stderr.write(str(error) + "\n")
        raise SystemExit(1) from None
