from __future__ import annotations

"""Secure verifier for the isolated dormant-store controller runtime.

The verifier derives every path from canonical receipt bytes and the closed
package capability.  It walks runtime and release paths without following
links, hashes the installed runtime and complete package release trees from
open file descriptors, closes launcher-local imports, requires the installed
distribution inventory to equal the hash-locked requirements, and performs
fixed isolated interpreter probes.  Importing this module has no effects; no
runtime is built or installed here.
"""

import ast
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import stat
import subprocess
from typing import Final, Mapping

from .authority import canonical_json_bytes
from .package_capability import PackageCapabilityError, _package_capability_parts


RUNTIME_RECEIPT_SCHEMA: Final = (
    "governed-memory-controller-runtime-build-receipt-v4"
)
RUNTIME_RECEIPT_RESULT: Final = "isolated_controller_runtime_built_and_closed"
RUNTIME_ROOT_PREFIX: PurePosixPath = PurePosixPath(
    "/opt/governed-memory-controller/runtimes"
)
RELEASE_ROOT_PREFIX: PurePosixPath = PurePosixPath(
    "/opt/governed-memory-controller/releases"
)
INTERPRETER_RELATIVE_PATH: Final = PurePosixPath("bin/python")
INVENTORY_RELATIVE_PATH: Final = PurePosixPath("controller-distributions.json")
LAUNCHER_RELATIVE_PATH: Final = PurePosixPath(
    "tools/governed_memory_install/store_supervisor_launcher.py"
)
PACKAGE_MANIFEST_RELATIVE_PATH: Final = PurePosixPath(
    "ops/governed_memory/installation/current/package_manifest.json"
)
REQUIREMENTS_LOCK_RELATIVE_PATH: Final = PurePosixPath(
    "ops/governed_memory/controller-requirements.lock"
)
CONTROLLER_RUNTIME_CONTRACT_RELATIVE_PATH: Final = PurePosixPath(
    "ops/governed_memory/installation/current/controller_runtime_contract.json"
)
MAX_RECEIPT_BYTES: Final = 128 * 1024
MAX_PROBE_BYTES: Final = 1024 * 1024
_EXPECTED_UID: int = 0
EXPECTED_POSTGRESQL_DRIVER_IDENTITY_SHA256: Final = (
    "364760713fd35d8d7029c972e9cc23ec69f3b5c4a9a1ce6bab302a525f0ef8fa"
)

_HASH_RE: Final = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_VERSION_RE: Final = re.compile(r"3\.12\.[0-9]+\Z", re.ASCII)
_WHEEL_FILENAME_RE: Final = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._+-]{0,239}\.whl\Z", re.ASCII
)
_RECEIPT_KEYS: Final = frozenset(
    {
        "schema_version",
        "result",
        "package_manifest_sha256",
        "controller_runtime_contract_sha256",
        "controller_requirements_lock_sha256",
        "build_plan_sha256",
        "standalone_cpython_specification_sha256",
        "standalone_cpython_archive_sha256",
        "standalone_cpython_payload_tree_sha256",
        "wheelhouse_tree_sha256",
        "python_implementation",
        "python_version",
        "platform_os",
        "platform_architecture",
        "runtime_tree_sha256",
        "release_tree_sha256",
        "interpreter_sha256",
        "installed_distribution_inventory_sha256",
        "interpreter_path_facts_sha256",
        "postgresql_driver_identity_sha256",
        "supervisor_launcher_sha256",
        "launcher_help_probe_sha256",
        "pip_present",
        "setuptools_present",
        "wheel_present",
        "user_site_enabled",
        "system_site_packages_enabled",
        "network_calls",
        "provider_calls",
        "production_data_read",
        "active_production_state_changed",
        "persistent_controller_substrate_created",
        "persistent_store_resources_created",
    }
)


class ControllerRuntimeCapabilityError(RuntimeError):
    """Content-free refusal for incomplete or changed runtime evidence."""


@dataclass(frozen=True, slots=True)
class VerifiedControllerRuntimeEvidence:
    result_type: str
    controller_runtime_receipt_sha256: str
    build_plan_sha256: str
    package_manifest_sha256: str
    controller_runtime_contract_sha256: str
    controller_requirements_lock_sha256: str
    standalone_cpython_specification_sha256: str
    standalone_cpython_archive_sha256: str
    standalone_cpython_payload_tree_sha256: str
    wheelhouse_tree_sha256: str
    runtime_root: str
    runtime_tree_sha256: str
    release_root: str
    release_tree_sha256: str
    package_manifest_path: str
    interpreter_path: str
    interpreter_sha256: str
    inventory_path: str
    installed_distribution_inventory_sha256: str
    interpreter_path_facts_sha256: str
    postgresql_driver_identity_sha256: str
    supervisor_launcher_path: str
    supervisor_launcher_sha256: str
    launcher_help_probe_sha256: str
    python_implementation: str
    python_version: str
    platform_os: str
    platform_architecture: str
    persistent_controller_substrate_created: bool
    persistent_store_resources_created: bool


@dataclass(frozen=True, slots=True)
class _ObservedRuntime:
    runtime_tree_sha256: str
    release_tree_sha256: str
    interpreter_sha256: str
    inventory_sha256: str
    inventory_bytes: bytes
    launcher_sha256: str


@dataclass(frozen=True, slots=True)
class _ProcessProbeResult:
    inventory_bytes: bytes
    launcher_help_probe_sha256: str


@dataclass(frozen=True, slots=True)
class _SelectedRuntimeInputs:
    python_version: str
    archive_sha256: str
    specification_sha256: str
    payload_tree_sha256: str
    wheelhouse_tree_sha256: str


_RUNTIME_TOKEN = object()


class _VerifiedControllerRuntimeCapability:
    __slots__ = ("_evidence", "_token")

    def __init__(
        self, evidence: VerifiedControllerRuntimeEvidence, token: object
    ) -> None:
        if token is not _RUNTIME_TOKEN:
            raise ControllerRuntimeCapabilityError(
                "controller_runtime_capability_invalid"
            )
        self._evidence = evidence
        self._token = token

    def __repr__(self) -> str:
        return "VerifiedControllerRuntimeCapability(<content-redacted>)"


def _controller_runtime_capability_evidence(
    value: object,
) -> VerifiedControllerRuntimeEvidence:
    if (
        type(value) is not _VerifiedControllerRuntimeCapability
        or value._token is not _RUNTIME_TOKEN
        or type(value._evidence) is not VerifiedControllerRuntimeEvidence
    ):
        raise ControllerRuntimeCapabilityError(
            "controller_runtime_capability_invalid"
        )
    return value._evidence


