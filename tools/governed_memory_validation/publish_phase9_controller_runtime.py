#!/usr/bin/env python3
from __future__ import annotations

"""Publish the exact Phase 9J controller runtime from one clean candidate.

This repository-only controller is deliberately excluded from the sealed
release.  It accepts no path, command, artifact, nonce, or authorization input.
All package and runtime inputs come from the exact checked-in manifest and the
fixed incoming roots selected by the packaged runtime contract.
"""

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import argparse
import ctypes
import errno
import fcntl
import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
from types import MappingProxyType, ModuleType
from typing import Final


_DONT_WRITE_BYTECODE_AT_START: Final = sys.dont_write_bytecode
sys.dont_write_bytecode = True


ROOT: Final = Path(__file__).resolve().parents[2]
CONTROLLER_RELATIVE: Final = (
    "tools/governed_memory_validation/publish_phase9_controller_runtime.py"
)
GIT_BINARY: Final = "/usr/bin/git"
ROOT_UID: Final = 0
ROOT_GID: Final = 0
BUILD_INTENT_ROOT: Final = PurePosixPath(
    "/var/lib/governed-memory-controller"
)
BUILD_INTENT_SCHEMA: Final = (
    "governed-memory-phase9j-runtime-publication-build-intent-v1"
)
RECEIPT_SCHEMA: Final = (
    "governed-memory-phase9j-runtime-publication-controller-receipt-v1"
)
BUILD_NONCE_DOMAIN: Final = (
    b"governed-memory-phase9j-runtime-publication-build-nonce-v1\x00"
)
SUBSTRATE_SPECIFICATION_SHA256: Final = (
    "883564be18c159544f8785ace4ad1b46abaa2989bb76ad9f03f7a8299d862a85"
)
SUBSTRATE_ARCHIVE_SHA256: Final = (
    "506191be3ee7bd190a8834dcdc1b3bc70aab50608deccc711935aa007239cabd"
)
SUBSTRATE_ARCHIVE_NAME: Final = (
    "cpython-3.12.13+20260807-x86_64-unknown-linux-gnu-"
    "install_only_stripped.tar.gz"
)
SUBSTRATE_SYMLINK_MAPPING_SHA256: Final = (
    "38bd37b4098179fe5997e9e5709eb833d4cfcfa2685cfad89d3f6676a2a87ac7"
)
SUBSTRATE_PAYLOAD_TREE_SHA256: Final = (
    "9eb6554a9807d955e8f2902d058d81e6d57881a8409fb361884297f80e153db1"
)
INCOMING_SUBSTRATE_PATH: Final = PurePosixPath(
    "/var/lib/governed-memory-controller/incoming/cpython"
) / SUBSTRATE_ARCHIVE_SHA256 / SUBSTRATE_ARCHIVE_NAME
WHEELHOUSE_TREE_SHA256: Final = (
    "d802e5000dab609a08d08438b37aace108cd07872246f48fcfe322cd9f028fb4"
)
INCOMING_WHEELHOUSE_ROOT: Final = PurePosixPath(
    "/var/lib/governed-memory-controller/incoming/wheelhouses"
) / WHEELHOUSE_TREE_SHA256
WHEEL_MEMBERS: Final = MappingProxyType(
    {
        "cffi-2.1.0-cp312-cp312-manylinux2014_x86_64."
        "manylinux_2_17_x86_64.whl": (
            221844,
            "1e9f50d192a3e525b15a75ab5114e442d83d657b7ec29182a991bc9a88fd3a66",
        ),
        "cryptography-49.0.0-cp311-abi3-manylinux_2_34_x86_64.whl": (
            4749290,
            "cbc77da8c523d5abd028635ba850a6966fcee2c82e2bf65a41d1d8afe0f98be9",
        ),
        "psycopg-3.3.4-py3-none-any.whl": (
            213001,
            "b6bbc25ccf05c8fad3b061d9db2ef0909a555171b84b07f29458a447253d679a",
        ),
        "psycopg_binary-3.3.4-cp312-cp312-manylinux2014_x86_64."
        "manylinux_2_17_x86_64.whl": (
            5152995,
            "e7510c37550f91a187e3660a8cc50d4b760f8c3b8b2f89ebc5698cd2c7f2c85d",
        ),
        "pycparser-3.0-py3-none-any.whl": (
            48172,
            "b727414169a36b7d524c1c3e31839a521725078d7b2ff038656844266160a992",
        ),
        "typing_extensions-4.15.0-py3-none-any.whl": (
            44614,
            "f0fa19c6845758ab08074a0cfa8b7aecb71c999ca73d62883bc25cc018c4e548",
        ),
    }
)
RUNTIME_CONTRACT_RELATIVE: Final = (
    "ops/governed_memory/installation/current/controller_runtime_contract.json"
)
REQUIREMENTS_LOCK_RELATIVE: Final = (
    "ops/governed_memory/controller-requirements.lock"
)
_HASH_RE: Final = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_GIT_RE: Final = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_MAX_GIT_OUTPUT: Final = 1024 * 1024


