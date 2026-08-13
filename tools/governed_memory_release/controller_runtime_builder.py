from __future__ import annotations

"""Fail-closed planner for the immutable controller runtime and release.

The planner keeps path derivation, exact release closure, receipt construction,
create-only publication, and recovery/refusal policy inside reviewed controller
code. Effects remain in a separately constructed closed transport. The selected
standalone archive may contain only safe relative in-payload links; those links
are deterministically expanded and independently hashed before dependency
installation.
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
    EXPECTED_POSTGRESQL_DRIVER_IDENTITY_SHA256,
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
    "governed-memory-controller-standalone-cpython-substrate-v2"
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
MAX_WHEELHOUSE_MEMBERS: Final = 64
MAX_WHEELHOUSE_MEMBER_BYTES: Final = 64 * 1024 * 1024
MAX_WHEELHOUSE_BYTES: Final = 256 * 1024 * 1024

SELECTED_CPYTHON_VERSION: Final = "3.12.13"
SELECTED_CPYTHON_ARCHIVE_NAME: Final = (
    "cpython-3.12.13+20260807-x86_64-unknown-linux-gnu-"
    "install_only_stripped.tar.gz"
)
SELECTED_CPYTHON_ARCHIVE_SHA256: Final = (
    "506191be3ee7bd190a8834dcdc1b3bc70aab50608deccc711935aa007239cabd"
)
WHEELHOUSE_TREE_SCHEMA: Final = (
    "governed-memory-controller-offline-wheelhouse-tree-v1"
)
POSTGRESQL_DRIVER_DISTRIBUTIONS: Final = frozenset(
    {"psycopg", "psycopg-binary"}
)
_RUNTIME_BUILD_OPERATIONS: Final = (
    "B01_observe_or_recover_exact_owned_stage",
    "B02_create_stage_exclusive_mode_0700_and_fsync_parent",
    "B03_expand_safe_archive_links_to_exact_regular_payload",
    "B04_verify_substrate_payload_tree_and_non_venv_interpreter",
    "B05_recompute_canonical_wheelhouse_membership_and_install_exact_lock",
    "B06_remove_pip_setuptools_wheel_and_reject_inventory_drift",
    "B07_copy_exact_manifest_closed_release",
    "B08_probe_and_seal_roots_root_owned_mode_0555",
    "B09_construct_canonical_runtime_build_receipt",
    "B10_refuse_any_existing_final_or_receipt_path",
    "B11_verify_exact_cleanup_targets_and_same_device_rename_preconditions",
    "B12_create_exact_publication_intent_no_replace_and_fsync",
    "B13_atomic_rename_each_root_no_replace_and_fsync_parents",
    "B14_unlink_exact_stage_metadata_and_remove_only_empty_stage_root",
    "B15_create_receipt_fsync_and_observe_exact_terminal_publication",
)

_HASH_RE: Final = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_WHEEL_FILENAME_RE: Final = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._+-]{0,239}\.whl\Z", re.ASCII
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
        "payload_root",
        "interpreter_relative_path",
        "archive_member_policy",
        "archive_hardlinks_or_special_files_present",
        "archive_symlink_count",
        "archive_symlinks_absolute_escape_dangling_or_cyclic",
        "symlink_expansion_policy",
        "symlink_expansion_mapping_sha256",
        "expanded_payload_tree_schema",
        "expanded_payload_tree_sha256",
        "expanded_payload_is_symlink_hardlink_special_free",
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
class WheelhouseMember:
    distribution: str
    version: str
    filename: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ExactOfflineWheelhouse:
    schema_version: str
    tree_sha256: str
    member_count: int
    total_bytes: int
    members: tuple[WheelhouseMember, ...]


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
    wheelhouse: ExactOfflineWheelhouse
    wheelhouse_root: str
    stage_root: str
    staged_runtime_root: str
    staged_release_root: str
    final_release_root: str
    operations: tuple[str, ...]

    @property
    def wheelhouse_tree_sha256(self) -> str:
        return self.wheelhouse.tree_sha256


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
    postgresql_driver_identity_sha256: str
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
    publication_intent_retained: bool
    terminal_state_observed: bool
    same_device_rename_preconditions_observed: bool


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
    """Closed effects required by a separately authorized execution."""

    def observe_stage(self, plan: ControllerRuntimeBuildPlan) -> StageObservation:
        ...

    def recover_completed_publication(
        self, plan: ControllerRuntimeBuildPlan
    ) -> ControllerRuntimeBuildResult:
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
        self,
        plan: ControllerRuntimeBuildPlan,
        *,
        controller_requirements_lock: bytes,
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
        or document.get("python_version") != SELECTED_CPYTHON_VERSION
        or document.get("platform_os") != "linux"
        or document.get("platform_architecture") != "x86_64"
        or document.get("distribution_kind")
        != "standalone-cpython-install-only"
        or document.get("archive_name") != SELECTED_CPYTHON_ARCHIVE_NAME
        or document.get("archive_sha256")
        != SELECTED_CPYTHON_ARCHIVE_SHA256
        or document.get("payload_root") != "python"
        or document.get("interpreter_relative_path")
        != str(INTERPRETER_RELATIVE_PATH)
        or document.get("archive_member_policy")
        != "directories-regular-files-and-relative-in-payload-symlinks-only"
        or document.get("archive_hardlinks_or_special_files_present") is not False
        or type(document.get("archive_symlink_count")) is not int
        or document.get("archive_symlink_count") != 1048
        or document.get(
            "archive_symlinks_absolute_escape_dangling_or_cyclic"
        )
        is not False
        or document.get("symlink_expansion_policy")
        != (
            "relative-in-payload-links-expanded-to-independent-regular-file-"
            "or-directory-copies"
        )
        or not _is_hash(document.get("symlink_expansion_mapping_sha256"))
        or document.get("expanded_payload_tree_schema")
        != "governed-memory-standalone-cpython-expanded-payload-tree-v1"
        or not _is_hash(document.get("expanded_payload_tree_sha256"))
        or document.get("expanded_payload_is_symlink_hardlink_special_free")
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
        payload_tree_sha256=str(document["expanded_payload_tree_sha256"]),
        archive_path=str(
            OFFLINE_SUBSTRATE_ROOT_PREFIX
            / archive_sha256
            / str(document["archive_name"])
        ),
    )


def _normalized_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _require_selected_substrate(
    contract: Mapping[str, object], substrate: StandaloneCPythonSubstrate
) -> None:
    selected = contract.get("selected_runtime_inputs")
    build_policy = contract.get("build_policy")
    cpython = (
        selected.get("standalone_cpython")
        if type(selected) is dict
        else None
    )
    if (
        type(substrate) is not StandaloneCPythonSubstrate
        or type(cpython) is not dict
        or type(build_policy) is not dict
        or cpython.get("implementation") != "CPython"
        or cpython.get("python_version") != substrate.python_version
        or cpython.get("distribution_kind")
        != "standalone-cpython-install-only"
        or cpython.get("archive_name") != substrate.archive_name
        or cpython.get("archive_sha256") != substrate.archive_sha256
        or cpython.get("payload_root") != "python"
        or cpython.get("interpreter_relative_path")
        != str(INTERPRETER_RELATIVE_PATH)
        or cpython.get("archive_staged") is not True
        or cpython.get("archive_bytes_sha256_verified_locally") is not True
        or cpython.get("archive_member_types_verified") is not True
        or cpython.get("specification_sha256")
        != substrate.specification_sha256
        or cpython.get("expanded_payload_tree_sha256")
        != substrate.payload_tree_sha256
        or cpython.get("archive_symlink_count") != 1048
        or cpython.get("archive_symlink_normalization_verified") is not True
        or build_policy.get("approved_standalone_cpython_substrate_selected")
        is not True
        or build_policy.get("standalone_cpython_archive_staged") is not True
        or build_policy.get("standalone_cpython_archive_content_verified")
        is not True
    ):
        raise ControllerRuntimeBuildError(
            "controller_runtime_substrate_not_ready"
        )


def _selected_postgresql_driver(
    contract: Mapping[str, object],
) -> dict[str, tuple[str, str, str]]:
    """Return the exact ready driver selection closed by the contract.

    A preferred-but-not-yet-locked selection is audit metadata, not a runtime
    build input.  In particular, the repository's current Phase 9H contract
    must remain unbuildable until a later authorized artifact-staging phase
    closes both selected wheels and their native-library evidence.
    """

    selected = contract.get("selected_runtime_inputs")
    build_policy = contract.get("build_policy")
    dependency_policy = contract.get("dependency_policy")
    if (
        type(selected) is not dict
        or type(build_policy) is not dict
        or type(dependency_policy) is not dict
    ):
        raise ControllerRuntimeBuildError(
            "controller_runtime_postgresql_driver_not_ready"
        )
    driver = selected.get("postgresql_driver")
    preferences = (
        driver.get("preferred_distributions")
        if type(driver) is dict
        else None
    )
    selected_wheels = (
        driver.get("selected_wheels") if type(driver) is dict else None
    )
    if (
        type(driver) is not dict
        or driver.get("api_style") != "synchronous"
        or driver.get("preferred_extra") != "binary"
        or driver.get("selection_state")
        != "exact-selected-wheels-staged-verified-and-locked"
        or driver.get("asyncpg_is_controller_driver") is not False
        or driver.get("current_controller_lock_contains_selection") is not True
        or driver.get("wheel_bytes_staged") is not True
        or driver.get("wheel_bytes_sha256_verified_locally") is not True
        or driver.get("binary_native_library_closure_inspected") is not True
        or driver.get("exact_wheel_filenames_frozen") is not True
        or driver.get("driver_native_postgresql_stages_packaged") is not True
        or driver.get("selection_ready_for_runtime_build") is not True
        or build_policy.get("preferred_postgresql_driver_locked") is not True
        or build_policy.get("offline_wheelhouse_staged") is not True
        or build_policy.get("canonical_wheelhouse_identity_present") is not True
        or dependency_policy.get(
            "current_lock_contains_preferred_postgresql_driver"
        )
        is not True
        or dependency_policy.get(
            "preferred_postgresql_driver_must_be_locked_before_runtime_build"
        )
        is not True
        or type(preferences) is not list
        or len(preferences) != len(POSTGRESQL_DRIVER_DISTRIBUTIONS)
        or type(selected_wheels) is not list
        or len(selected_wheels) != len(POSTGRESQL_DRIVER_DISTRIBUTIONS)
    ):
        raise ControllerRuntimeBuildError(
            "controller_runtime_postgresql_driver_not_ready"
        )

    preferred_versions: dict[str, str] = {}
    for item in preferences:
        if (
            type(item) is not dict
            or set(item) != {"normalized_distribution", "version"}
            or type(item.get("normalized_distribution")) is not str
            or _normalized_distribution_name(
                str(item["normalized_distribution"])
            )
            != item["normalized_distribution"]
            or type(item.get("version")) is not str
            or not item["version"]
            or item["normalized_distribution"] in preferred_versions
        ):
            raise ControllerRuntimeBuildError(
                "controller_runtime_postgresql_driver_not_ready"
            )
        preferred_versions[str(item["normalized_distribution"])] = str(
            item["version"]
        )
    if (
        preferred_versions
        != {"psycopg": "3.3.4", "psycopg-binary": "3.3.4"}
    ):
        raise ControllerRuntimeBuildError(
            "controller_runtime_postgresql_driver_not_ready"
        )

    result: dict[str, tuple[str, str, str]] = {}
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
            or _normalized_distribution_name(
                str(item["normalized_distribution"])
            )
            != item["normalized_distribution"]
            or type(item.get("version")) is not str
            or not item["version"]
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
            or item["normalized_distribution"] in result
        ):
            raise ControllerRuntimeBuildError(
                "controller_runtime_postgresql_driver_not_ready"
            )
        result[str(item["normalized_distribution"])] = (
            str(item["version"]),
            str(item["selected_wheel_sha256"]),
            str(item["selected_wheel_filename"]),
        )
    if set(result) != POSTGRESQL_DRIVER_DISTRIBUTIONS:
        raise ControllerRuntimeBuildError(
            "controller_runtime_postgresql_driver_not_ready"
        )
    return dict(sorted(result.items()))


def _require_selected_wheelhouse(
    contract: Mapping[str, object],
    *,
    required: Mapping[str, str],
    locked_hashes: Mapping[str, frozenset[str]],
    wheelhouse: ExactOfflineWheelhouse,
    selected_driver: Mapping[str, tuple[str, str, str]],
) -> None:
    selected_inputs = contract.get("selected_runtime_inputs")
    wheelhouse_contract = (
        selected_inputs.get("wheelhouse")
        if type(selected_inputs) is dict
        else None
    )
    if (
        type(wheelhouse_contract) is not dict
        or wheelhouse_contract.get("wheelhouse_staged") is not True
        or wheelhouse_contract.get("canonical_tree_sha256")
        != wheelhouse.tree_sha256
        or type(wheelhouse_contract.get("canonical_member_count")) is not int
        or wheelhouse_contract.get("canonical_member_count")
        != wheelhouse.member_count
        or type(wheelhouse_contract.get("canonical_total_bytes")) is not int
        or wheelhouse_contract.get("canonical_total_bytes")
        != wheelhouse.total_bytes
    ):
        raise ControllerRuntimeBuildError(
            "controller_runtime_wheelhouse_not_ready"
        )

    observed = {
        member.distribution: (
            member.version,
            member.sha256,
            member.filename,
        )
        for member in wheelhouse.members
        if member.distribution in POSTGRESQL_DRIVER_DISTRIBUTIONS
    }
    for distribution, (version, digest, filename) in selected_driver.items():
        if (
            required.get(distribution) != version
            or digest not in locked_hashes.get(distribution, frozenset())
            or observed.get(distribution) != (version, digest, filename)
        ):
            raise ControllerRuntimeBuildError(
                "controller_runtime_postgresql_driver_not_ready"
            )


def _requirement_hashes_from_lock(
    raw: bytes,
    required: Mapping[str, str],
) -> dict[str, frozenset[str]]:
    """Return the exact allowed hashes already validated by the narrow lock."""

    try:
        text = raw.decode("ascii")
    except UnicodeError:
        raise ControllerRuntimeBuildError(
            "controller_requirements_lock_invalid"
        ) from None
    logical_lines: list[str] = []
    pending = ""
    for physical in text.splitlines():
        stripped = physical.strip()
        if not stripped or stripped.startswith("#"):
            if pending:
                raise ControllerRuntimeBuildError(
                    "controller_requirements_lock_invalid"
                )
            continue
        continued = stripped.endswith("\\")
        fragment = stripped[:-1].rstrip() if continued else stripped
        if not fragment:
            raise ControllerRuntimeBuildError(
                "controller_requirements_lock_invalid"
            )
        pending = f"{pending} {fragment}".strip()
        if not continued:
            logical_lines.append(pending)
            pending = ""
    if pending or not logical_lines:
        raise ControllerRuntimeBuildError(
            "controller_requirements_lock_invalid"
        )

    head_pattern = re.compile(
        r"([A-Za-z0-9][A-Za-z0-9._-]*)=="
        r"([A-Za-z0-9][A-Za-z0-9.+!_-]*)\Z",
        re.ASCII,
    )
    hash_pattern = re.compile(r"--hash=sha256:([0-9a-f]{64})\Z", re.ASCII)
    result: dict[str, frozenset[str]] = {}
    for logical in logical_lines:
        tokens = logical.split()
        match = head_pattern.fullmatch(tokens[0]) if tokens else None
        hash_matches = [hash_pattern.fullmatch(token) for token in tokens[1:]]
        if match is None or not hash_matches or any(
            item is None for item in hash_matches
        ):
            raise ControllerRuntimeBuildError(
                "controller_requirements_lock_invalid"
            )
        distribution = _normalized_distribution_name(match.group(1))
        hashes = frozenset(
            item.group(1) for item in hash_matches if item is not None
        )
        if (
            distribution in result
            or required.get(distribution) != match.group(2)
            or len(hashes) != len(hash_matches)
        ):
            raise ControllerRuntimeBuildError(
                "controller_requirements_lock_invalid"
            )
        result[distribution] = hashes
    if set(result) != set(required):
        raise ControllerRuntimeBuildError(
            "controller_requirements_lock_invalid"
        )
    return dict(sorted(result.items()))


def canonical_wheelhouse_identity(
    *,
    wheelhouse_artifacts: Mapping[str, bytes],
    controller_requirements_lock: bytes,
) -> ExactOfflineWheelhouse:
    """Close exact wheel bytes into one deterministic, lock-bound identity.

    The digest covers an ASCII canonical-JSON member document, not directory
    enumeration order, mtimes, ownership, or filesystem modes.  The later
    transport must separately prove the root-owned non-writable directory and
    recompute this same member closure from regular no-follow files.
    """

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
    except ControllerRuntimeBuildError:
        raise
    except Exception:
        raise ControllerRuntimeBuildError(
            "controller_requirements_lock_invalid"
        ) from None
    locked_hashes = _requirement_hashes_from_lock(
        controller_requirements_lock, required
    )
    hash_bindings: dict[str, tuple[str, str]] = {}
    for distribution, hashes in locked_hashes.items():
        for digest in hashes:
            if digest in hash_bindings:
                raise ControllerRuntimeBuildError(
                    "controller_wheelhouse_invalid"
                )
            hash_bindings[digest] = (distribution, required[distribution])

    if (
        type(wheelhouse_artifacts) is not dict
        or not wheelhouse_artifacts
        or len(wheelhouse_artifacts) > MAX_WHEELHOUSE_MEMBERS
    ):
        raise ControllerRuntimeBuildError("controller_wheelhouse_invalid")
    artifact_snapshot = dict(wheelhouse_artifacts)
    if any(type(filename) is not str for filename in artifact_snapshot):
        raise ControllerRuntimeBuildError("controller_wheelhouse_invalid")
    members: list[WheelhouseMember] = []
    seen_distributions: set[str] = set()
    total_bytes = 0
    for filename in sorted(artifact_snapshot):
        raw = artifact_snapshot[filename]
        if (
            type(filename) is not str
            or _WHEEL_FILENAME_RE.fullmatch(filename) is None
            or PurePosixPath(filename).name != filename
            or type(raw) is not bytes
            or not raw
            or len(raw) > MAX_WHEELHOUSE_MEMBER_BYTES
        ):
            raise ControllerRuntimeBuildError("controller_wheelhouse_invalid")
        digest = hashlib.sha256(raw).hexdigest()
        binding = hash_bindings.get(digest)
        if binding is None:
            raise ControllerRuntimeBuildError("controller_wheelhouse_invalid")
        distribution, version = binding
        encoded_distribution = re.sub(r"[-_.]+", "_", distribution)
        if (
            distribution in seen_distributions
            or not filename.startswith(
                f"{encoded_distribution}-{version}-"
            )
        ):
            raise ControllerRuntimeBuildError("controller_wheelhouse_invalid")
        seen_distributions.add(distribution)
        total_bytes += len(raw)
        if total_bytes > MAX_WHEELHOUSE_BYTES:
            raise ControllerRuntimeBuildError("controller_wheelhouse_invalid")
        members.append(
            WheelhouseMember(
                distribution=distribution,
                version=version,
                filename=filename,
                size=len(raw),
                sha256=digest,
            )
        )
    if seen_distributions != set(required):
        raise ControllerRuntimeBuildError("controller_wheelhouse_invalid")

    ordered = tuple(
        sorted(members, key=lambda item: (item.distribution, item.filename))
    )
    material = {
        "schema_version": WHEELHOUSE_TREE_SCHEMA,
        "platform": {
            "architecture": "x86_64",
            "os": "linux",
            "python_tag": "cp312",
        },
        "members": [
            {
                "distribution": member.distribution,
                "filename": member.filename,
                "sha256": member.sha256,
                "size": member.size,
                "version": member.version,
            }
            for member in ordered
        ],
    }
    return ExactOfflineWheelhouse(
        schema_version=WHEELHOUSE_TREE_SCHEMA,
        tree_sha256=hashlib.sha256(canonical_json_bytes(material)).hexdigest(),
        member_count=len(ordered),
        total_bytes=total_bytes,
        members=ordered,
    )


def _validate_wheelhouse_identity(
    wheelhouse: ExactOfflineWheelhouse,
    *,
    required: Mapping[str, str],
    locked_hashes: Mapping[str, frozenset[str]],
) -> None:
    if (
        type(wheelhouse) is not ExactOfflineWheelhouse
        or wheelhouse.schema_version != WHEELHOUSE_TREE_SCHEMA
        or type(wheelhouse.members) is not tuple
        or not wheelhouse.members
        or type(wheelhouse.member_count) is not int
        or wheelhouse.member_count != len(wheelhouse.members)
        or type(wheelhouse.total_bytes) is not int
        or wheelhouse.total_bytes <= 0
        or not _is_hash(wheelhouse.tree_sha256)
    ):
        raise ControllerRuntimeBuildError("controller_wheelhouse_invalid")
    seen: set[str] = set()
    total_bytes = 0
    for member in wheelhouse.members:
        if (
            type(member) is not WheelhouseMember
            or type(member.distribution) is not str
            or member.distribution in seen
            or required.get(member.distribution) != member.version
            or type(member.filename) is not str
            or _WHEEL_FILENAME_RE.fullmatch(member.filename) is None
            or PurePosixPath(member.filename).name != member.filename
            or not member.filename.startswith(
                f"{re.sub(r'[-_.]+', '_', member.distribution)}-"
                f"{member.version}-"
            )
            or type(member.size) is not int
            or not 1 <= member.size <= MAX_WHEELHOUSE_MEMBER_BYTES
            or not _is_hash(member.sha256)
            or member.sha256
            not in locked_hashes.get(member.distribution, frozenset())
        ):
            raise ControllerRuntimeBuildError("controller_wheelhouse_invalid")
        seen.add(member.distribution)
        total_bytes += member.size
    ordered = tuple(
        sorted(
            wheelhouse.members,
            key=lambda item: (item.distribution, item.filename),
        )
    )
    material = {
        "schema_version": WHEELHOUSE_TREE_SCHEMA,
        "platform": {
            "architecture": "x86_64",
            "os": "linux",
            "python_tag": "cp312",
        },
        "members": [
            {
                "distribution": member.distribution,
                "filename": member.filename,
                "sha256": member.sha256,
                "size": member.size,
                "version": member.version,
            }
            for member in ordered
        ],
    }
    if (
        wheelhouse.members != ordered
        or seen != set(required)
        or total_bytes != wheelhouse.total_bytes
        or total_bytes > MAX_WHEELHOUSE_BYTES
        or hashlib.sha256(canonical_json_bytes(material)).hexdigest()
        != wheelhouse.tree_sha256
    ):
        raise ControllerRuntimeBuildError("controller_wheelhouse_invalid")


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


def _build_plan_material(
    *,
    build_nonce: str,
    package_manifest_sha256: str,
    controller_runtime_contract_sha256: str,
    controller_requirements_lock_sha256: str,
    supervisor_launcher_sha256: str,
    release_closure_sha256: str,
    substrate: StandaloneCPythonSubstrate,
    wheelhouse: ExactOfflineWheelhouse,
) -> dict[str, object]:
    return {
        "schema_version": BUILD_PLAN_SCHEMA,
        "build_nonce": build_nonce,
        "package_manifest_sha256": package_manifest_sha256,
        "controller_runtime_contract_sha256": (
            controller_runtime_contract_sha256
        ),
        "controller_requirements_lock_sha256": (
            controller_requirements_lock_sha256
        ),
        "supervisor_launcher_sha256": supervisor_launcher_sha256,
        "release_closure_sha256": release_closure_sha256,
        "standalone_cpython_specification_sha256": (
            substrate.specification_sha256
        ),
        "standalone_cpython_archive_sha256": substrate.archive_sha256,
        "standalone_cpython_payload_tree_sha256": (
            substrate.payload_tree_sha256
        ),
        "wheelhouse_tree_sha256": wheelhouse.tree_sha256,
        "wheelhouse_member_count": wheelhouse.member_count,
        "wheelhouse_total_bytes": wheelhouse.total_bytes,
    }


def create_controller_runtime_build_plan(
    *,
    build_nonce: str,
    standalone_cpython_substrate_json: bytes,
    wheelhouse_artifacts: Mapping[str, bytes],
    controller_runtime_contract_json: bytes,
    controller_requirements_lock: bytes,
    package_manifest_json: bytes,
    package_artifacts: Mapping[str, bytes],
) -> ControllerRuntimeBuildPlan:
    """Return an exact offline plan; no filesystem or process effect occurs."""

    if not _is_hash(build_nonce):
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
    _require_selected_substrate(contract, substrate)
    selected_driver = _selected_postgresql_driver(contract)
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
    except Exception:
        raise ControllerRuntimeBuildError(
            "controller_requirements_lock_invalid"
        ) from None
    locked_hashes = _requirement_hashes_from_lock(
        controller_requirements_lock, required
    )
    wheelhouse = canonical_wheelhouse_identity(
        wheelhouse_artifacts=wheelhouse_artifacts,
        controller_requirements_lock=controller_requirements_lock,
    )
    _validate_wheelhouse_identity(
        wheelhouse, required=required, locked_hashes=locked_hashes
    )
    _require_selected_wheelhouse(
        contract,
        required=required,
        locked_hashes=locked_hashes,
        wheelhouse=wheelhouse,
        selected_driver=selected_driver,
    )
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
    plan_material = _build_plan_material(
        build_nonce=build_nonce,
        package_manifest_sha256=package_sha256,
        controller_runtime_contract_sha256=contract_sha256,
        controller_requirements_lock_sha256=lock_sha256,
        supervisor_launcher_sha256=launcher_sha256,
        release_closure_sha256=closure_sha256,
        substrate=substrate,
        wheelhouse=wheelhouse,
    )
    plan_sha256 = hashlib.sha256(canonical_json_bytes(plan_material)).hexdigest()
    stage_root = STAGING_ROOT_PREFIX / plan_sha256
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
        wheelhouse=wheelhouse,
        wheelhouse_root=str(
            OFFLINE_WHEELHOUSE_ROOT_PREFIX / wheelhouse.tree_sha256
        ),
        stage_root=str(stage_root),
        staged_runtime_root=str(stage_root / "runtime"),
        staged_release_root=str(stage_root / "release"),
        final_release_root=str(RELEASE_ROOT_PREFIX / package_sha256),
        operations=_RUNTIME_BUILD_OPERATIONS,
    )


def _validate_controller_runtime_build_plan(
    plan: ControllerRuntimeBuildPlan,
    *,
    standalone_cpython_substrate_json: bytes,
    package_manifest_json: bytes,
    package_artifacts: Mapping[str, bytes],
) -> dict[str, bytes]:
    """Recompute every authority-bearing plan field before any effect call."""

    if type(plan) is not ControllerRuntimeBuildPlan or type(package_artifacts) is not dict:
        raise ControllerRuntimeBuildError("controller_build_plan_invalid")
    artifacts = dict(package_artifacts)
    package_sha256, artifact_count, closure_sha256 = _release_closure(
        package_manifest_json, artifacts
    )
    lock_path = str(REQUIREMENTS_LOCK_RELATIVE_PATH)
    contract_path = str(CONTROLLER_RUNTIME_CONTRACT_RELATIVE_PATH)
    launcher_path = str(LAUNCHER_RELATIVE_PATH)
    lock = artifacts.get(lock_path)
    contract_raw = artifacts.get(contract_path)
    launcher = artifacts.get(launcher_path)
    if (
        type(lock) is not bytes
        or type(contract_raw) is not bytes
        or type(launcher) is not bytes
    ):
        raise ControllerRuntimeBuildError(
            "controller_build_package_binding_mismatch"
        )
    selected_substrate = parse_standalone_cpython_substrate(
        standalone_cpython_substrate_json
    )
    contract = _json_document(
        contract_raw, maximum_bytes=MAX_DOCUMENT_BYTES
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
        or type(plan.substrate) is not StandaloneCPythonSubstrate
        or plan.substrate != selected_substrate
        or plan.substrate.python_version != SELECTED_CPYTHON_VERSION
        or plan.substrate.archive_name != SELECTED_CPYTHON_ARCHIVE_NAME
        or plan.substrate.archive_sha256 != SELECTED_CPYTHON_ARCHIVE_SHA256
        or not _is_hash(plan.substrate.payload_tree_sha256)
        or plan.substrate.archive_path
        != str(
            OFFLINE_SUBSTRATE_ROOT_PREFIX
            / SELECTED_CPYTHON_ARCHIVE_SHA256
            / SELECTED_CPYTHON_ARCHIVE_NAME
        )
    ):
        raise ControllerRuntimeBuildError("controller_build_plan_invalid")
    _require_selected_substrate(contract, plan.substrate)
    selected_driver = _selected_postgresql_driver(contract)
    try:
        required = _requirements_from_lock(lock)
    except Exception:
        raise ControllerRuntimeBuildError(
            "controller_requirements_lock_invalid"
        ) from None
    locked_hashes = _requirement_hashes_from_lock(lock, required)
    _validate_wheelhouse_identity(
        plan.wheelhouse, required=required, locked_hashes=locked_hashes
    )
    _require_selected_wheelhouse(
        contract,
        required=required,
        locked_hashes=locked_hashes,
        wheelhouse=plan.wheelhouse,
        selected_driver=selected_driver,
    )
    contract_sha256 = hashlib.sha256(contract_raw).hexdigest()
    lock_sha256 = hashlib.sha256(lock).hexdigest()
    launcher_sha256 = hashlib.sha256(launcher).hexdigest()
    material = _build_plan_material(
        build_nonce=plan.build_nonce,
        package_manifest_sha256=package_sha256,
        controller_runtime_contract_sha256=contract_sha256,
        controller_requirements_lock_sha256=lock_sha256,
        supervisor_launcher_sha256=launcher_sha256,
        release_closure_sha256=closure_sha256,
        substrate=plan.substrate,
        wheelhouse=plan.wheelhouse,
    )
    plan_sha256 = hashlib.sha256(canonical_json_bytes(material)).hexdigest()
    stage_root = STAGING_ROOT_PREFIX / plan_sha256
    if (
        plan.schema_version != BUILD_PLAN_SCHEMA
        or not _is_hash(plan.build_nonce)
        or plan.build_plan_sha256 != plan_sha256
        or plan.package_manifest_sha256 != package_sha256
        or plan.controller_runtime_contract_sha256 != contract_sha256
        or plan.controller_requirements_lock_sha256 != lock_sha256
        or type(plan.release_artifact_count) is not int
        or plan.release_artifact_count != artifact_count
        or plan.release_closure_sha256 != closure_sha256
        or plan.supervisor_launcher_sha256 != launcher_sha256
        or not isinstance(plan.required_distributions, Mapping)
        or dict(plan.required_distributions) != dict(sorted(required.items()))
        or plan.wheelhouse_root
        != str(OFFLINE_WHEELHOUSE_ROOT_PREFIX / plan.wheelhouse.tree_sha256)
        or plan.stage_root != str(stage_root)
        or plan.staged_runtime_root != str(stage_root / "runtime")
        or plan.staged_release_root != str(stage_root / "release")
        or plan.final_release_root != str(RELEASE_ROOT_PREFIX / package_sha256)
        or plan.operations != _RUNTIME_BUILD_OPERATIONS
    ):
        raise ControllerRuntimeBuildError("controller_build_plan_invalid")
    return artifacts


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
        observation.postgresql_driver_identity_sha256,
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
        or observation.postgresql_driver_identity_sha256
        != EXPECTED_POSTGRESQL_DRIVER_IDENTITY_SHA256
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
        "build_plan_sha256": plan.build_plan_sha256,
        "standalone_cpython_specification_sha256": (
            plan.substrate.specification_sha256
        ),
        "standalone_cpython_archive_sha256": plan.substrate.archive_sha256,
        "standalone_cpython_payload_tree_sha256": (
            plan.substrate.payload_tree_sha256
        ),
        "wheelhouse_tree_sha256": plan.wheelhouse.tree_sha256,
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
        "postgresql_driver_identity_sha256": (
            observation.postgresql_driver_identity_sha256
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


def _require_completed_publication(
    plan: ControllerRuntimeBuildPlan,
    result: ControllerRuntimeBuildResult,
) -> ControllerRuntimeBuildResult:
    if type(result) is not ControllerRuntimeBuildResult:
        raise ControllerRuntimeBuildError(
            "controller_build_completed_publication_invalid"
        )
    raw = result.runtime_build_receipt_json
    document = _json_document(
        raw, maximum_bytes=MAX_DOCUMENT_BYTES, canonical=True
    )
    receipt_sha256 = hashlib.sha256(raw).hexdigest()
    expected_runtime_root = str(RUNTIME_ROOT_PREFIX / receipt_sha256)
    expected_receipt_path = str(
        RUNTIME_RECEIPT_ROOT_PREFIX / f"{receipt_sha256}.json"
    )
    exact = {
        "schema_version": RUNTIME_RECEIPT_SCHEMA,
        "result": RUNTIME_RECEIPT_RESULT,
        "package_manifest_sha256": plan.package_manifest_sha256,
        "controller_runtime_contract_sha256": (
            plan.controller_runtime_contract_sha256
        ),
        "controller_requirements_lock_sha256": (
            plan.controller_requirements_lock_sha256
        ),
        "build_plan_sha256": plan.build_plan_sha256,
        "standalone_cpython_specification_sha256": (
            plan.substrate.specification_sha256
        ),
        "standalone_cpython_archive_sha256": plan.substrate.archive_sha256,
        "standalone_cpython_payload_tree_sha256": (
            plan.substrate.payload_tree_sha256
        ),
        "wheelhouse_tree_sha256": plan.wheelhouse.tree_sha256,
        "python_implementation": "CPython",
        "python_version": plan.substrate.python_version,
        "platform_os": "linux",
        "platform_architecture": "x86_64",
        "supervisor_launcher_sha256": plan.supervisor_launcher_sha256,
    }
    false_keys = (
        "pip_present",
        "setuptools_present",
        "wheel_present",
        "user_site_enabled",
        "system_site_packages_enabled",
        "production_data_read",
        "active_production_state_changed",
        "persistent_store_resources_created",
    )
    hash_keys = (
        "runtime_tree_sha256",
        "release_tree_sha256",
        "interpreter_sha256",
        "installed_distribution_inventory_sha256",
        "interpreter_path_facts_sha256",
        "postgresql_driver_identity_sha256",
        "launcher_help_probe_sha256",
    )
    if (
        set(document) != _RECEIPT_KEYS
        or any(document.get(key) != value for key, value in exact.items())
        or any(document.get(key) is not False for key in false_keys)
        or document.get("persistent_controller_substrate_created") is not True
        or any(
            type(document.get(key)) is not int or document[key] != 0
            for key in ("network_calls", "provider_calls")
        )
        or any(not _is_hash(document.get(key)) for key in hash_keys)
        or result.build_plan_sha256 != plan.build_plan_sha256
        or result.controller_runtime_receipt_sha256 != receipt_sha256
        or result.runtime_root != expected_runtime_root
        or result.release_root != plan.final_release_root
        or result.receipt_path != expected_receipt_path
        or not _is_hash(result.runtime_tree_sha256)
        or not _is_hash(result.release_tree_sha256)
        or document.get("runtime_tree_sha256") != result.runtime_tree_sha256
        or document.get("release_tree_sha256") != result.release_tree_sha256
    ):
        raise ControllerRuntimeBuildError(
            "controller_build_completed_publication_invalid"
        )
    return result


def _abandon_stage_or_refuse(
    transport: ControllerRuntimeBuildTransport,
    plan: ControllerRuntimeBuildPlan,
) -> None:
    try:
        abandoned = transport.abandon_owned_stage(plan)
        _require_stage(
            abandoned, state="absent", plan_sha256=plan.build_plan_sha256
        )
    except Exception:
        raise ControllerRuntimeBuildError(
            "controller_build_partial_stage_requires_review"
        ) from None


def execute_controller_runtime_build(
    plan: ControllerRuntimeBuildPlan,
    *,
    standalone_cpython_substrate_json: bytes,
    package_manifest_json: bytes,
    package_artifacts: Mapping[str, bytes],
    transport: ControllerRuntimeBuildTransport,
) -> ControllerRuntimeBuildResult:
    """Run the typed plan through an injected, separately authorized transport."""

    artifact_snapshot = _validate_controller_runtime_build_plan(
        plan,
        standalone_cpython_substrate_json=standalone_cpython_substrate_json,
        package_manifest_json=package_manifest_json,
        package_artifacts=package_artifacts,
    )
    stage_created = False
    publication_started = False
    uncertain_stage_mutation: str | None = None
    try:
        stage = transport.observe_stage(plan)
        if (
            type(stage) is StageObservation
            and stage.state == "publication_completed"
            and stage.build_plan_sha256 == plan.build_plan_sha256
        ):
            publication_started = True
            return _require_completed_publication(
                plan, transport.recover_completed_publication(plan)
            )
        if (
            type(stage) is StageObservation
            and (
                (
                    stage.state == "publication_started"
                    and stage.build_plan_sha256 == plan.build_plan_sha256
                )
                or (
                    stage.state == "publication_started_foreign"
                    and stage.build_plan_sha256 is None
                )
            )
        ):
            publication_started = True
            raise ControllerRuntimeBuildError(
                "controller_build_partial_publication_requires_review"
            )
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
            uncertain_stage_mutation = (
                "controller_build_stage_recovery_requires_review"
            )
            recovered = transport.recover_owned_partial_stage(plan)
            _require_stage(
                recovered, state="absent", plan_sha256=plan.build_plan_sha256
            )
            uncertain_stage_mutation = None
            stage = recovered
        _require_stage(stage, state="absent", plan_sha256=plan.build_plan_sha256)
        uncertain_stage_mutation = (
            "controller_build_stage_creation_requires_review"
        )
        created = transport.create_stage(plan)
        _require_stage(
            created, state="owned_ready", plan_sha256=plan.build_plan_sha256
        )
        stage_created = True
        uncertain_stage_mutation = None
        transport.materialize_standalone_substrate(plan)
        transport.install_locked_offline_distributions(
            plan,
            controller_requirements_lock=artifact_snapshot[
                str(REQUIREMENTS_LOCK_RELATIVE_PATH)
            ],
        )
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
            or publication.publication_intent_retained is not True
            or publication.terminal_state_observed is not True
            or publication.same_device_rename_preconditions_observed is not True
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
        if publication_started:
            raise ControllerRuntimeBuildError(
                "controller_build_partial_publication_requires_review"
            ) from None
        if uncertain_stage_mutation is not None:
            raise ControllerRuntimeBuildError(
                uncertain_stage_mutation
            ) from None
        if stage_created and not publication_started:
            _abandon_stage_or_refuse(transport, plan)
        raise
    except Exception:
        if publication_started:
            raise ControllerRuntimeBuildError(
                "controller_build_partial_publication_requires_review"
            ) from None
        if uncertain_stage_mutation is not None:
            raise ControllerRuntimeBuildError(
                uncertain_stage_mutation
            ) from None
        if stage_created:
            _abandon_stage_or_refuse(transport, plan)
        raise ControllerRuntimeBuildError(
            "controller_build_transport_failed"
        ) from None


__all__ = [
    "BUILD_RESULT_TYPE",
    "ControllerRuntimeBuildError",
    "ControllerRuntimeBuildPlan",
    "ControllerRuntimeBuildResult",
    "ControllerRuntimeBuildTransport",
    "BuildObservation",
    "ExactOfflineWheelhouse",
    "PublicationObservation",
    "PublicationTargetObservation",
    "SELECTED_CPYTHON_ARCHIVE_NAME",
    "SELECTED_CPYTHON_ARCHIVE_SHA256",
    "SELECTED_CPYTHON_VERSION",
    "StageObservation",
    "StandaloneCPythonSubstrate",
    "WHEELHOUSE_TREE_SCHEMA",
    "WheelhouseMember",
    "canonical_wheelhouse_identity",
    "create_controller_runtime_build_plan",
    "execute_controller_runtime_build",
    "parse_standalone_cpython_substrate",
]