def verified_controller_runtime_evidence(
    value: object,
) -> VerifiedControllerRuntimeEvidence:
    return _controller_runtime_capability_evidence(value)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ControllerRuntimeCapabilityError(
                "controller_runtime_receipt_invalid"
            )
        result[key] = value
    return result


def _receipt_document(raw: bytes | str) -> tuple[bytes, dict[str, object]]:
    if isinstance(raw, bytes):
        receipt_raw = bytes(raw)
    elif isinstance(raw, str):
        try:
            receipt_raw = raw.encode("ascii")
        except UnicodeError as error:
            raise ControllerRuntimeCapabilityError(
                "controller_runtime_receipt_invalid"
            ) from error
    else:
        raise ControllerRuntimeCapabilityError(
            "controller_runtime_receipt_invalid"
        )
    if not receipt_raw or len(receipt_raw) > MAX_RECEIPT_BYTES:
        raise ControllerRuntimeCapabilityError(
            "controller_runtime_receipt_invalid"
        )
    try:
        receipt = json.loads(
            receipt_raw.decode("ascii"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda unused: (_ for _ in ()).throw(
                ControllerRuntimeCapabilityError(
                    "controller_runtime_receipt_invalid"
                )
            ),
        )
    except ControllerRuntimeCapabilityError:
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ControllerRuntimeCapabilityError(
            "controller_runtime_receipt_invalid"
        ) from error
    if (
        type(receipt) is not dict
        or set(receipt) != _RECEIPT_KEYS
        or canonical_json_bytes(receipt) != receipt_raw
    ):
        raise ControllerRuntimeCapabilityError(
            "controller_runtime_receipt_invalid"
        )
    return receipt_raw, receipt


def _is_hash(value: object) -> bool:
    return type(value) is str and _HASH_RE.fullmatch(value) is not None


def _require_closed_receipt(receipt: Mapping[str, object]) -> None:
    for key in (
        "package_manifest_sha256",
        "controller_runtime_contract_sha256",
        "controller_requirements_lock_sha256",
        "build_plan_sha256",
        "standalone_cpython_specification_sha256",
        "standalone_cpython_archive_sha256",
        "standalone_cpython_payload_tree_sha256",
        "wheelhouse_tree_sha256",
        "runtime_tree_sha256",
        "release_tree_sha256",
        "interpreter_sha256",
        "installed_distribution_inventory_sha256",
        "interpreter_path_facts_sha256",
        "postgresql_driver_identity_sha256",
        "supervisor_launcher_sha256",
        "launcher_help_probe_sha256",
    ):
        if not _is_hash(receipt.get(key)):
            raise ControllerRuntimeCapabilityError(
                "controller_runtime_receipt_invalid"
            )
    if (
        receipt.get("schema_version") != RUNTIME_RECEIPT_SCHEMA
        or receipt.get("result") != RUNTIME_RECEIPT_RESULT
        or receipt.get("python_implementation") != "CPython"
        or type(receipt.get("python_version")) is not str
        or _VERSION_RE.fullmatch(str(receipt["python_version"])) is None
        or receipt.get("platform_os") != "linux"
        or receipt.get("platform_architecture") != "x86_64"
        or receipt.get("postgresql_driver_identity_sha256")
        != EXPECTED_POSTGRESQL_DRIVER_IDENTITY_SHA256
        or any(
            receipt.get(key) is not False
            for key in (
                "pip_present",
                "setuptools_present",
                "wheel_present",
                "user_site_enabled",
                "system_site_packages_enabled",
                "production_data_read",
                "active_production_state_changed",
                "persistent_store_resources_created",
            )
        )
        or receipt.get("persistent_controller_substrate_created") is not True
        or type(receipt.get("network_calls")) is not int
        or receipt.get("network_calls") != 0
        or type(receipt.get("provider_calls")) is not int
        or receipt.get("provider_calls") != 0
    ):
        raise ControllerRuntimeCapabilityError(
            "controller_runtime_receipt_invalid"
        )


def _selected_runtime_input_identity(
    raw: bytes,
    *,
    expected_contract_sha256: str,
    requirements_lock: bytes,
) -> _SelectedRuntimeInputs:
    """Return only a fully staged, lock-closed runtime-input selection."""

    if (
        type(raw) is not bytes
        or not raw
        or len(raw) > MAX_RECEIPT_BYTES
        or not _is_hash(expected_contract_sha256)
        or hashlib.sha256(raw).hexdigest() != expected_contract_sha256
    ):
        raise ControllerRuntimeCapabilityError(
            "controller_runtime_input_contract_invalid"
        )
    try:
        document = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda unused: (_ for _ in ()).throw(
                ControllerRuntimeCapabilityError(
                    "controller_runtime_input_contract_invalid"
                )
            ),
        )
        selected = document["selected_runtime_inputs"]
        cpython = selected["standalone_cpython"]
        driver = selected["postgresql_driver"]
        wheelhouse = selected["wheelhouse"]
        build_policy = document["build_policy"]
        dependency_policy = document["dependency_policy"]
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError):
        raise ControllerRuntimeCapabilityError(
            "controller_runtime_input_contract_invalid"
        ) from None
    except ControllerRuntimeCapabilityError:
        raise ControllerRuntimeCapabilityError(
            "controller_runtime_input_contract_invalid"
        ) from None
    if (
        type(document) is not dict
        or document.get("schema_version")
        != "governed-memory-dormant-store-install-controller-runtime-contract-v2"
        or type(selected) is not dict
        or type(cpython) is not dict
        or type(driver) is not dict
        or type(wheelhouse) is not dict
        or type(build_policy) is not dict
        or type(dependency_policy) is not dict
        or cpython.get("implementation") != "CPython"
        or type(cpython.get("python_version")) is not str
        or _VERSION_RE.fullmatch(str(cpython["python_version"])) is None
        or type(cpython.get("archive_name")) is not str
        or not cpython["archive_name"]
        or not _is_hash(cpython.get("archive_sha256"))
        or cpython.get("payload_root") != "python"
        or cpython.get("interpreter_relative_path")
        != str(INTERPRETER_RELATIVE_PATH)
        or cpython.get("archive_staged") is not True
        or cpython.get("archive_bytes_sha256_verified_locally") is not True
        or cpython.get("archive_member_types_verified") is not True
        or not _is_hash(cpython.get("specification_sha256"))
        or not _is_hash(cpython.get("expanded_payload_tree_sha256"))
        or cpython.get("archive_symlink_count") != 1048
        or cpython.get("archive_symlink_normalization_verified") is not True
        or build_policy.get("approved_standalone_cpython_substrate_selected")
        is not True
        or build_policy.get("standalone_cpython_archive_staged") is not True
        or build_policy.get("standalone_cpython_archive_content_verified")
        is not True
        or build_policy.get("offline_wheelhouse_staged") is not True
        or build_policy.get("canonical_wheelhouse_identity_present") is not True
        or build_policy.get("preferred_postgresql_driver_locked") is not True
        or dependency_policy.get(
            "current_lock_contains_preferred_postgresql_driver"
        )
        is not True
        or driver.get("api_style") != "synchronous"
        or driver.get("preferred_extra") != "binary"
        or driver.get("selection_state")
        != "exact-selected-wheels-staged-verified-and-locked"
        or driver.get("asyncpg_is_controller_driver") is not False
        or driver.get("current_controller_lock_contains_selection") is not True
        or driver.get("exact_wheel_filenames_frozen") is not True
        or driver.get("wheel_bytes_staged") is not True
        or driver.get("wheel_bytes_sha256_verified_locally") is not True
        or driver.get("binary_native_library_closure_inspected") is not True
        or driver.get("driver_native_postgresql_stages_packaged") is not True
        or driver.get("selection_ready_for_runtime_build") is not True
        or wheelhouse.get("wheelhouse_staged") is not True
        or not _is_hash(wheelhouse.get("canonical_tree_sha256"))
        or type(wheelhouse.get("canonical_member_count")) is not int
        or type(wheelhouse.get("canonical_total_bytes")) is not int
        or wheelhouse["canonical_member_count"] <= 0
        or wheelhouse["canonical_total_bytes"] <= 0
    ):
        raise ControllerRuntimeCapabilityError(
            "controller_runtime_input_contract_not_ready"
        )
    lock_bindings = _requirements_lock_bindings(requirements_lock)
    preferences = driver.get("preferred_distributions")
    selected_wheels = driver.get("selected_wheels")
    if (
        type(preferences) is not list
        or len(preferences) != 2
        or type(selected_wheels) is not list
        or len(selected_wheels) != 2
    ):
        raise ControllerRuntimeCapabilityError(
            "controller_runtime_input_contract_not_ready"
        )
    preferred_versions: dict[str, str] = {}
    for item in preferences:
        if (
            type(item) is not dict
            or set(item) != {"normalized_distribution", "version"}
            or type(item.get("normalized_distribution")) is not str
            or type(item.get("version")) is not str
            or not item["version"]
            or item["normalized_distribution"] in preferred_versions
        ):
            raise ControllerRuntimeCapabilityError(
                "controller_runtime_input_contract_not_ready"
            )
        preferred_versions[str(item["normalized_distribution"])] = str(
            item["version"]
        )
    if preferred_versions != {
        "psycopg": "3.3.4",
        "psycopg-binary": "3.3.4",
    }:
        raise ControllerRuntimeCapabilityError(
            "controller_runtime_input_contract_not_ready"
        )
    selected_driver: set[str] = set()
    for item in selected_wheels:
        if (
            type(item) is not dict
            or set(item)
            != {
                "normalized_distribution",
                "selected_wheel_filename",
                "selected_wheel_sha256",
                "version",
            }
            or type(item.get("normalized_distribution")) is not str
            or type(item.get("version")) is not str
            or preferred_versions.get(str(item["normalized_distribution"]))
            != item["version"]
            or type(item.get("selected_wheel_filename")) is not str
            or _WHEEL_FILENAME_RE.fullmatch(
                str(item["selected_wheel_filename"])
            )
            is None
            or PurePosixPath(str(item["selected_wheel_filename"])).name
            != item["selected_wheel_filename"]
            or not _is_hash(item.get("selected_wheel_sha256"))
        ):
            raise ControllerRuntimeCapabilityError(
                "controller_runtime_input_contract_not_ready"
            )
        name = str(item["normalized_distribution"])
        binding = lock_bindings.get(name)
        if (
            name in selected_driver
            or name not in {"psycopg", "psycopg-binary"}
            or binding is None
            or binding[0] != item["version"]
            or item["selected_wheel_sha256"] not in binding[1]
        ):
            raise ControllerRuntimeCapabilityError(
                "controller_runtime_input_contract_not_ready"
            )
        selected_driver.add(name)
    if (
        selected_driver != {"psycopg", "psycopg-binary"}
        or wheelhouse["canonical_member_count"] != len(lock_bindings)
    ):
        raise ControllerRuntimeCapabilityError(
            "controller_runtime_input_contract_not_ready"
        )
    return _SelectedRuntimeInputs(
        python_version=str(cpython["python_version"]),
        archive_sha256=str(cpython["archive_sha256"]),
        specification_sha256=str(cpython["specification_sha256"]),
        payload_tree_sha256=str(cpython["expanded_payload_tree_sha256"]),
        wheelhouse_tree_sha256=str(wheelhouse["canonical_tree_sha256"]),
    )