class Phase9RuntimePublicationError(RuntimeError):
    """Content-free refusal from the exact runtime publication controller."""


@dataclass(frozen=True, slots=True)
class CandidateIdentity:
    commit: str
    tree: str
    controller_blob: str


@dataclass(frozen=True, slots=True)
class PackageSnapshot:
    verification: Mapping[str, object]
    manifest: bytes
    artifacts: Mapping[str, bytes]


@dataclass(frozen=True, slots=True)
class RuntimeModules:
    package: ModuleType
    builder: ModuleType
    stager: ModuleType
    publication: ModuleType
    primitives: ModuleType
    inspector: ModuleType


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
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_document_invalid"
        ) from error


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise Phase9RuntimePublicationError(
                "phase9_runtime_publication_document_invalid"
            )
        result[key] = value
    return result


def _document(raw: bytes) -> dict[str, object]:
    if type(raw) is not bytes or not raw:
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_document_invalid"
        )
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique,
            parse_constant=lambda unused: (_ for _ in ()).throw(
                Phase9RuntimePublicationError(
                    "phase9_runtime_publication_document_invalid"
                )
            ),
        )
    except Phase9RuntimePublicationError:
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_document_invalid"
        ) from error
    if type(value) is not dict:
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_document_invalid"
        )
    return value


def _git_environment() -> dict[str, str]:
    return {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }


def _git(*arguments: str, maximum: int = _MAX_GIT_OUTPUT) -> bytes:
    if not Path(GIT_BINARY).is_file():
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_git_unavailable"
        )
    command = (
        GIT_BINARY,
        "-c",
        "safe.directory=" + str(ROOT),
        "-C",
        str(ROOT),
        *arguments,
    )
    try:
        completed = subprocess.run(
            command,
            cwd="/",
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_git_failed"
        ) from error
    if (
        completed.returncode != 0
        or len(completed.stdout) > maximum
        or len(completed.stderr) > 64 * 1024
    ):
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_git_failed"
        )
    return bytes(completed.stdout)


def _one_line(raw: bytes, pattern: re.Pattern[str], code: str) -> str:
    try:
        value = raw.decode("ascii").rstrip("\n")
    except UnicodeError as error:
        raise Phase9RuntimePublicationError(code) from error
    if raw != (value + "\n").encode("ascii") or pattern.fullmatch(value) is None:
        raise Phase9RuntimePublicationError(code)
    return value


