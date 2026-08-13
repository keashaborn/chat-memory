from __future__ import annotations

"""Fail-closed planner for the immutable controller runtime and release.

Phase 9D packages orchestration only.  The module never opens a socket, runs a
process, extracts an archive, or writes a path.  A later, separately authorized
Linux transport must implement the typed operations below.  The orchestrator
keeps path derivation, exact release closure, receipt construction, create-only
publication, and recovery/refusal policy inside reviewed controller code.
"""

from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
import re
from types import MappingProxyType
from typing import Final, Mapping, Protocol

from tools.governed_memory_install.authority import canonical_json_bytes
from tools.governed_memory_install.controller_runtime import (
    INTERPRETER_RELATIVE_PATH,
    INVENTORY_RELATIVE_PATH,
    LAUNCHER_RELATIVE_PATH,
    PACKAGE_MANIFEST_RELATIVE_PATH,
    RELEASE_ROOT_PREFIX,
    REQUIREMENTS_LOCK_RELATIVE_PATH,
    RUNTIME_RECEIPT_RESULT,
    RUNTIME_RECEIPT_SCHEMA,
    RUNTIME_ROOT_PREFIX,
    _requirements_from_lock,
)


SUBSTRATE_SCHEMA: Final = (
    "governed-memory-controller-standalone-cpython-substrate-v1"
)
SUBSTRATE_STATE: Final = "externally-approved-exact-offline-substrate"
BUILD_PLAN_SCHEMA: Final = "governed-memory-controller-runtime-build-plan-v1"
BUILD_RESULT_TYPE: Final = "controller_runtime_and_release_staged_v1"
PUBLICATION_RESULT_TYPE: Final = (
    "controller_runtime_release_and_receipt_published_v1"
)
STAGING_ROOT_PREFIX: Final = PurePosixPath(
    "/var/lib/governed-memory-controller/build-staging"
)
OFFLINE_SUBSTRATE_ROOT_PREFIX: Final = PurePosixPath(
    "/var/lib/governed-memory-controller/offline/cpython"
)
OFFLINE_WHEELHOUSE_ROOT_PREFIX: Final = PurePosixPath(
    "/var/lib/governed-memory-controller/offline/wheelhouses"
)
RUNTIME_RECEIPT_ROOT_PREFIX: Final = PurePosixPath(
    "/var/lib/governed-memory-controller/runtime-receipts"
)
CONTROLLER_RUNTIME_CONTRACT_RELATIVE_PATH: Final = PurePosixPath(
    "ops/governed_memory/installation/current/controller_runtime_contract.json"
)
MAX_DOCUMENT_BYTES: Final = 256 * 1024
MAX_RELEASE_ARTIFACTS: Final = 512
MAX_RELEASE_BYTES: Final = 32 * 1024 * 1024