def _open_absolute_directory(path: PurePosixPath) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise ControllerRuntimeCapabilityError(
            "controller_runtime_path_invalid"
        )
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    try:
        for component in path.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            info = os.fstat(child)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != _EXPECTED_UID
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                os.close(child)
                raise ControllerRuntimeCapabilityError(
                    "controller_runtime_directory_identity_invalid"
                )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_relative_directory(parent_fd: int, relative: PurePosixPath) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.dup(parent_fd)
    try:
        for component in relative.parts:
            child = os.open(component, flags, dir_fd=descriptor)
            info = os.fstat(child)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != _EXPECTED_UID
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                os.close(child)
                raise ControllerRuntimeCapabilityError(
                    "controller_runtime_directory_identity_invalid"
                )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _read_regular(
    parent_fd: int,
    name: str,
    *,
    executable: bool,
) -> tuple[bytes, str, int]:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        info = os.fstat(descriptor)
        mode = stat.S_IMODE(info.st_mode)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != _EXPECTED_UID
            or info.st_nlink != 1
            or mode & 0o222
            or (executable and mode & 0o111 == 0)
            or (not executable and mode & 0o111 != 0)
        ):
            raise ControllerRuntimeCapabilityError(
                "controller_runtime_file_identity_invalid"
            )
        chunks: list[bytes] = []
        digest = hashlib.sha256()
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
            digest.update(block)
        after = os.fstat(descriptor)
        if (
            after.st_dev != info.st_dev
            or after.st_ino != info.st_ino
            or after.st_mode != info.st_mode
            or after.st_uid != info.st_uid
            or after.st_gid != info.st_gid
            or after.st_nlink != info.st_nlink
            or after.st_size != info.st_size
            or after.st_mtime_ns != info.st_mtime_ns
            or after.st_ctime_ns != info.st_ctime_ns
        ):
            raise ControllerRuntimeCapabilityError(
                "controller_runtime_file_changed_during_read"
            )
        return b"".join(chunks), digest.hexdigest(), mode
    finally:
        os.close(descriptor)


def _safe_artifact_path(value: object) -> str:
    if type(value) is not str:
        raise ControllerRuntimeCapabilityError(
            "controller_release_artifact_index_invalid"
        )
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ControllerRuntimeCapabilityError(
            "controller_release_artifact_index_invalid"
        )
    return value