def _verified_candidate_identity() -> CandidateIdentity:
    try:
        source = Path(__file__)
        if not source.is_absolute():
            source = source.absolute()
        if source.resolve(strict=True) != source or ROOT.resolve(strict=True) != ROOT:
            raise Phase9RuntimePublicationError(
                "phase9_runtime_publication_candidate_path_invalid"
            )
        top = _git("rev-parse", "--show-toplevel").decode("utf-8").rstrip("\n")
    except (OSError, UnicodeError) as error:
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_candidate_path_invalid"
        ) from error
    if top != str(ROOT):
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_candidate_path_invalid"
        )
    if _git("status", "--porcelain=v1", "--untracked-files=all") != b"":
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_candidate_not_clean"
        )
    _git("ls-files", "--error-unmatch", CONTROLLER_RELATIVE)
    commit = _one_line(
        _git("rev-parse", "--verify", "HEAD^{commit}"),
        _GIT_RE,
        "phase9_runtime_publication_candidate_identity_invalid",
    )
    tree = _one_line(
        _git("rev-parse", "--verify", "HEAD^{tree}"),
        _GIT_RE,
        "phase9_runtime_publication_candidate_identity_invalid",
    )
    committed_blob = _one_line(
        _git("rev-parse", "HEAD:" + CONTROLLER_RELATIVE),
        _GIT_RE,
        "phase9_runtime_publication_candidate_identity_invalid",
    )
    observed_blob = _one_line(
        _git("hash-object", str(ROOT / CONTROLLER_RELATIVE)),
        _GIT_RE,
        "phase9_runtime_publication_candidate_identity_invalid",
    )
    if committed_blob != observed_blob:
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_candidate_not_clean"
        )
    return CandidateIdentity(commit, tree, committed_blob)


def _closed_python_invocation() -> bool:
    return (
        sys.flags.isolated == 1
        and _DONT_WRITE_BYTECODE_AT_START
        and sys.version_info[:2] == (3, 12)
    )


def _reverify_candidate(expected: CandidateIdentity) -> None:
    if _verified_candidate_identity() != expected:
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_candidate_changed"
        )


def _load_modules() -> RuntimeModules:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        return RuntimeModules(
            package=importlib.import_module(
                "tools.governed_memory_install.package"
            ),
            builder=importlib.import_module(
                "tools.governed_memory_release.controller_runtime_builder"
            ),
            stager=importlib.import_module(
                "tools.governed_memory_release.runtime_input_stager"
            ),
            publication=importlib.import_module(
                "tools.governed_memory_release.runtime_publication_transport"
            ),
            primitives=importlib.import_module(
                "tools.governed_memory_release.linux_runtime_publication_primitives"
            ),
            inspector=importlib.import_module(
                "tools.governed_memory_release.inspect_standalone_cpython"
            ),
        )
    except Exception as error:
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_module_load_failed"
        ) from error


def _load_package_snapshot(package: ModuleType) -> PackageSnapshot:
    try:
        verification = package.verify()
        manifest = package._read_repository_file(package.MANIFEST_RELATIVE)
        document = _document(manifest)
        index = document.get("artifacts")
        if type(index) is not dict or not index:
            raise Phase9RuntimePublicationError(
                "phase9_runtime_publication_package_invalid"
            )
        artifacts: dict[str, bytes] = {}
        for relative in sorted(index):
            expected = index[relative]
            if (
                type(relative) is not str
                or type(expected) is not str
                or _HASH_RE.fullmatch(expected) is None
            ):
                raise Phase9RuntimePublicationError(
                    "phase9_runtime_publication_package_invalid"
                )
            raw = package._read_repository_file(relative)
            if _sha(raw) != expected:
                raise Phase9RuntimePublicationError(
                    "phase9_runtime_publication_package_changed"
                )
            artifacts[relative] = raw
        if (
            verification.get("package_manifest_sha256") != _sha(manifest)
            or verification.get("artifact_count") != len(artifacts)
        ):
            raise Phase9RuntimePublicationError(
                "phase9_runtime_publication_package_changed"
            )
        return PackageSnapshot(
            MappingProxyType(dict(verification)),
            manifest,
            MappingProxyType(artifacts),
        )
    except Phase9RuntimePublicationError:
        raise
    except Exception as error:
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_package_invalid"
        ) from error