_HASH_RE: Final = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_VERSION_RE: Final = re.compile(r"3\.12\.[0-9]+\Z", re.ASCII)
_ARCHIVE_NAME_RE: Final = re.compile(
    r"cpython-[A-Za-z0-9][A-Za-z0-9._+-]{0,180}\.tar\Z", re.ASCII
)
_SUBSTRATE_KEYS: Final = frozenset(
    {
        "schema_version",
        "state",
        "implementation",
        "python_version",
        "platform_os",
        "platform_architecture",
        "distribution_kind",
        "archive_name",
        "archive_sha256",
        "payload_tree_sha256",
        "payload_root",
        "interpreter_relative_path",
        "archive_members_are_regular_files_or_directories_only",
        "archive_contains_no_symlinks_hardlinks_or_special_files",
        "runtime_is_not_venv",
        "network_calls",
    }
)
_RECEIPT_KEYS: Final = frozenset(
    {
        "schema_version",
        "result",
        "package_manifest_sha256",
        "controller_runtime_contract_sha256",
        "controller_requirements_lock_sha256",
        "python_implementation",
        "python_version",
        "platform_os",
        "platform_architecture",
        "runtime_tree_sha256",
        "release_tree_sha256",
        "interpreter_sha256",
        "installed_distribution_inventory_sha256",
        "interpreter_path_facts_sha256",
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


class ControllerRuntimeBuildError(RuntimeError):
    """Content-free refusal from runtime/release planning or orchestration."""


@dataclass(frozen=True, slots=True)
class StandaloneCPythonSubstrate:
    specification_sha256: str
    python_version: str
    archive_name: str
    archive_sha256: str
    payload_tree_sha256: str
    archive_path: str


@dataclass(frozen=True, slots=True)
class ControllerRuntimeBuildPlan:
    schema_version: str
    build_plan_sha256: str
    build_nonce: str
    package_manifest_sha256: str
    controller_runtime_contract_sha256: str
    controller_requirements_lock_sha256: str
    release_artifact_count: int
    release_closure_sha256: str
    supervisor_launcher_sha256: str
    required_distributions: Mapping[str, str]
    substrate: StandaloneCPythonSubstrate
    wheelhouse_tree_sha256: str
    wheelhouse_root: str
    stage_root: str
    staged_runtime_root: str
    staged_release_root: str
    final_release_root: str
    operations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StageObservation:
    state: str
    build_plan_sha256: str | None
    root_mode: int | None = None
    root_uid: int | None = None
    root_gid: int | None = None
    marker_regular_no_follow: bool | None = None
    created_no_replace: bool | None = None
    parent_fsynced: bool | None = None


@dataclass(frozen=True, slots=True)
class BuildObservation:
    result_type: str
    build_plan_sha256: str
    substrate_specification_sha256: str
    substrate_archive_sha256: str
    substrate_payload_tree_sha256: str
    wheelhouse_tree_sha256: str
    release_closure_sha256: str
    release_artifact_count: int
    python_implementation: str
    python_version: str
    platform_os: str
    platform_architecture: str
    runtime_tree_sha256: str
    release_tree_sha256: str
    interpreter_sha256: str
    installed_distribution_inventory_sha256: str
    interpreter_path_facts_sha256: str
    supervisor_launcher_sha256: str
    launcher_help_probe_sha256: str
    pip_present: bool
    setuptools_present: bool
    wheel_present: bool
    user_site_enabled: bool
    system_site_packages_enabled: bool
    normal_venv_created: bool
    offline_substrate_regular_root_owned_nonwritable: bool
    offline_wheelhouse_regular_root_owned_nonwritable: bool
    runtime_root_mode: int
    release_root_mode: int
    all_members_root_owned: bool
    writable_member_count: int
    symlink_count: int
    hardlink_count: int
    special_file_count: int
    network_calls: int
    provider_calls: int
    production_data_read: bool
    active_production_state_changed: bool


@dataclass(frozen=True, slots=True)
class PublicationTargetObservation:
    runtime_state: str
    release_state: str
    receipt_state: str
    secure_no_follow_observation: bool


@dataclass(frozen=True, slots=True)
class PublicationObservation:
    result_type: str
    runtime_root: str
    release_root: str
    receipt_path: str
    runtime_rename_no_replace: bool
    release_rename_no_replace: bool
    receipt_create_no_replace: bool
    runtime_root_mode: int
    release_root_mode: int
    receipt_mode: int
    root_uid: int
    root_gid: int
    runtime_parent_fsynced: bool
    release_parent_fsynced: bool
    receipt_file_fsynced: bool
    receipt_parent_fsynced: bool
    stage_absent: bool


@dataclass(frozen=True, slots=True)
class ControllerRuntimeBuildResult:
    build_plan_sha256: str
    runtime_build_receipt_json: bytes
    controller_runtime_receipt_sha256: str
    runtime_root: str
    release_root: str
    receipt_path: str
    runtime_tree_sha256: str
    release_tree_sha256: str


class ControllerRuntimeBuildTransport(Protocol):
    """Privileged effects required later; no implementation ships in Phase 9D."""

    def observe_stage(self, plan: ControllerRuntimeBuildPlan) -> StageObservation:
        ...

    def recover_owned_partial_stage(
        self, plan: ControllerRuntimeBuildPlan
    ) -> StageObservation:
        ...

    def create_stage(self, plan: ControllerRuntimeBuildPlan) -> StageObservation:
        ...

    def materialize_standalone_substrate(
        self, plan: ControllerRuntimeBuildPlan
    ) -> None:
        ...

    def install_locked_offline_distributions(
        self, plan: ControllerRuntimeBuildPlan
    ) -> None:
        ...

    def remove_bootstrap_packaging_tools(
        self, plan: ControllerRuntimeBuildPlan
    ) -> None:
        ...

    def stage_exact_release(
        self,
        plan: ControllerRuntimeBuildPlan,
        *,
        package_manifest_json: bytes,
        package_artifacts: Mapping[str, bytes],
    ) -> None:
        ...

    def seal_and_observe(
        self, plan: ControllerRuntimeBuildPlan
    ) -> BuildObservation:
        ...

    def observe_publication_targets(
        self,
        plan: ControllerRuntimeBuildPlan,
        *,
        runtime_root: str,
        receipt_path: str,
    ) -> PublicationTargetObservation:
        ...

    def publish_no_replace(
        self,
        plan: ControllerRuntimeBuildPlan,
        *,
        runtime_root: str,
        receipt_path: str,
        runtime_build_receipt_json: bytes,
    ) -> PublicationObservation:
        ...

    def abandon_owned_stage(
        self, plan: ControllerRuntimeBuildPlan
    ) -> StageObservation:
        ...


def _is_hash(value: object) -> bool:
    return type(value) is str and _HASH_RE.fullmatch(value) is not None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ControllerRuntimeBuildError("controller_build_document_invalid")
        result[key] = value
    return result


def _json_document(
    raw: bytes,
    *,
    maximum_bytes: int,
    canonical: bool = False,
) -> dict[str, object]:
    if type(raw) is not bytes or not raw or len(raw) > maximum_bytes:
        raise ControllerRuntimeBuildError("controller_build_document_invalid")
    try:
        value = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda unused: (_ for _ in ()).throw(
                ControllerRuntimeBuildError("controller_build_document_invalid")
            ),
        )
    except ControllerRuntimeBuildError:
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ControllerRuntimeBuildError(
            "controller_build_document_invalid"
        ) from error
    if (
        type(value) is not dict
        or (canonical and canonical_json_bytes(value) != raw)
    ):
        raise ControllerRuntimeBuildError("controller_build_document_invalid")
    return value


def parse_standalone_cpython_substrate(
    raw: bytes,
) -> StandaloneCPythonSubstrate:
    document = _json_document(
        raw, maximum_bytes=MAX_DOCUMENT_BYTES, canonical=True
    )
    if (
        set(document) != _SUBSTRATE_KEYS
        or document.get("schema_version") != SUBSTRATE_SCHEMA
        or document.get("state") != SUBSTRATE_STATE
        or document.get("implementation") != "CPython"
        or type(document.get("python_version")) is not str
        or _VERSION_RE.fullmatch(str(document["python_version"])) is None
        or document.get("platform_os") != "linux"
        or document.get("platform_architecture") != "x86_64"
        or document.get("distribution_kind")
        != "standalone-cpython-install-only"
        or type(document.get("archive_name")) is not str
        or _ARCHIVE_NAME_RE.fullmatch(str(document["archive_name"])) is None
        or not _is_hash(document.get("archive_sha256"))
        or not _is_hash(document.get("payload_tree_sha256"))
        or document.get("payload_root") != "python/install"
        or document.get("interpreter_relative_path")
        != str(INTERPRETER_RELATIVE_PATH)
        or document.get(
            "archive_members_are_regular_files_or_directories_only"
        )
        is not True
        or document.get(
            "archive_contains_no_symlinks_hardlinks_or_special_files"
        )
        is not True
        or document.get("runtime_is_not_venv") is not True
        or type(document.get("network_calls")) is not int
        or document.get("network_calls") != 0
    ):
        raise ControllerRuntimeBuildError("controller_substrate_not_selected")
    specification_sha256 = hashlib.sha256(raw).hexdigest()
    archive_sha256 = str(document["archive_sha256"])
    return StandaloneCPythonSubstrate(
        specification_sha256=specification_sha256,
        python_version=str(document["python_version"]),
        archive_name=str(document["archive_name"]),
        archive_sha256=archive_sha256,
        payload_tree_sha256=str(document["payload_tree_sha256"]),
        archive_path=str(
            OFFLINE_SUBSTRATE_ROOT_PREFIX
            / archive_sha256
            / str(document["archive_name"])
        ),
    )


def _safe_release_path(value: object) -> str:
    if type(value) is not str:
        raise ControllerRuntimeBuildError("controller_release_closure_invalid")
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ControllerRuntimeBuildError("controller_release_closure_invalid")
    return value


def _release_closure(
    package_manifest_json: bytes,
    package_artifacts: Mapping[str, bytes],
) -> tuple[str, int, str]:
    manifest = _json_document(
        package_manifest_json, maximum_bytes=MAX_DOCUMENT_BYTES
    )
    indexed = manifest.get("artifacts")
    if (
        set(manifest) != {"schema_version", "state", "artifacts"}
        or type(manifest.get("schema_version")) is not str
        or not manifest["schema_version"]
        or type(manifest.get("state")) is not str
        or not manifest["state"]
        or type(indexed) is not dict
        or not indexed
        or len(indexed) > MAX_RELEASE_ARTIFACTS
        or type(package_artifacts) is not dict
        or set(package_artifacts) != set(indexed)
        or str(PACKAGE_MANIFEST_RELATIVE_PATH) in indexed
    ):
        raise ControllerRuntimeBuildError("controller_release_closure_invalid")
    total_bytes = 0
    closure: list[dict[str, object]] = []
    for candidate in sorted(indexed):
        path = _safe_release_path(candidate)
        expected = indexed[path]
        raw = package_artifacts.get(path)
        if (
            not _is_hash(expected)
            or type(raw) is not bytes
            or hashlib.sha256(raw).hexdigest() != expected
        ):
            raise ControllerRuntimeBuildError(
                "controller_release_closure_invalid"
            )
        total_bytes += len(raw)
        if total_bytes > MAX_RELEASE_BYTES:
            raise ControllerRuntimeBuildError(
                "controller_release_closure_invalid"
            )
        closure.append(
            {"path": path, "size": len(raw), "sha256": str(expected)}
        )
    manifest_sha256 = hashlib.sha256(package_manifest_json).hexdigest()
    closure_sha256 = hashlib.sha256(
        canonical_json_bytes(
            {
                "schema_version": "governed-memory-controller-release-closure-v1",
                "package_manifest_sha256": manifest_sha256,
                "package_manifest_size": len(package_manifest_json),
                "artifacts": closure,
            }
        )
    ).hexdigest()
    return manifest_sha256, len(closure), closure_sha256


def create_controller_runtime_build_plan(
    *,
    build_nonce: str,
    standalone_cpython_substrate_json: bytes,
    wheelhouse_tree_sha256: str,
    controller_runtime_contract_json: bytes,
    controller_requirements_lock: bytes,
    package_manifest_json: bytes,
    package_artifacts: Mapping[str, bytes],
) -> ControllerRuntimeBuildPlan:
    """Return an exact offline plan; no filesystem or process effect occurs."""

    if not _is_hash(build_nonce) or not _is_hash(wheelhouse_tree_sha256):
        raise ControllerRuntimeBuildError("controller_build_binding_invalid")
    substrate = parse_standalone_cpython_substrate(
        standalone_cpython_substrate_json
    )
    contract = _json_document(
        controller_runtime_contract_json, maximum_bytes=MAX_DOCUMENT_BYTES
    )
    if (
        contract.get("schema_version")
        != "governed-memory-dormant-store-install-controller-runtime-contract-v2"
        or contract.get("platform")
        != {
            "implementation": "CPython",
            "python_version": "3.12",
            "os": "linux",
            "architecture": "x86_64",
        }
    ):
        raise ControllerRuntimeBuildError(
            "controller_runtime_contract_invalid"
        )
    try:
        if (
            type(controller_requirements_lock) is not bytes
            or not controller_requirements_lock
            or len(controller_requirements_lock) > 64 * 1024
        ):
            raise ControllerRuntimeBuildError(
                "controller_requirements_lock_invalid"
            )
        required = _requirements_from_lock(controller_requirements_lock)
    except Exception as error:
        raise ControllerRuntimeBuildError(
            "controller_requirements_lock_invalid"
        ) from error
    package_sha256, artifact_count, closure_sha256 = _release_closure(
        package_manifest_json, package_artifacts
    )
    lock_path = str(REQUIREMENTS_LOCK_RELATIVE_PATH)
    contract_path = str(CONTROLLER_RUNTIME_CONTRACT_RELATIVE_PATH)
    launcher_path = str(LAUNCHER_RELATIVE_PATH)
    if (
        package_artifacts.get(lock_path) != controller_requirements_lock
        or package_artifacts.get(contract_path)
        != controller_runtime_contract_json
        or type(package_artifacts.get(launcher_path)) is not bytes
    ):
        raise ControllerRuntimeBuildError(
            "controller_build_package_binding_mismatch"
        )
    contract_sha256 = hashlib.sha256(controller_runtime_contract_json).hexdigest()
    lock_sha256 = hashlib.sha256(controller_requirements_lock).hexdigest()
    launcher_sha256 = hashlib.sha256(package_artifacts[launcher_path]).hexdigest()
    plan_material = {
        "schema_version": BUILD_PLAN_SCHEMA,
        "build_nonce": build_nonce,
        "package_manifest_sha256": package_sha256,
        "controller_runtime_contract_sha256": contract_sha256,
        "controller_requirements_lock_sha256": lock_sha256,
        "supervisor_launcher_sha256": launcher_sha256,
        "release_closure_sha256": closure_sha256,
        "standalone_cpython_specification_sha256": (
            substrate.specification_sha256
        ),
        "standalone_cpython_archive_sha256": substrate.archive_sha256,
        "standalone_cpython_payload_tree_sha256": substrate.payload_tree_sha256,
        "wheelhouse_tree_sha256": wheelhouse_tree_sha256,
    }
    plan_sha256 = hashlib.sha256(canonical_json_bytes(plan_material)).hexdigest()
    stage_root = STAGING_ROOT_PREFIX / plan_sha256
    operations = (
        "B01_observe_or_recover_exact_owned_stage",
        "B02_create_stage_exclusive_mode_0700_and_fsync_parent",
        "B03_extract_exact_standalone_cpython_without_links_or_special_files",
        "B04_verify_substrate_payload_tree_and_non_venv_interpreter",
        "B05_install_exact_lock_from_exact_offline_wheelhouse",
        "B06_remove_pip_setuptools_wheel_and_reject_inventory_drift",
        "B07_copy_exact_manifest_closed_release",
        "B08_probe_and_seal_roots_root_owned_mode_0555",
        "B09_construct_canonical_runtime_build_receipt",
        "B10_refuse_any_existing_final_or_receipt_path",
        "B11_atomic_rename_each_root_no_replace_and_fsync_parents",
        "B12_create_receipt_no_replace_mode_0400_and_fsync",
    )
    return ControllerRuntimeBuildPlan(
        schema_version=BUILD_PLAN_SCHEMA,
        build_plan_sha256=plan_sha256,
        build_nonce=build_nonce,
        package_manifest_sha256=package_sha256,
        controller_runtime_contract_sha256=contract_sha256,
        controller_requirements_lock_sha256=lock_sha256,
        release_artifact_count=artifact_count,
        release_closure_sha256=closure_sha256,
        supervisor_launcher_sha256=launcher_sha256,
        required_distributions=MappingProxyType(dict(sorted(required.items()))),
        substrate=substrate,
        wheelhouse_tree_sha256=wheelhouse_tree_sha256,
        wheelhouse_root=str(
            OFFLINE_WHEELHOUSE_ROOT_PREFIX / wheelhouse_tree_sha256
        ),
        stage_root=str(stage_root),
        staged_runtime_root=str(stage_root / "runtime"),
        staged_release_root=str(stage_root / "release"),
        final_release_root=str(RELEASE_ROOT_PREFIX / package_sha256),
        operations=operations,
    )


def _require_stage(
    observation: StageObservation,
    *,
    state: str,
    plan_sha256: str,
) -> None:
    if type(observation) is not StageObservation or observation.state != state:
        raise ControllerRuntimeBuildError("controller_build_stage_invalid")
    if state == "absent":
        if any(
            value is not None
            for value in (
                observation.build_plan_sha256,
                observation.root_mode,
                observation.root_uid,
                observation.root_gid,
                observation.marker_regular_no_follow,
                observation.created_no_replace,
                observation.parent_fsynced,
            )
        ):
            raise ControllerRuntimeBuildError("controller_build_stage_invalid")
    elif (
        observation.build_plan_sha256 != plan_sha256
        or observation.root_mode != 0o700
        or type(observation.root_uid) is not int
        or observation.root_uid != 0
        or type(observation.root_gid) is not int
        or observation.root_gid != 0
        or observation.marker_regular_no_follow is not True
        or observation.created_no_replace is not True
        or observation.parent_fsynced is not True
    ):
        raise ControllerRuntimeBuildError("controller_build_stage_invalid")


def _validate_build_observation(
    plan: ControllerRuntimeBuildPlan,
    observation: BuildObservation,
) -> None:
    hashes = (
        observation.runtime_tree_sha256,
        observation.release_tree_sha256,
        observation.interpreter_sha256,
        observation.installed_distribution_inventory_sha256,
        observation.interpreter_path_facts_sha256,
        observation.supervisor_launcher_sha256,
        observation.launcher_help_probe_sha256,
    )
    if (
        type(observation) is not BuildObservation
        or observation.result_type != BUILD_RESULT_TYPE
        or observation.build_plan_sha256 != plan.build_plan_sha256
        or observation.substrate_specification_sha256
        != plan.substrate.specification_sha256
        or observation.substrate_archive_sha256
        != plan.substrate.archive_sha256
        or observation.substrate_payload_tree_sha256
        != plan.substrate.payload_tree_sha256
        or observation.wheelhouse_tree_sha256 != plan.wheelhouse_tree_sha256
        or observation.release_closure_sha256 != plan.release_closure_sha256
        or type(observation.release_artifact_count) is not int
        or observation.release_artifact_count != plan.release_artifact_count
        or observation.supervisor_launcher_sha256
        != plan.supervisor_launcher_sha256
        or observation.python_implementation != "CPython"
        or observation.python_version != plan.substrate.python_version
        or observation.platform_os != "linux"
        or observation.platform_architecture != "x86_64"
        or not all(_is_hash(value) for value in hashes)
        or any(
            value is not False
            for value in (
                observation.pip_present,
                observation.setuptools_present,
                observation.wheel_present,
                observation.user_site_enabled,
                observation.system_site_packages_enabled,
                observation.normal_venv_created,
                observation.offline_substrate_regular_root_owned_nonwritable
                is not True,
                observation.offline_wheelhouse_regular_root_owned_nonwritable
                is not True,
                observation.production_data_read,
                observation.active_production_state_changed,
            )
        )
        or observation.runtime_root_mode != 0o555
        or observation.release_root_mode != 0o555
        or observation.all_members_root_owned is not True
        or any(
            type(value) is not int or value != 0
            for value in (
                observation.writable_member_count,
                observation.symlink_count,
                observation.hardlink_count,
                observation.special_file_count,
                observation.network_calls,
                observation.provider_calls,
            )
        )
    ):
        raise ControllerRuntimeBuildError(
            "controller_build_observation_invalid"
        )


def _runtime_receipt(
    plan: ControllerRuntimeBuildPlan,
    observation: BuildObservation,
) -> bytes:
    receipt: dict[str, object] = {
        "schema_version": RUNTIME_RECEIPT_SCHEMA,
        "result": RUNTIME_RECEIPT_RESULT,
        "package_manifest_sha256": plan.package_manifest_sha256,
        "controller_runtime_contract_sha256": (
            plan.controller_runtime_contract_sha256
        ),
        "controller_requirements_lock_sha256": (
            plan.controller_requirements_lock_sha256
        ),
        "python_implementation": observation.python_implementation,
        "python_version": observation.python_version,
        "platform_os": observation.platform_os,
        "platform_architecture": observation.platform_architecture,
        "runtime_tree_sha256": observation.runtime_tree_sha256,
        "release_tree_sha256": observation.release_tree_sha256,
        "interpreter_sha256": observation.interpreter_sha256,
        "installed_distribution_inventory_sha256": (
            observation.installed_distribution_inventory_sha256
        ),
        "interpreter_path_facts_sha256": (
            observation.interpreter_path_facts_sha256
        ),
        "supervisor_launcher_sha256": observation.supervisor_launcher_sha256,
        "launcher_help_probe_sha256": observation.launcher_help_probe_sha256,
        "pip_present": False,
        "setuptools_present": False,
        "wheel_present": False,
        "user_site_enabled": False,
        "system_site_packages_enabled": False,
        "network_calls": 0,
        "provider_calls": 0,
        "production_data_read": False,
        "active_production_state_changed": False,
        "persistent_controller_substrate_created": True,
        "persistent_store_resources_created": False,
    }
    if set(receipt) != _RECEIPT_KEYS:
        raise ControllerRuntimeBuildError("controller_runtime_receipt_invalid")
    return canonical_json_bytes(receipt)


def _abandon_stage_or_refuse(
    transport: ControllerRuntimeBuildTransport,
    plan: ControllerRuntimeBuildPlan,
) -> None:
    try:
        abandoned = transport.abandon_owned_stage(plan)
        _require_stage(
            abandoned, state="absent", plan_sha256=plan.build_plan_sha256
        )
    except Exception as error:
        raise ControllerRuntimeBuildError(
            "controller_build_partial_stage_requires_review"
        ) from error


def execute_controller_runtime_build(
    plan: ControllerRuntimeBuildPlan,
    *,
    package_manifest_json: bytes,
    package_artifacts: Mapping[str, bytes],
    transport: ControllerRuntimeBuildTransport,
) -> ControllerRuntimeBuildResult:
    """Run the typed plan through an injected, separately authorized transport."""

    if type(plan) is not ControllerRuntimeBuildPlan:
        raise ControllerRuntimeBuildError("controller_build_plan_invalid")
    if type(package_artifacts) is not dict:
        raise ControllerRuntimeBuildError("controller_release_closure_invalid")
    artifact_snapshot = dict(package_artifacts)
    package_sha, count, closure_sha = _release_closure(
        package_manifest_json, artifact_snapshot
    )
    if (
        package_sha != plan.package_manifest_sha256
        or count != plan.release_artifact_count
        or closure_sha != plan.release_closure_sha256
    ):
        raise ControllerRuntimeBuildError("controller_release_closure_changed")
    stage_created = False
    publication_started = False
    try:
        stage = transport.observe_stage(plan)
        if (
            type(stage) is StageObservation
            and stage.state == "owned_partial"
            and stage.build_plan_sha256 == plan.build_plan_sha256
        ):
            _require_stage(
                stage,
                state="owned_partial",
                plan_sha256=plan.build_plan_sha256,
            )
            recovered = transport.recover_owned_partial_stage(plan)
            _require_stage(
                recovered, state="absent", plan_sha256=plan.build_plan_sha256
            )
            stage = recovered
        _require_stage(stage, state="absent", plan_sha256=plan.build_plan_sha256)
        created = transport.create_stage(plan)
        _require_stage(
            created, state="owned_ready", plan_sha256=plan.build_plan_sha256
        )
        stage_created = True
        transport.materialize_standalone_substrate(plan)
        transport.install_locked_offline_distributions(plan)
        transport.remove_bootstrap_packaging_tools(plan)
        transport.stage_exact_release(
            plan,
            package_manifest_json=package_manifest_json,
            package_artifacts=artifact_snapshot,
        )
        observation = transport.seal_and_observe(plan)
        _validate_build_observation(plan, observation)
        receipt_raw = _runtime_receipt(plan, observation)
        receipt_sha256 = hashlib.sha256(receipt_raw).hexdigest()
        runtime_root = str(RUNTIME_ROOT_PREFIX / receipt_sha256)
        receipt_path = str(
            RUNTIME_RECEIPT_ROOT_PREFIX / f"{receipt_sha256}.json"
        )
        targets = transport.observe_publication_targets(
            plan, runtime_root=runtime_root, receipt_path=receipt_path
        )
        if (
            type(targets) is not PublicationTargetObservation
            or targets
            != PublicationTargetObservation(
                "absent", "absent", "absent", True
            )
        ):
            raise ControllerRuntimeBuildError(
                "controller_build_final_path_exists_or_partial"
            )
        publication_started = True
        publication = transport.publish_no_replace(
            plan,
            runtime_root=runtime_root,
            receipt_path=receipt_path,
            runtime_build_receipt_json=receipt_raw,
        )
        if (
            type(publication) is not PublicationObservation
            or publication.result_type != PUBLICATION_RESULT_TYPE
            or publication.runtime_root != runtime_root
            or publication.release_root != plan.final_release_root
            or publication.receipt_path != receipt_path
            or publication.runtime_rename_no_replace is not True
            or publication.release_rename_no_replace is not True
            or publication.receipt_create_no_replace is not True
            or publication.runtime_root_mode != 0o555
            or publication.release_root_mode != 0o555
            or publication.receipt_mode != 0o400
            or type(publication.root_uid) is not int
            or publication.root_uid != 0
            or type(publication.root_gid) is not int
            or publication.root_gid != 0
            or publication.runtime_parent_fsynced is not True
            or publication.release_parent_fsynced is not True
            or publication.receipt_file_fsynced is not True
            or publication.receipt_parent_fsynced is not True
            or publication.stage_absent is not True
        ):
            raise ControllerRuntimeBuildError(
                "controller_build_publication_invalid"
            )
        return ControllerRuntimeBuildResult(
            build_plan_sha256=plan.build_plan_sha256,
            runtime_build_receipt_json=receipt_raw,
            controller_runtime_receipt_sha256=receipt_sha256,
            runtime_root=runtime_root,
            release_root=plan.final_release_root,
            receipt_path=receipt_path,
            runtime_tree_sha256=observation.runtime_tree_sha256,
            release_tree_sha256=observation.release_tree_sha256,
        )
    except ControllerRuntimeBuildError:
        if stage_created and not publication_started:
            _abandon_stage_or_refuse(transport, plan)
        raise
    except Exception as error:
        if publication_started:
            raise ControllerRuntimeBuildError(
                "controller_build_partial_publication_requires_review"
            ) from error
        if stage_created:
            _abandon_stage_or_refuse(transport, plan)
        raise ControllerRuntimeBuildError(
            "controller_build_transport_failed"
        ) from error


__all__ = [
    "BUILD_RESULT_TYPE",
    "ControllerRuntimeBuildError",
    "ControllerRuntimeBuildPlan",
    "ControllerRuntimeBuildResult",
    "ControllerRuntimeBuildTransport",
    "BuildObservation",
    "PublicationObservation",
    "PublicationTargetObservation",
    "StageObservation",
    "StandaloneCPythonSubstrate",
    "create_controller_runtime_build_plan",
    "execute_controller_runtime_build",
    "parse_standalone_cpython_substrate",
]