def _package_artifact_digests(
    package_artifacts: Mapping[str, bytes],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for candidate, raw in package_artifacts.items():
        path = _safe_artifact_path(candidate)
        if type(raw) is not bytes or path == str(PACKAGE_MANIFEST_RELATIVE_PATH):
            raise ControllerRuntimeCapabilityError(
                "controller_release_artifact_index_invalid"
            )
        result[path] = hashlib.sha256(raw).hexdigest()
    if not result:
        raise ControllerRuntimeCapabilityError(
            "controller_release_artifact_index_invalid"
        )
    return result


def _release_manifest_document(
    raw: bytes,
    *,
    expected_manifest_sha256: str,
    expected_artifacts: Mapping[str, str],
) -> None:
    if hashlib.sha256(raw).hexdigest() != expected_manifest_sha256:
        raise ControllerRuntimeCapabilityError(
            "controller_release_manifest_identity_mismatch"
        )
    try:
        manifest = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda unused: (_ for _ in ()).throw(
                ControllerRuntimeCapabilityError(
                    "controller_release_manifest_invalid"
                )
            ),
        )
    except ControllerRuntimeCapabilityError:
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ControllerRuntimeCapabilityError(
            "controller_release_manifest_invalid"
        ) from error
    if (
        type(manifest) is not dict
        or set(manifest) != {"schema_version", "state", "artifacts"}
        or type(manifest.get("schema_version")) is not str
        or not manifest["schema_version"]
        or type(manifest.get("state")) is not str
        or not manifest["state"]
        or manifest.get("artifacts") != dict(expected_artifacts)
    ):
        raise ControllerRuntimeCapabilityError(
            "controller_release_manifest_invalid"
        )


def _directory_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _observe_release_tree(
    release_fd: int,
    *,
    package_artifacts: Mapping[str, bytes],
    package_manifest_sha256: str,
) -> str:
    """Verify one closed release and return its deterministic tree hash."""

    if not _is_hash(package_manifest_sha256):
        raise ControllerRuntimeCapabilityError(
            "controller_release_manifest_identity_invalid"
        )
    artifact_digests = _package_artifact_digests(package_artifacts)
    expected_files = dict(artifact_digests)
    manifest_path = str(PACKAGE_MANIFEST_RELATIVE_PATH)
    expected_files[manifest_path] = package_manifest_sha256

    expected_directories: set[str] = set()
    for path in expected_files:
        parts = PurePosixPath(path).parts
        for limit in range(1, len(parts)):
            expected_directories.add("/".join(parts[:limit]))

    expected_children: dict[str, dict[str, str]] = {}
    for directory in sorted(expected_directories):
        parent, _, name = directory.rpartition("/")
        expected_children.setdefault(parent, {})[name] = "directory"
    for path in sorted(expected_files):
        parent, _, name = path.rpartition("/")
        expected_children.setdefault(parent, {})[name] = "file"

    entries: list[tuple[str, int, str]] = []
    manifest_raw: bytes | None = None

    def walk(current_fd: int, prefix: str) -> None:
        nonlocal manifest_raw
        before = os.fstat(current_fd)
        before_identity = _directory_identity(before)
        mode = stat.S_IMODE(before.st_mode)
        if (
            not stat.S_ISDIR(before.st_mode)
            or before.st_uid != _EXPECTED_UID
            or mode & 0o222
        ):
            raise ControllerRuntimeCapabilityError(
                "controller_release_tree_identity_invalid"
            )
        try:
            names = sorted(os.listdir(current_fd))
        except OSError as error:
            raise ControllerRuntimeCapabilityError(
                "controller_release_tree_probe_failed"
            ) from error
        expected = expected_children.get(prefix, {})
        if names != sorted(expected):
            raise ControllerRuntimeCapabilityError(
                "controller_release_tree_set_mismatch"
            )
        for name in names:
            if name in {".", ".."} or "/" in name or "\x00" in name:
                raise ControllerRuntimeCapabilityError(
                    "controller_release_tree_invalid"
                )
            relative = f"{prefix}/{name}" if prefix else name
            info = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
            child_mode = stat.S_IMODE(info.st_mode)
            if info.st_uid != _EXPECTED_UID or child_mode & 0o222:
                raise ControllerRuntimeCapabilityError(
                    "controller_release_tree_identity_invalid"
                )
            if expected[name] == "directory":
                if not stat.S_ISDIR(info.st_mode):
                    raise ControllerRuntimeCapabilityError(
                        "controller_release_tree_special_file_forbidden"
                    )
                child = _open_relative_directory(
                    current_fd, PurePosixPath(name)
                )
                try:
                    entries.append((relative + "/", child_mode, "directory"))
                    walk(child, relative)
                finally:
                    os.close(child)
            else:
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ControllerRuntimeCapabilityError(
                        "controller_release_tree_special_file_forbidden"
                    )
                raw, digest, opened_mode = _read_regular(
                    current_fd,
                    name,
                    executable=bool(child_mode & 0o111),
                )
                if opened_mode != child_mode:
                    raise ControllerRuntimeCapabilityError(
                        "controller_release_tree_changed_during_read"
                    )
                if digest != expected_files[relative]:
                    raise ControllerRuntimeCapabilityError(
                        "controller_release_artifact_identity_mismatch"
                    )
                if relative == manifest_path:
                    manifest_raw = raw
                entries.append((relative, child_mode, digest))
        after = os.fstat(current_fd)
        if (
            _directory_identity(after) != before_identity
            or sorted(os.listdir(current_fd)) != names
        ):
            raise ControllerRuntimeCapabilityError(
                "controller_release_tree_changed_during_read"
            )

    walk(release_fd, "")
    if manifest_raw is None:
        raise ControllerRuntimeCapabilityError(
            "controller_release_manifest_invalid"
        )
    _release_manifest_document(
        manifest_raw,
        expected_manifest_sha256=package_manifest_sha256,
        expected_artifacts=artifact_digests,
    )
    root_mode = stat.S_IMODE(os.fstat(release_fd).st_mode)
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "schema_version": "governed-memory-controller-release-tree-v1",
                "package_manifest_path": manifest_path,
                "package_manifest_sha256": package_manifest_sha256,
                "root_mode": root_mode,
                "entries": [
                    {"path": path, "mode": mode, "sha256": digest}
                    for path, mode, digest in entries
                ],
            }
        )
    ).hexdigest()