def _load_exact_wheelhouse(primitives: object) -> dict[str, bytes]:
    artifacts: dict[str, bytes] = {}
    for filename, (size, expected_sha256) in sorted(WHEEL_MEMBERS.items()):
        path = str(INCOMING_WHEELHOUSE_ROOT / filename)
        try:
            raw = primitives.read_regular_no_follow(path, size)
            observed = primitives.observe_no_follow(path)
        except Exception as error:
            raise Phase9RuntimePublicationError(
                "phase9_runtime_publication_wheelhouse_invalid"
            ) from error
        if (
            len(raw) != size
            or _sha(raw) != expected_sha256
            or observed.kind != "file"
            or observed.mode != 0o444
            or observed.uid != 0
            or observed.gid != 0
            or observed.nlink != 1
            or observed.size != size
            or observed.sha256 != expected_sha256
        ):
            raise Phase9RuntimePublicationError(
                "phase9_runtime_publication_wheelhouse_invalid"
            )
        artifacts[filename] = raw
    try:
        observed_root = primitives.observe_no_follow(str(INCOMING_WHEELHOUSE_ROOT))
        observed_members = primitives.observe_tree_no_follow(
            str(INCOMING_WHEELHOUSE_ROOT)
        )
    except Exception as error:
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_wheelhouse_invalid"
        ) from error
    if (
        observed_root.kind != "directory"
        or observed_root.mode != 0o555
        or observed_root.uid != 0
        or observed_root.gid != 0
        or len(observed_members) != len(WHEEL_MEMBERS)
        or {member.path for member in observed_members} != set(WHEEL_MEMBERS)
    ):
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_wheelhouse_invalid"
        )
    return artifacts


def _selected_substrate_specification() -> bytes:
    raw = _canonical(
        {
            "schema_version": (
                "governed-memory-controller-standalone-cpython-substrate-v2"
            ),
            "state": "externally-approved-exact-offline-substrate",
            "implementation": "CPython",
            "python_version": "3.12.13",
            "platform_os": "linux",
            "platform_architecture": "x86_64",
            "distribution_kind": "standalone-cpython-install-only",
            "archive_name": SUBSTRATE_ARCHIVE_NAME,
            "archive_sha256": SUBSTRATE_ARCHIVE_SHA256,
            "payload_root": "python",
            "interpreter_relative_path": "bin/python",
            "archive_member_policy": (
                "directories-regular-files-and-relative-in-payload-"
                "symlinks-only"
            ),
            "archive_hardlinks_or_special_files_present": False,
            "archive_symlink_count": 1048,
            "archive_symlinks_absolute_escape_dangling_or_cyclic": False,
            "symlink_expansion_policy": (
                "relative-in-payload-links-expanded-to-independent-regular-"
                "file-or-directory-copies"
            ),
            "symlink_expansion_mapping_sha256": (
                SUBSTRATE_SYMLINK_MAPPING_SHA256
            ),
            "expanded_payload_tree_schema": (
                "governed-memory-standalone-cpython-expanded-payload-tree-v1"
            ),
            "expanded_payload_tree_sha256": SUBSTRATE_PAYLOAD_TREE_SHA256,
            "expanded_payload_is_symlink_hardlink_special_free": True,
            "runtime_is_not_venv": True,
            "network_calls": 0,
        }
    )
    if _sha(raw) != SUBSTRATE_SPECIFICATION_SHA256:
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_substrate_contract_invalid"
        )
    return raw


def _build_nonce_material(
    *,
    identity: CandidateIdentity,
    package_manifest_sha256: str,
    runtime_contract_sha256: str,
    requirements_lock_sha256: str,
) -> dict[str, object]:
    return {
        "build_nonce_schema_version": (
            "governed-memory-phase9j-runtime-publication-build-nonce-v1"
        ),
        "candidate_git_commit": identity.commit,
        "candidate_git_tree": identity.tree,
        "controller_source_blob": identity.controller_blob,
        "package_manifest_sha256": package_manifest_sha256,
        "controller_runtime_contract_sha256": runtime_contract_sha256,
        "controller_requirements_lock_sha256": requirements_lock_sha256,
        "standalone_cpython_specification_sha256": (
            SUBSTRATE_SPECIFICATION_SHA256
        ),
        "standalone_cpython_archive_sha256": SUBSTRATE_ARCHIVE_SHA256,
        "wheelhouse_tree_sha256": WHEELHOUSE_TREE_SHA256,
    }


def _build_intent(
    *,
    identity: CandidateIdentity,
    package_manifest_sha256: str,
    runtime_contract_sha256: str,
    requirements_lock_sha256: str,
    build_plan_sha256: str,
) -> tuple[str, bytes]:
    nonce_material = _canonical(
        _build_nonce_material(
            identity=identity,
            package_manifest_sha256=package_manifest_sha256,
            runtime_contract_sha256=runtime_contract_sha256,
            requirements_lock_sha256=requirements_lock_sha256,
        )
    )
    build_nonce = _sha(BUILD_NONCE_DOMAIN + nonce_material)
    intent = {
        "schema_version": BUILD_INTENT_SCHEMA,
        "state": "build_nonce_committed_before_runtime_input_publication",
        **_build_nonce_material(
            identity=identity,
            package_manifest_sha256=package_manifest_sha256,
            runtime_contract_sha256=runtime_contract_sha256,
            requirements_lock_sha256=requirements_lock_sha256,
        ),
        "build_nonce": build_nonce,
        "build_plan_sha256": build_plan_sha256,
        "network_calls": 0,
        "docker_calls": 0,
        "provider_calls": 0,
        "production_data_read": False,
        "store_resources_created": False,
        "activation_performed": False,
    }
    return build_nonce, _canonical(intent)


def _intent_names(package_manifest_sha256: str) -> tuple[str, str]:
    if _HASH_RE.fullmatch(package_manifest_sha256) is None:
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_package_invalid"
        )
    final = package_manifest_sha256 + ".phase9j-runtime-publication-intent.json"
    return final, "." + final + ".staging"