def _hash_tree(directory_fd: int) -> str:
    entries: list[tuple[str, int, str]] = []

    def walk(current_fd: int, prefix: str) -> None:
        for name in sorted(os.listdir(current_fd)):
            if name in {".", ".."} or "/" in name or "\x00" in name:
                raise ControllerRuntimeCapabilityError(
                    "controller_runtime_tree_invalid"
                )
            info = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
            relative = f"{prefix}/{name}" if prefix else name
            mode = stat.S_IMODE(info.st_mode)
            if info.st_uid != _EXPECTED_UID or mode & 0o222:
                raise ControllerRuntimeCapabilityError(
                    "controller_runtime_tree_identity_invalid"
                )
            if stat.S_ISDIR(info.st_mode):
                child = _open_relative_directory(
                    current_fd, PurePosixPath(name)
                )
                try:
                    entries.append((relative + "/", mode, "directory"))
                    walk(child, relative)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise ControllerRuntimeCapabilityError(
                        "controller_runtime_tree_identity_invalid"
                    )
                unused_raw, digest, opened_mode = _read_regular(
                    current_fd,
                    name,
                    executable=bool(mode & 0o111),
                )
                if opened_mode != mode:
                    raise ControllerRuntimeCapabilityError(
                        "controller_runtime_tree_changed_during_read"
                    )
                entries.append((relative, mode, digest))
            else:
                raise ControllerRuntimeCapabilityError(
                    "controller_runtime_tree_special_file_forbidden"
                )

    walk(directory_fd, "")
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "schema_version": "governed-memory-controller-runtime-tree-v1",
                "entries": [
                    {"path": path, "mode": mode, "sha256": digest}
                    for path, mode, digest in entries
                ],
            }
        )
    ).hexdigest()


def _observe_filesystem(
    *,
    runtime_root: PurePosixPath,
    launcher_path: PurePosixPath,
    package_artifacts: Mapping[str, bytes],
    package_manifest_sha256: str,
) -> _ObservedRuntime:
    try:
        runtime_fd = _open_absolute_directory(runtime_root)
        try:
            runtime_info = os.fstat(runtime_fd)
            if stat.S_IMODE(runtime_info.st_mode) != 0o555:
                raise ControllerRuntimeCapabilityError(
                    "controller_runtime_root_mode_invalid"
                )
            bin_fd = _open_relative_directory(runtime_fd, PurePosixPath("bin"))
            try:
                unused_python, interpreter_sha256, unused_mode = _read_regular(
                    bin_fd, "python", executable=True
                )
            finally:
                os.close(bin_fd)
            inventory, inventory_sha256, unused_mode = _read_regular(
                runtime_fd,
                str(INVENTORY_RELATIVE_PATH),
                executable=False,
            )
            runtime_tree_sha256 = _hash_tree(runtime_fd)
        finally:
            os.close(runtime_fd)
        release_root = launcher_path.parents[2]
        release_fd = _open_absolute_directory(release_root)
        try:
            release_info = os.fstat(release_fd)
            if stat.S_IMODE(release_info.st_mode) != 0o555:
                raise ControllerRuntimeCapabilityError(
                    "controller_release_root_mode_invalid"
                )
            release_tree_sha256 = _observe_release_tree(
                release_fd,
                package_artifacts=package_artifacts,
                package_manifest_sha256=package_manifest_sha256,
            )
            launcher_parent = _open_relative_directory(
                release_fd, LAUNCHER_RELATIVE_PATH.parent
            )
            try:
                unused_launcher, launcher_sha256, unused_mode = _read_regular(
                    launcher_parent,
                    LAUNCHER_RELATIVE_PATH.name,
                    executable=True,
                )
            finally:
                os.close(launcher_parent)
        finally:
            os.close(release_fd)
    except ControllerRuntimeCapabilityError:
        raise
    except OSError as error:
        raise ControllerRuntimeCapabilityError(
            "controller_runtime_filesystem_probe_failed"
        ) from error
    return _ObservedRuntime(
        runtime_tree_sha256,
        release_tree_sha256,
        interpreter_sha256,
        inventory_sha256,
        inventory,
        launcher_sha256,
    )


def _normalized_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _requirements_lock_bindings(
    raw: bytes,
) -> dict[str, tuple[str, frozenset[str]]]:
    """Parse exact versions and hashes from the narrow runtime lock."""

    try:
        text = raw.decode("ascii")
    except UnicodeError as error:
        raise ControllerRuntimeCapabilityError(
            "controller_runtime_requirements_lock_invalid"
        ) from error
    logical_lines: list[str] = []
    pending = ""
    for physical in text.splitlines():
        stripped = physical.strip()
        if not stripped or stripped.startswith("#"):
            if pending:
                raise ControllerRuntimeCapabilityError(
                    "controller_runtime_requirements_lock_invalid"
                )
            continue
        continued = stripped.endswith("\\")
        fragment = stripped[:-1].rstrip() if continued else stripped
        if not fragment:
            raise ControllerRuntimeCapabilityError(
                "controller_runtime_requirements_lock_invalid"
            )
        pending = f"{pending} {fragment}".strip()
        if not continued:
            logical_lines.append(pending)
            pending = ""
    if pending or not logical_lines:
        raise ControllerRuntimeCapabilityError(
            "controller_runtime_requirements_lock_invalid"
        )

    required: dict[str, tuple[str, frozenset[str]]] = {}
    head_pattern = re.compile(
        r"([A-Za-z0-9][A-Za-z0-9._-]*)=="
        r"([A-Za-z0-9][A-Za-z0-9.+!_-]*)\Z",
        re.ASCII,
    )
    hash_pattern = re.compile(r"--hash=sha256:([0-9a-f]{64})\Z", re.ASCII)
    for logical in logical_lines:
        tokens = logical.split()
        match = head_pattern.fullmatch(tokens[0]) if tokens else None
        hash_matches = [hash_pattern.fullmatch(token) for token in tokens[1:]]
        if match is None or not hash_matches or any(
            item is None for item in hash_matches
        ):
            raise ControllerRuntimeCapabilityError(
                "controller_runtime_requirements_lock_invalid"
            )
        normalized = _normalized_distribution_name(match.group(1))
        if (
            normalized in required
            or len(
                {
                    item.group(1)
                    for item in hash_matches
                    if item is not None
                }
            )
            != len(hash_matches)
        ):
            raise ControllerRuntimeCapabilityError(
                "controller_runtime_requirements_lock_invalid"
            )
        required[normalized] = (
            match.group(2),
            frozenset(
                item.group(1)
                for item in hash_matches
                if item is not None
            ),
        )
    return dict(sorted(required.items()))


def _requirements_from_lock(raw: bytes) -> dict[str, str]:
    """Return exact normalized distributions and versions from the lock."""

    return {
        name: version
        for name, (version, unused_hashes) in _requirements_lock_bindings(
            raw
        ).items()
    }