def _read_intent_at(parent_fd: int, name: str, maximum: int) -> bytes | None:
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_build_intent_invalid"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o400
            or metadata.st_uid != ROOT_UID
            or metadata.st_gid != ROOT_GID
            or metadata.st_nlink != 1
            or metadata.st_size < 0
            or metadata.st_size > maximum
            or (metadata.st_dev, metadata.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise Phase9RuntimePublicationError(
                "phase9_runtime_publication_build_intent_invalid"
            )
        raw = bytearray()
        while len(raw) <= maximum:
            block = os.read(descriptor, min(65536, maximum + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
        after = os.fstat(descriptor)
        stable_before = (
            metadata.st_dev,
            metadata.st_ino,
            stat.S_IFMT(metadata.st_mode),
            stat.S_IMODE(metadata.st_mode),
            metadata.st_uid,
            metadata.st_gid,
            metadata.st_nlink,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )
        stable_after = (
            after.st_dev,
            after.st_ino,
            stat.S_IFMT(after.st_mode),
            stat.S_IMODE(after.st_mode),
            after.st_uid,
            after.st_gid,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if len(raw) != metadata.st_size or stable_after != stable_before:
            raise Phase9RuntimePublicationError(
                "phase9_runtime_publication_build_intent_changed"
            )
        return bytes(raw)
    finally:
        os.close(descriptor)


@contextmanager
def _publication_lock():
    descriptor = -1
    try:
        flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(str(BUILD_INTENT_ROOT), flags)
        metadata = os.fstat(descriptor)
        named = Path(str(BUILD_INTENT_ROOT)).stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_uid != ROOT_UID
            or metadata.st_gid != ROOT_GID
            or (metadata.st_dev, metadata.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise Phase9RuntimePublicationError(
                "phase9_runtime_publication_build_root_invalid"
            )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise Phase9RuntimePublicationError(
                "phase9_runtime_publication_already_running"
            ) from error
        yield descriptor
    except Phase9RuntimePublicationError:
        raise
    except OSError as error:
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_build_root_invalid"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _ensure_build_intent_root() -> None:
    """Create only the fixed durable-intent parent before writing the intent."""

    expected = PurePosixPath("/var/lib/governed-memory-controller")
    if BUILD_INTENT_ROOT != expected:
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_build_root_invalid"
        )
    parent_fd = -1
    child_fd = -1
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        parent_fd = os.open("/var/lib", flags)
        parent = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != ROOT_UID
            or parent.st_gid != ROOT_GID
            or parent.st_mode & 0o022
        ):
            raise Phase9RuntimePublicationError(
                "phase9_runtime_publication_build_root_invalid"
            )
        try:
            os.mkdir(expected.name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        child_fd = os.open(expected.name, flags, dir_fd=parent_fd)
        child = os.fstat(child_fd)
        if (
            not stat.S_ISDIR(child.st_mode)
            or child.st_uid != ROOT_UID
            or child.st_gid != ROOT_GID
            or stat.S_IMODE(child.st_mode) not in {0o700, 0o755}
        ):
            raise Phase9RuntimePublicationError(
                "phase9_runtime_publication_build_root_invalid"
            )
        if stat.S_IMODE(child.st_mode) != 0o700:
            os.fchmod(child_fd, 0o700)
        os.fsync(child_fd)
        os.fsync(parent_fd)
    except Phase9RuntimePublicationError:
        raise
    except OSError as error:
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_build_root_invalid"
        ) from error
    finally:
        if child_fd >= 0:
            os.close(child_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _rename_intent_no_replace(
    parent_fd: int, source_name: str, destination_name: str
) -> None:
    if (
        type(parent_fd) is not int
        or type(source_name) is not str
        or type(destination_name) is not str
        or PurePosixPath(source_name).name != source_name
        or PurePosixPath(destination_name).name != destination_name
        or not source_name.startswith(".")
        or destination_name.startswith(".")
        or source_name != "." + destination_name + ".staging"
    ):
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_build_intent_publish_failed"
        )
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise OSError(errno.ENOSYS, "renameat2")
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            parent_fd,
            os.fsencode(source_name),
            parent_fd,
            os.fsencode(destination_name),
            1,
        )
        if result != 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number))
    except OSError as error:
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_build_intent_publish_failed"
        ) from error


def _publish_build_intent(
    *,
    parent_fd: int,
    package_manifest_sha256: str,
    expected: bytes,
) -> tuple[str, str, bool]:
    final_name, staging_name = _intent_names(package_manifest_sha256)
    final_path = str(BUILD_INTENT_ROOT / final_name)
    final = _read_intent_at(parent_fd, final_name, len(expected))
    partial = _read_intent_at(parent_fd, staging_name, len(expected))
    if final is not None:
        if final != expected or partial is not None:
            raise Phase9RuntimePublicationError(
                "phase9_runtime_publication_build_intent_mismatch"
            )
        return final_path, _sha(expected), True
    if partial is not None:
        if not expected.startswith(partial):
            raise Phase9RuntimePublicationError(
                "phase9_runtime_publication_build_intent_mismatch"
            )
        try:
            os.unlink(staging_name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except OSError as error:
            raise Phase9RuntimePublicationError(
                "phase9_runtime_publication_build_intent_recovery_failed"
            ) from error
    descriptor = -1
    try:
        descriptor = os.open(
            staging_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            0o400,
            dir_fd=parent_fd,
        )
        os.fchown(descriptor, ROOT_UID, ROOT_GID)
        os.fchmod(descriptor, 0o400)
        view = memoryview(expected)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError
            view = view[written:]
        os.fsync(descriptor)
    except OSError as error:
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_build_intent_write_failed"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if _read_intent_at(parent_fd, staging_name, len(expected)) != expected:
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_build_intent_write_failed"
        )
    try:
        _rename_intent_no_replace(parent_fd, staging_name, final_name)
        os.fsync(parent_fd)
    except Exception as error:
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_build_intent_publish_failed"
        ) from error
    if _read_intent_at(parent_fd, final_name, len(expected)) != expected:
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_build_intent_publish_failed"
        )
    return final_path, _sha(expected), False