def _module_name_for_path(path: str) -> tuple[str, bool] | None:
    prefix = "tools/governed_memory_install/"
    if not path.startswith(prefix) or not path.endswith(".py"):
        return None
    relative = path[len(prefix) :]
    parts = relative.split("/")
    if any(not part for part in parts):
        return None
    if parts[-1] == "__init__.py":
        suffix = parts[:-1]
        is_package = True
    else:
        suffix = [*parts[:-1], parts[-1][:-3]]
        is_package = False
    if any(not part.isidentifier() for part in suffix):
        return None
    name = ".".join(["tools", "governed_memory_install", *suffix])
    return name, is_package


def _verify_launcher_module_closure(
    package_artifacts: Mapping[str, bytes],
) -> frozenset[str]:
    """Reject a release whose exact launcher can reach an unsealed module."""

    module_index: dict[str, tuple[str, bytes, bool]] = {}
    for path, raw in package_artifacts.items():
        module = _module_name_for_path(path)
        if module is None:
            continue
        name, is_package = module
        if name in module_index or type(raw) is not bytes:
            raise ControllerRuntimeCapabilityError(
                "controller_release_module_closure_invalid"
            )
        module_index[name] = (path, raw, is_package)

    launcher_module = _module_name_for_path(str(LAUNCHER_RELATIVE_PATH))
    package_name = "tools.governed_memory_install"
    if (
        launcher_module is None
        or launcher_module[0] not in module_index
        or package_name not in module_index
    ):
        raise ControllerRuntimeCapabilityError(
            "controller_release_module_closure_invalid"
        )

    pending = [launcher_module[0], package_name]
    reachable: set[str] = set()

    def require_local(name: str, dependencies: set[str]) -> None:
        if name == package_name or name.startswith(package_name + "."):
            if name not in module_index:
                raise ControllerRuntimeCapabilityError(
                    "controller_release_module_closure_invalid"
                )
            dependencies.add(name)

    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        path, raw, is_package = module_index[name]
        try:
            tree = ast.parse(raw.decode("utf-8"), filename=path)
        except (UnicodeError, SyntaxError) as error:
            raise ControllerRuntimeCapabilityError(
                "controller_release_module_closure_invalid"
            ) from error
        dependencies: set[str] = {package_name}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "__import__"
                ) or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "import_module"
                ):
                    raise ControllerRuntimeCapabilityError(
                        "controller_release_dynamic_module_load_forbidden"
                    )
            if isinstance(node, ast.Import):
                for alias in node.names:
                    require_local(alias.name, dependencies)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    context = name.split(".") if is_package else name.split(".")[:-1]
                    if node.level > len(context):
                        raise ControllerRuntimeCapabilityError(
                            "controller_release_module_closure_invalid"
                        )
                    base_parts = context[: len(context) - node.level + 1]
                    if node.module:
                        base_parts.extend(node.module.split("."))
                    base = ".".join(base_parts)
                else:
                    base = node.module or ""
                require_local(base, dependencies)
                for alias in node.names:
                    candidate = f"{base}.{alias.name}" if base else alias.name
                    if candidate in module_index:
                        dependencies.add(candidate)
        reachable.add(name)
        pending.extend(sorted(dependencies - reachable))
    return frozenset(reachable)


_INVENTORY_PROBE: Final = r"""
import hashlib
import importlib.metadata
import json
import platform
from pathlib import Path
import re
import site
import sys
import sysconfig

runtime_root = Path(sys.argv[1]).resolve(strict=True)

def relative_to_runtime(value, *, allow_missing_stdlib_zip=False):
    candidate = Path(value)
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as error:
        expected_zip = runtime_root / "lib" / (
            f"python{sys.version_info.major}{sys.version_info.minor}.zip"
        )
        if (
            not allow_missing_stdlib_zip
            or not candidate.is_absolute()
            or candidate != expected_zip
            or candidate.is_symlink()
        ):
            raise SystemExit("controller_runtime_import_path_invalid") from error
        resolved = candidate.parent.resolve(strict=True) / candidate.name
    try:
        relative = resolved.relative_to(runtime_root)
    except ValueError as error:
        raise SystemExit("controller_runtime_import_path_escape") from error
    rendered = relative.as_posix()
    return rendered if rendered != "." else "."

def hash_regular(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            block = source.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()

if not sys.path or any(not isinstance(value, str) or not value for value in sys.path):
    raise SystemExit("controller_runtime_import_path_invalid")

prefixes = {
    "base_exec_prefix": relative_to_runtime(sys.base_exec_prefix),
    "base_prefix": relative_to_runtime(sys.base_prefix),
    "exec_prefix": relative_to_runtime(sys.exec_prefix),
    "prefix": relative_to_runtime(sys.prefix),
}
if set(prefixes.values()) != {"."}:
    raise SystemExit("controller_runtime_not_standalone")

import_paths = [
    relative_to_runtime(value, allow_missing_stdlib_zip=True)
    for value in sys.path
]
if len(import_paths) != len(set(import_paths)):
    raise SystemExit("controller_runtime_duplicate_import_path")

configured = sysconfig.get_paths()
stdlib_paths = {
    key: relative_to_runtime(configured[key])
    for key in ("stdlib", "platstdlib")
}
site_package_paths = sorted(
    {
        relative_to_runtime(value)
        for value in site.getsitepackages([sys.prefix, sys.exec_prefix])
    }
)
base_site_package_paths = {
    relative_to_runtime(value)
    for value in site.getsitepackages([sys.base_prefix, sys.base_exec_prefix])
}
system_site_candidates = base_site_package_paths - set(site_package_paths)
system_site_packages_enabled = any(
    value in set(import_paths) for value in system_site_candidates
)

path_facts = {
    "import_paths": import_paths,
    "prefixes": prefixes,
    "site_package_paths": site_package_paths,
    "stdlib_paths": stdlib_paths,
}
path_facts_sha256 = hashlib.sha256(
    json.dumps(
        path_facts,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
).hexdigest()

distributions = {}
for distribution in importlib.metadata.distributions():
    raw_name = distribution.metadata["Name"]
    if not raw_name:
        raise SystemExit("controller_runtime_distribution_name_invalid")
    name = re.sub(r"[-_.]+", "-", raw_name).lower()
    if name in distributions:
        raise SystemExit("controller_runtime_distribution_duplicate")
    distributions[name] = distribution.version

try:
    import psycopg
    from psycopg import pq
except Exception as error:
    raise SystemExit("controller_runtime_psycopg_import_failed") from error
if psycopg.__version__ != distributions.get("psycopg") or pq.__impl__ != "binary":
    raise SystemExit("controller_runtime_psycopg_identity_invalid")
binary_distribution = importlib.metadata.distribution("psycopg-binary")
site_root = Path(binary_distribution.locate_file("")).resolve(strict=True)
relative_to_runtime(site_root)
native_files = []
for item in sorted(binary_distribution.files or (), key=lambda value: str(value)):
    relative_name = str(item).replace("\\", "/")
    if not (relative_name.endswith(".so") or ".so." in relative_name):
        continue
    path = Path(binary_distribution.locate_file(item)).resolve(strict=True)
    relative_to_runtime(path)
    try:
        relative = path.relative_to(site_root).as_posix()
    except ValueError as error:
        raise SystemExit("controller_runtime_psycopg_native_path_escape") from error
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise SystemExit("controller_runtime_psycopg_native_file_invalid")
    native_files.append(
        {
            "path": relative,
            "size": path.stat().st_size,
            "sha256": hash_regular(path),
        }
    )
if not native_files:
    raise SystemExit("controller_runtime_psycopg_native_files_absent")
postgresql_driver = {
    "schema_version": "governed-memory-psycopg-runtime-probe-v1",
    "python_version": platform.python_version(),
    "psycopg_version": psycopg.__version__,
    "pq_impl": pq.__impl__,
    "libpq_version": pq.version(),
    "native_files": native_files,
}
postgresql_driver_identity_sha256 = hashlib.sha256(
    json.dumps(
        postgresql_driver,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
).hexdigest()

output = {
    "distributions": dict(sorted(distributions.items())),
    "interpreter_path_facts": path_facts,
    "interpreter_path_facts_sha256": path_facts_sha256,
    "postgresql_driver_identity_sha256": postgresql_driver_identity_sha256,
    "pip_present": "pip" in distributions,
    "platform_architecture": platform.machine(),
    "platform_os": sys.platform,
    "python_implementation": platform.python_implementation(),
    "python_version": platform.python_version(),
    "setuptools_present": "setuptools" in distributions,
    "system_site_packages_enabled": system_site_packages_enabled,
    "user_site_enabled": site.ENABLE_USER_SITE is True,
    "wheel_present": "wheel" in distributions,
}
print(json.dumps(output, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
"""