def _receipt(
    *,
    identity: CandidateIdentity,
    snapshot: PackageSnapshot,
    plan: object,
    result: object,
    bootstrap: object,
    cpython_stage: object,
    wheelhouse_stage: object,
    intent_path: str,
    intent_sha256: str,
    intent_replayed: bool,
) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": RECEIPT_SCHEMA,
        "result": "exact_controller_runtime_release_and_receipt_published",
        "candidate_git_commit": identity.commit,
        "candidate_git_tree": identity.tree,
        "controller_source_blob": identity.controller_blob,
        "package_manifest_sha256": snapshot.verification[
            "package_manifest_sha256"
        ],
        "package_artifact_count": snapshot.verification["artifact_count"],
        "build_plan_sha256": plan.build_plan_sha256,
        "build_intent_path": intent_path,
        "build_intent_sha256": intent_sha256,
        "build_intent_replayed": intent_replayed,
        "standalone_cpython_specification_sha256": (
            plan.substrate.specification_sha256
        ),
        "standalone_cpython_archive_sha256": plan.substrate.archive_sha256,
        "standalone_cpython_payload_tree_sha256": (
            plan.substrate.payload_tree_sha256
        ),
        "wheelhouse_tree_sha256": plan.wheelhouse.tree_sha256,
        "controller_runtime_receipt_sha256": (
            result.controller_runtime_receipt_sha256
        ),
        "runtime_root": result.runtime_root,
        "runtime_tree_sha256": result.runtime_tree_sha256,
        "release_root": result.release_root,
        "release_tree_sha256": result.release_tree_sha256,
        "runtime_receipt_path": result.receipt_path,
        "runtime_build_receipt_verified_by_builder": True,
        "authority_bound_runtime_capability_verified": False,
        "authority_bound_runtime_capability_deferred_to_exact_proof_runner": True,
        "controller_runtime_parent_roots_bootstrapped": True,
        "controller_runtime_parent_roots_created_count": len(
            bootstrap.created_roots
        ),
        "controller_runtime_parent_roots_normalized_count": len(
            bootstrap.normalized_roots
        ),
        "standalone_cpython_input_replayed": cpython_stage.replayed,
        "wheelhouse_input_replayed": wheelhouse_stage.replayed,
        "network_calls": 0,
        "docker_calls": 0,
        "provider_calls": 0,
        "secrets_touched": False,
        "production_data_read": False,
        "persistent_store_resources_created": False,
        "activation_performed": False,
    }
    value["receipt_sha256"] = _sha(_canonical(value))
    return value