def _run_process_probes(
    *, interpreter_path: str, launcher_path: str, package_sha256: str
) -> _ProcessProbeResult:
    environment = {
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }

    def run(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
        try:
            result = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd="/",
                env=environment,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ControllerRuntimeCapabilityError(
                "controller_runtime_process_probe_failed"
            ) from error
        if (
            result.returncode != 0
            or len(result.stdout) > MAX_PROBE_BYTES
            or len(result.stderr) > MAX_PROBE_BYTES
        ):
            raise ControllerRuntimeCapabilityError(
                "controller_runtime_process_probe_failed"
            )
        return result

    inventory = run(
        [
            interpreter_path,
            "-I",
            "-B",
            "-c",
            _INVENTORY_PROBE,
            str(PurePosixPath(interpreter_path).parents[1]),
        ]
    )
    help_result = run(
        [
            interpreter_path,
            "-I",
            "-B",
            launcher_path,
            "--package-manifest-sha256",
            package_sha256,
            "--help",
        ]
    )
    help_probe_sha256 = hashlib.sha256(
        canonical_json_bytes(
            {
                "schema_version": "governed-memory-supervisor-help-probe-v1",
                "exit_code": help_result.returncode,
                "stdout_sha256": hashlib.sha256(help_result.stdout).hexdigest(),
                "stderr_sha256": hashlib.sha256(help_result.stderr).hexdigest(),
            }
        )
    ).hexdigest()
    return _ProcessProbeResult(inventory.stdout, help_probe_sha256)


def verify_controller_runtime_capability(
    verified_package_capability: object,
    *,
    runtime_build_receipt_json: bytes | str,
) -> object:
    """Probe exact installed paths and mint a package-bound capability."""

    try:
        package, unused_scope, package_artifacts = _package_capability_parts(
            verified_package_capability
        )
    except PackageCapabilityError as error:
        raise ControllerRuntimeCapabilityError(
            "controller_runtime_package_capability_invalid"
        ) from error
    artifact_digests = _package_artifact_digests(package_artifacts)
    lock_path = str(REQUIREMENTS_LOCK_RELATIVE_PATH)
    launcher_relative = str(LAUNCHER_RELATIVE_PATH)
    runtime_contract_path = str(CONTROLLER_RUNTIME_CONTRACT_RELATIVE_PATH)
    if (
        artifact_digests.get(lock_path)
        != package.controller_requirements_lock_sha256
        or artifact_digests.get(launcher_relative)
        != package.supervisor_launcher_sha256
        or artifact_digests.get(runtime_contract_path)
        != package.controller_runtime_contract_sha256
    ):
        raise ControllerRuntimeCapabilityError(
            "controller_runtime_package_binding_mismatch"
        )
    selected_runtime_inputs = _selected_runtime_input_identity(
        package_artifacts[runtime_contract_path],
        expected_contract_sha256=(
            package.controller_runtime_contract_sha256
        ),
        requirements_lock=package_artifacts[lock_path],
    )
    required_distributions = _requirements_from_lock(
        package_artifacts[lock_path]
    )
    _verify_launcher_module_closure(package_artifacts)
    raw, receipt = _receipt_document(runtime_build_receipt_json)
    _require_closed_receipt(receipt)
    receipt_sha256 = hashlib.sha256(raw).hexdigest()
    if (
        receipt_sha256 != package.controller_runtime_receipt_sha256
        or receipt["package_manifest_sha256"]
        != package.package_manifest_sha256
        or receipt["controller_runtime_contract_sha256"]
        != package.controller_runtime_contract_sha256
        or receipt["controller_requirements_lock_sha256"]
        != package.controller_requirements_lock_sha256
        or receipt["supervisor_launcher_sha256"]
        != package.supervisor_launcher_sha256
    ):
        raise ControllerRuntimeCapabilityError(
            "controller_runtime_package_binding_mismatch"
        )
    if (
        receipt["python_version"] != selected_runtime_inputs.python_version
        or receipt["standalone_cpython_archive_sha256"]
        != selected_runtime_inputs.archive_sha256
        or receipt["standalone_cpython_specification_sha256"]
        != selected_runtime_inputs.specification_sha256
        or receipt["standalone_cpython_payload_tree_sha256"]
        != selected_runtime_inputs.payload_tree_sha256
        or receipt["wheelhouse_tree_sha256"]
        != selected_runtime_inputs.wheelhouse_tree_sha256
    ):
        raise ControllerRuntimeCapabilityError(
            "controller_runtime_input_binding_mismatch"
        )
    runtime_root = RUNTIME_ROOT_PREFIX / receipt_sha256
    release_root = RELEASE_ROOT_PREFIX / package.package_manifest_sha256
    interpreter_path = runtime_root / INTERPRETER_RELATIVE_PATH
    inventory_path = runtime_root / INVENTORY_RELATIVE_PATH
    package_manifest_path = release_root / PACKAGE_MANIFEST_RELATIVE_PATH
    launcher_path = release_root / LAUNCHER_RELATIVE_PATH
    observed = _observe_filesystem(
        runtime_root=runtime_root,
        launcher_path=launcher_path,
        package_artifacts=package_artifacts,
        package_manifest_sha256=package.package_manifest_sha256,
    )
    if (
        observed.runtime_tree_sha256 != receipt["runtime_tree_sha256"]
        or observed.release_tree_sha256 != receipt["release_tree_sha256"]
        or observed.interpreter_sha256 != receipt["interpreter_sha256"]
        or observed.inventory_sha256
        != receipt["installed_distribution_inventory_sha256"]
        or observed.launcher_sha256 != receipt["supervisor_launcher_sha256"]
    ):
        raise ControllerRuntimeCapabilityError(
            "controller_runtime_filesystem_binding_mismatch"
        )
    probes = _run_process_probes(
        interpreter_path=str(interpreter_path),
        launcher_path=str(launcher_path),
        package_sha256=package.package_manifest_sha256,
    )
    if (
        probes.inventory_bytes != observed.inventory_bytes
        or probes.launcher_help_probe_sha256
        != receipt["launcher_help_probe_sha256"]
    ):
        raise ControllerRuntimeCapabilityError(
            "controller_runtime_process_binding_mismatch"
        )
    try:
        inventory = json.loads(
            observed.inventory_bytes.decode("ascii"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda unused: (_ for _ in ()).throw(
                ControllerRuntimeCapabilityError(
                    "controller_runtime_inventory_invalid"
                )
            ),
        )
    except ControllerRuntimeCapabilityError:
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ControllerRuntimeCapabilityError(
            "controller_runtime_inventory_invalid"
        ) from error
    if type(inventory) is not dict:
        raise ControllerRuntimeCapabilityError(
            "controller_runtime_inventory_invalid"
        )
    inventory_facts = {
        "python_implementation": inventory.get("python_implementation"),
        "python_version": inventory.get("python_version"),
        "platform_os": inventory.get("platform_os"),
        "platform_architecture": inventory.get("platform_architecture"),
        "pip_present": inventory.get("pip_present"),
        "setuptools_present": inventory.get("setuptools_present"),
        "wheel_present": inventory.get("wheel_present"),
        "user_site_enabled": inventory.get("user_site_enabled"),
        "system_site_packages_enabled": inventory.get(
            "system_site_packages_enabled"
        ),
    }
    path_facts = inventory.get("interpreter_path_facts")
    path_facts_sha256 = inventory.get("interpreter_path_facts_sha256")
    postgresql_driver_identity_sha256 = inventory.get(
        "postgresql_driver_identity_sha256"
    )
    distributions = inventory.get("distributions")
    if (
        type(path_facts) is not dict
        or not _is_hash(path_facts_sha256)
        or hashlib.sha256(canonical_json_bytes(path_facts)).hexdigest()
        != path_facts_sha256
        or path_facts_sha256 != receipt["interpreter_path_facts_sha256"]
    ):
        raise ControllerRuntimeCapabilityError(
            "controller_runtime_import_path_facts_invalid"
        )
    if (
        set(inventory)
        != {
            "distributions",
            "interpreter_path_facts",
            "interpreter_path_facts_sha256",
            "postgresql_driver_identity_sha256",
            *inventory_facts,
        }
        or canonical_json_bytes(inventory) + b"\n" != observed.inventory_bytes
        or any(receipt.get(key) != value for key, value in inventory_facts.items())
        or postgresql_driver_identity_sha256
        != EXPECTED_POSTGRESQL_DRIVER_IDENTITY_SHA256
        or postgresql_driver_identity_sha256
        != receipt["postgresql_driver_identity_sha256"]
    ):
        raise ControllerRuntimeCapabilityError(
            "controller_runtime_inventory_invalid"
        )
    if (
        type(distributions) is not dict
        or any(
            type(name) is not str
            or _normalized_distribution_name(name) != name
            or type(version) is not str
            or not version
            for name, version in distributions.items()
        )
        or distributions != required_distributions
    ):
        raise ControllerRuntimeCapabilityError(
            "controller_runtime_distribution_inventory_mismatch"
        )
    evidence = VerifiedControllerRuntimeEvidence(
        result_type="verified_controller_runtime_v1",
        controller_runtime_receipt_sha256=receipt_sha256,
        build_plan_sha256=str(receipt["build_plan_sha256"]),
        package_manifest_sha256=package.package_manifest_sha256,
        controller_runtime_contract_sha256=(
            package.controller_runtime_contract_sha256
        ),
        controller_requirements_lock_sha256=(
            package.controller_requirements_lock_sha256
        ),
        standalone_cpython_specification_sha256=str(
            receipt["standalone_cpython_specification_sha256"]
        ),
        standalone_cpython_archive_sha256=str(
            receipt["standalone_cpython_archive_sha256"]
        ),
        standalone_cpython_payload_tree_sha256=str(
            receipt["standalone_cpython_payload_tree_sha256"]
        ),
        wheelhouse_tree_sha256=str(receipt["wheelhouse_tree_sha256"]),
        runtime_root=str(runtime_root),
        runtime_tree_sha256=str(receipt["runtime_tree_sha256"]),
        release_root=str(release_root),
        release_tree_sha256=str(receipt["release_tree_sha256"]),
        package_manifest_path=str(package_manifest_path),
        interpreter_path=str(interpreter_path),
        interpreter_sha256=str(receipt["interpreter_sha256"]),
        inventory_path=str(inventory_path),
        installed_distribution_inventory_sha256=str(
            receipt["installed_distribution_inventory_sha256"]
        ),
        interpreter_path_facts_sha256=str(
            receipt["interpreter_path_facts_sha256"]
        ),
        postgresql_driver_identity_sha256=str(
            receipt["postgresql_driver_identity_sha256"]
        ),
        supervisor_launcher_path=str(launcher_path),
        supervisor_launcher_sha256=str(receipt["supervisor_launcher_sha256"]),
        launcher_help_probe_sha256=str(receipt["launcher_help_probe_sha256"]),
        python_implementation=str(receipt["python_implementation"]),
        python_version=str(receipt["python_version"]),
        platform_os=str(receipt["platform_os"]),
        platform_architecture=str(receipt["platform_architecture"]),
        persistent_controller_substrate_created=True,
        persistent_store_resources_created=False,
    )
    return _VerifiedControllerRuntimeCapability(evidence, _RUNTIME_TOKEN)


__all__ = [
    "ControllerRuntimeCapabilityError",
    "EXPECTED_POSTGRESQL_DRIVER_IDENTITY_SHA256",
    "VerifiedControllerRuntimeEvidence",
    "verified_controller_runtime_evidence",
    "verify_controller_runtime_capability",
]