def publish_phase9_controller_runtime() -> Mapping[str, object]:
    """Publish or exactly replay the one selected Phase 9J runtime build."""

    if (
        sys.platform != "linux"
        or os.geteuid() != 0
        or os.getegid() != 0
        or not _closed_python_invocation()
    ):
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_root_linux_required"
        )
    identity = _verified_candidate_identity()
    modules = _load_modules()
    snapshot = _load_package_snapshot(modules.package)
    _reverify_candidate(identity)
    try:
        primitives = modules.primitives.LinuxRuntimePublicationPrimitives()
        substrate_json = _selected_substrate_specification()
        wheelhouse = _load_exact_wheelhouse(primitives)
        runtime_contract = snapshot.artifacts[RUNTIME_CONTRACT_RELATIVE]
        requirements_lock = snapshot.artifacts[REQUIREMENTS_LOCK_RELATIVE]
        nonce_material = _build_nonce_material(
            identity=identity,
            package_manifest_sha256=str(
                snapshot.verification["package_manifest_sha256"]
            ),
            runtime_contract_sha256=_sha(runtime_contract),
            requirements_lock_sha256=_sha(requirements_lock),
        )
        build_nonce = _sha(BUILD_NONCE_DOMAIN + _canonical(nonce_material))
        plan = modules.builder.create_controller_runtime_build_plan(
            build_nonce=build_nonce,
            standalone_cpython_substrate_json=substrate_json,
            wheelhouse_artifacts=wheelhouse,
            controller_runtime_contract_json=runtime_contract,
            controller_requirements_lock=requirements_lock,
            package_manifest_json=snapshot.manifest,
            package_artifacts=dict(snapshot.artifacts),
        )
        derived_nonce, intent = _build_intent(
            identity=identity,
            package_manifest_sha256=str(
                snapshot.verification["package_manifest_sha256"]
            ),
            runtime_contract_sha256=_sha(runtime_contract),
            requirements_lock_sha256=_sha(requirements_lock),
            build_plan_sha256=plan.build_plan_sha256,
        )
        if derived_nonce != build_nonce:
            raise Phase9RuntimePublicationError(
                "phase9_runtime_publication_build_binding_invalid"
            )
        _reverify_candidate(identity)
        _ensure_build_intent_root()
        with _publication_lock() as parent_fd:
            intent_path, intent_sha256, intent_replayed = _publish_build_intent(
                parent_fd=parent_fd,
                package_manifest_sha256=str(
                    snapshot.verification["package_manifest_sha256"]
                ),
                expected=intent,
            )
            inspected_substrate = (
                modules.inspector.inspect_selected_standalone_archive(
                    str(INCOMING_SUBSTRATE_PATH)
                )
            )
            if inspected_substrate != substrate_json:
                raise Phase9RuntimePublicationError(
                    "phase9_runtime_publication_substrate_invalid"
                )
            bootstrap = primitives.bootstrap_fixed_root_directories()
            stager = modules.stager.ClosedRuntimeInputStager(primitives)
            cpython_stage = stager.stage_selected_cpython_archive(plan)
            wheelhouse_stage = stager.stage_exact_wheelhouse(plan)
            result = modules.builder.execute_controller_runtime_build(
                plan,
                standalone_cpython_substrate_json=substrate_json,
                package_manifest_json=snapshot.manifest,
                package_artifacts=dict(snapshot.artifacts),
                transport=modules.publication.ClosedRuntimePublicationTransport(
                    primitives
                ),
            )
        _reverify_candidate(identity)
        return MappingProxyType(
            _receipt(
                identity=identity,
                snapshot=snapshot,
                plan=plan,
                result=result,
                bootstrap=bootstrap,
                cpython_stage=cpython_stage,
                wheelhouse_stage=wheelhouse_stage,
                intent_path=intent_path,
                intent_sha256=intent_sha256,
                intent_replayed=intent_replayed,
            )
        )
    except Phase9RuntimePublicationError:
        raise
    except Exception as error:
        raise Phase9RuntimePublicationError(
            "phase9_runtime_publication_refused"
        ) from error


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="publish_phase9_controller_runtime.py",
        allow_abbrev=False,
        description=(
            "Publish the exact checked-in Phase 9J controller runtime; "
            "no operational arguments are accepted."
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    _parser().parse_args(tuple(sys.argv[1:] if argv is None else argv))
    try:
        receipt = publish_phase9_controller_runtime()
    except Phase9RuntimePublicationError as error:
        sys.stderr.write(str(error) + "\n")
        return 1
    sys.stdout.buffer.write(_canonical(dict(receipt)) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "Phase9RuntimePublicationError",
    "publish_phase9_controller_runtime",
    "main",
]
