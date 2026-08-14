from __future__ import annotations

"""Fence the exact failed Phase 9J staged-only prefix without deleting it.

This module is deliberately not an entrypoint.  A sealed manager must first
materialize the reviewed repository contract at the durable contract path and
then call :func:`execute_staged_prefix_disposition` while it still owns the
normal live-proof authority boundary.  The disposition preserves every byte
of the failed attempt, proves that the attempt made no store or service
effect, and creates one fail-closed tombstone in the failed attempt's resolved
store-spec slot.  A corrected attempt must require that tombstone before its
first effect.
"""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from types import MappingProxyType
from typing import Final, Mapping, Protocol, Sequence

from tools.governed_memory_install.execution_lock import (
    ExecutionLockError,
    GlobalExecutionLock,
)


CONTRACT_SCHEMA: Final = (
    "governed-memory-phase9-staged-prefix-disposition-contract-v1"
)
TOMBSTONE_SCHEMA: Final = (
    "governed-memory-phase9-staged-prefix-disposition-tombstone-v1"
)
RESULT: Final = "failed_staged_prefix_fenced_for_corrected_successor"

PRODUCTION_REPOSITORY_ROOT: Final = Path(
    "/tmp/chat-memory-governed-phase8d-retirement-20260812"
)
PRODUCTION_REPOSITORY_CONTRACT_SOURCE: Final = (
    "ops/governed_memory/staged_prefix_disposition_contract.json"
)
PRODUCTION_STATE_ROOT: Final = Path("/var/lib/governed-memory-controller")
PRODUCTION_CONTROL_ROOT: Final = Path("/etc/governed-memory-controller")
PRODUCTION_DURABLE_CONTRACT_PATH: Final = (
    PRODUCTION_STATE_ROOT
    / "pre-effect-dispositions"
    / "phase9-v6-staged-to-v7-000003.contract.json"
)
PRODUCTION_TOMBSTONE_PATH: Final = PRODUCTION_CONTROL_ROOT / "store_spec-v2.json"
PRODUCTION_OLD_TAG_REF: Final = (
    "refs/tags/governed-memory-phase9j-pre-effect-disposition-000002"
)
PRODUCTION_OLD_PERMIT_PATH: Final = (
    PRODUCTION_STATE_ROOT / "phase9j-pre-effect-disposition-permit-000002.json"
)
PRODUCTION_OLD_CONTRACT_PATH: Final = (
    PRODUCTION_STATE_ROOT
    / "pre-effect-dispositions"
    / "phase9-v5-pre-effect-to-v6-000002.contract.json"
)
PRODUCTION_OLD_RECEIPT_PATH: Final = PRODUCTION_CONTROL_ROOT / "store_spec.json"
PRODUCTION_STAGED_CAPSULE_PATH: Final = (
    PRODUCTION_STATE_ROOT
    / "phase9-disposable-proof-recovery-capsule-v3.json.publishing"
)
PRODUCTION_FINAL_CAPSULE_PATH: Final = (
    PRODUCTION_STATE_ROOT / "phase9-disposable-proof-recovery-capsule-v3.json"
)
PRODUCTION_AUTHORITY_STATE_PATH: Final = (
    PRODUCTION_STATE_ROOT / "authority-state-v2.sqlite3"
)
PRODUCTION_PROMOTABLE_RECEIPT_PATH: Final = (
    PRODUCTION_STATE_ROOT
    / "phase9-disposable-live-proof-promotable-receipt-000002.json"
)
PRODUCTION_PROMOTABLE_RECEIPT_STAGING_PATH: Final = Path(
    str(PRODUCTION_PROMOTABLE_RECEIPT_PATH) + ".publishing"
)
PRODUCTION_EXECUTIONS_ROOT: Final = PRODUCTION_STATE_ROOT / "executions-v2"
PRODUCTION_SECRET_ROOT: Final = Path(
    "/etc/governed-memory-stores/9a54cf123493-000002"
)
PRODUCTION_SYSTEMD_UNIT_PATH: Final = Path(
    "/etc/systemd/system/governed-memory-stores-v2.service"
)
PRODUCTION_SYSTEMD_LINK_PATH: Final = Path(
    "/etc/systemd/system/multi-user.target.wants/"
    "governed-memory-stores-v2.service"
)
PRODUCTION_GLOBAL_LOCK_PATH: Final = Path(
    "/run/lock/governed-memory-controller/execution.lock"
)
PRODUCTION_LIVE_GUARD_PATH: Final = Path(
    "/run/lock/governed-memory-controller/phase9-disposable-live-proof.lock"
)
PRODUCTION_FAILED_GENERATION: Final = "000002"
PRODUCTION_CORRECTED_GENERATION: Final = "000003"
PRODUCTION_CONTRACT_SHA256: Final[str | None] = (
    "14eacc2c32492f2d76d5c7a3d145a1b0a019077fd3a880e74b5e12a658d55de8"
)
PRODUCTION_FAILED_PACKAGE_MANIFEST_SHA256: Final = (
    "2e5da1091b456b705d955c5cc3a119e507116f892de0bcb92017185ca4ed2e89"
)
PRODUCTION_FAILED_RUNTIME_RECEIPT_SHA256: Final = (
    "7de191f42a1c14b6bc2c29b3e7b425bd1b2593f7de95e03af7d2f93ff5d4a53b"
)
PRODUCTION_OLD_TAG_COMMIT: Final = (
    "2c1bfc4afd4990323d13435f4d3c9fff90499a41"
)
PRODUCTION_OLD_TAG_TREE: Final = (
    "3bee80d9a0c13c6aa70628da6286077a02ab8079"
)
PRODUCTION_OLD_PERMIT_SHA256: Final = (
    "ea184d209b3dc9e0f3afb2a19eedba22393ac25a9c67a2980223cc1e12016907"
)
PRODUCTION_OLD_CONTRACT_SHA256: Final = (
    "758175f12c844a81c1fac2061d2a41d094034d697241545656c8ddac4e51e989"
)
PRODUCTION_OLD_RECEIPT_SHA256: Final = (
    "d61779a4b0a41dd2670134f47bfe18c2c449a208dda9e4ea01d3eeba88f950b3"
)
PRODUCTION_STAGED_CAPSULE_SHA256: Final = (
    "008275ce2f3102aad10440dcb45038f7a4d4d4b60033429b7078cd1542511771"
)
PRODUCTION_CORRECTED_AUTHORITY_STATE_PATH: Final = (
    PRODUCTION_STATE_ROOT / "authority-state-v3.sqlite3"
)
PRODUCTION_CORRECTED_CAPSULE_PATH: Final = (
    PRODUCTION_STATE_ROOT / "phase9-disposable-proof-recovery-capsule-v4.json"
)
PRODUCTION_CORRECTED_CAPSULE_STAGING_PATH: Final = Path(
    str(PRODUCTION_CORRECTED_CAPSULE_PATH) + ".publishing"
)
PRODUCTION_CORRECTED_PROMOTABLE_RECEIPT_PATH: Final = (
    PRODUCTION_STATE_ROOT
    / "phase9-disposable-live-proof-promotable-receipt-000003.json"
)
PRODUCTION_CORRECTED_EXECUTIONS_ROOT: Final = (
    PRODUCTION_STATE_ROOT / "executions-v3"
)
PRODUCTION_CORRECTED_SECRET_ROOT: Final = Path(
    "/etc/governed-memory-stores/9a54cf123493-000003"
)
PRODUCTION_CORRECTED_STORE_SPEC_PATH: Final = (
    PRODUCTION_CONTROL_ROOT / "store_spec-v3.json"
)
PRODUCTION_CORRECTED_SYSTEMD_UNIT_PATH: Final = Path(
    "/etc/systemd/system/governed-memory-stores-v3.service"
)
PRODUCTION_CORRECTED_SYSTEMD_LINK_PATH: Final = Path(
    "/etc/systemd/system/multi-user.target.wants/"
    "governed-memory-stores-v3.service"
)
PRODUCTION_CORRECTED_AUTHORIZATION_NAMESPACE: Final = (
    "governed-memory-phase9-live-proof-v3"
)
PRODUCTION_CORRECTED_INSTALL_SCOPE_ID: Final = (
    "phase9-disposable-live-install-000003"
)
PRODUCTION_CORRECTED_ROLLBACK_SCOPE_ID: Final = (
    "phase9-disposable-live-rollback-000003"
)
PRODUCTION_DISPOSITION_ID: Final = "phase9-v6-staged-to-v7-000003"

_FAILED_PREFIX_IDENTITY_SCHEMA: Final = (
    "governed-memory-phase9-failed-staged-prefix-identity-v1"
)
_FAILED_PREFIX_IDENTITY_DOMAIN: Final = (
    b"governed-memory-phase9-failed-staged-prefix-identity-v1\x00"
)
_CORRECTED_ATTEMPT_IDENTITY_SCHEMA: Final = (
    "governed-memory-phase9-corrected-attempt-identity-v1"
)
_CORRECTED_ATTEMPT_IDENTITY_DOMAIN: Final = (
    b"governed-memory-phase9-corrected-attempt-identity-v1\x00"
)

MAX_CONTRACT_BYTES: Final = 256 * 1024
MAX_EVIDENCE_BYTES: Final = 512 * 1024
MAX_TOMBSTONE_BYTES: Final = 64 * 1024
MAX_COMMAND_OUTPUT_BYTES: Final = 128 * 1024

_HASH_RE: Final = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_GIT_RE: Final = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_ID_RE: Final = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}\Z", re.ASCII)
_GENERATION_RE: Final = re.compile(r"[0-9]{6}\Z", re.ASCII)
_TAG_RE: Final = re.compile(
    r"refs/tags/[A-Za-z0-9][A-Za-z0-9._/-]{0,190}\Z", re.ASCII
)
_RESOURCE_NAME_RE: Final = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z", re.ASCII
)

_FILE_ROLES: Final = (
    "old_contract",
    "old_permit",
    "old_pre_effect_receipt",
    "staged_capsule",
)
_DIRECTORY_ROLES: Final = ("executions_v2", "store_secret")
_FILE_SPEC_KEYS: Final = {
    "role",
    "path",
    "sha256",
    "size",
    "mode",
    "uid",
    "gid",
    "nlink",
    "device",
    "inode",
}
_DIRECTORY_SPEC_KEYS: Final = {
    "role",
    "path",
    "mode",
    "uid",
    "gid",
    "nlink",
    "device",
    "inode",
}
_CONTRACT_KEYS: Final = {
    "schema_version",
    "disposition_id",
    "repository_contract_source",
    "durable_contract_path",
    "tombstone_path",
    "failed_attempt",
    "corrected_successor",
    "empty_directories",
    "absent_resources",
}
_FAILED_ATTEMPT_KEYS: Final = {
    "generation",
    "tag",
    "package_manifest_sha256",
    "controller_runtime_receipt_sha256",
    "evidence_files",
}
_TAG_KEYS: Final = {"ref", "object_type", "commit", "tree"}
_SUCCESSOR_KEYS: Final = {
    "generation",
    "attempt_identity_sha256",
    "package_manifest_sha256",
    "controller_runtime_receipt_sha256",
    "authority_state_path",
    "capsule_path",
    "authorization_namespace",
    "install_scope_id",
    "rollback_scope_id",
    "physical_resource_generation",
}
_ABSENT_RESOURCE_KEYS: Final = {"kind", "identity"}

_DOCKER_IDENTITIES: Final = MappingProxyType(
    {
        "docker_container": (
            "governed-memory-postgres-9a54cf123493-000002",
            "governed-memory-qdrant-9a54cf123493-000002",
            "governed-memory-postgres-9a54cf123493-000003",
            "governed-memory-qdrant-9a54cf123493-000003",
        ),
        "docker_network": (
            "governed-memory-net-9a54cf123493-000002",
            "governed-memory-net-9a54cf123493-000003",
        ),
        "docker_volume": (
            "governed-memory-postgres-data-9a54cf123493-000002",
            "governed-memory-qdrant-data-9a54cf123493-000002",
            "governed-memory-postgres-data-9a54cf123493-000003",
            "governed-memory-qdrant-data-9a54cf123493-000003",
        ),
    }
)
_DOCKER_COMMANDS: Final = MappingProxyType(
    {
        "docker_container": (
            "/usr/bin/docker",
            "ps",
            "--all",
            "--format",
            "{{.Names}}",
        ),
        "docker_network": (
            "/usr/bin/docker",
            "network",
            "ls",
            "--no-trunc",
            "--format",
            "{{.Name}}",
        ),
        "docker_volume": (
            "/usr/bin/docker",
            "volume",
            "ls",
            "--format",
            "{{.Name}}",
        ),
    }
)
_TCP_IDENTITIES: Final = MappingProxyType(
    {
        "127.0.0.1:55433@000002": "55433",
        "127.0.0.1:6344@000002": "6344",
        "127.0.0.1:55434@000003": "55434",
        "127.0.0.1:6345@000003": "6345",
    }
)
_SYSTEMD_SERVICES: Final = (
    "governed-memory-stores-v2.service",
    "governed-memory-stores-v3.service",
)


class StagedPrefixDispositionError(RuntimeError):
    """Base class for content-free staged-prefix disposition refusals."""


class StagedPrefixDispositionSecurityError(StagedPrefixDispositionError):
    """A path, identity, lock, or command boundary is unsafe."""


class StagedPrefixDispositionIntegrityError(StagedPrefixDispositionError):
    """Reviewed bytes or exact evidence identity do not match."""


class StagedPrefixDispositionHostStateError(StagedPrefixDispositionError):
    """A forbidden effect is present or absence cannot be proved."""


@dataclass(frozen=True, slots=True)
class StagedPrefixDispositionPaths:
    repository_root: Path
    durable_contract_path: Path
    tombstone_path: Path
    old_permit_path: Path
    old_contract_path: Path
    old_pre_effect_receipt_path: Path
    staged_capsule_path: Path
    final_capsule_path: Path
    authority_state_path: Path
    promotable_receipt_path: Path
    promotable_receipt_staging_path: Path
    systemd_unit_path: Path
    systemd_link_path: Path
    executions_root: Path
    store_secret_root: Path
    corrected_capsule_path: Path
    corrected_capsule_staging_path: Path
    corrected_authority_state_path: Path
    corrected_promotable_receipt_path: Path
    corrected_promotable_receipt_staging_path: Path
    corrected_executions_root: Path
    corrected_secret_root: Path
    corrected_store_spec_path: Path
    corrected_systemd_unit_path: Path
    corrected_systemd_link_path: Path
    global_lock_path: Path
    live_guard_path: Path
    expected_uid: int
    expected_gid: int
    production: bool = False

    def __post_init__(self) -> None:
        path_values = tuple(
            value
            for name, value in (
                (field_name, getattr(self, field_name))
                for field_name in self.__dataclass_fields__
            )
            if name
            not in {"expected_uid", "expected_gid", "production"}
        )
        concrete_path_type = type(Path("/"))
        if (
            any(
                type(value) is not concrete_path_type or not value.is_absolute()
                for value in path_values
            )
            or len({str(value) for value in path_values}) != len(path_values)
            or type(self.expected_uid) is not int
            or self.expected_uid < 0
            or type(self.expected_gid) is not int
            or self.expected_gid < 0
            or type(self.production) is not bool
        ):
            raise StagedPrefixDispositionSecurityError(
                "staged_prefix_disposition_paths_invalid"
            )
        if self.production and (
            self.repository_root != PRODUCTION_REPOSITORY_ROOT
            or self.durable_contract_path != PRODUCTION_DURABLE_CONTRACT_PATH
            or self.tombstone_path != PRODUCTION_TOMBSTONE_PATH
            or self.old_permit_path != PRODUCTION_OLD_PERMIT_PATH
            or self.old_contract_path != PRODUCTION_OLD_CONTRACT_PATH
            or self.old_pre_effect_receipt_path != PRODUCTION_OLD_RECEIPT_PATH
            or self.staged_capsule_path != PRODUCTION_STAGED_CAPSULE_PATH
            or self.final_capsule_path != PRODUCTION_FINAL_CAPSULE_PATH
            or self.authority_state_path != PRODUCTION_AUTHORITY_STATE_PATH
            or self.promotable_receipt_path
            != PRODUCTION_PROMOTABLE_RECEIPT_PATH
            or self.promotable_receipt_staging_path
            != PRODUCTION_PROMOTABLE_RECEIPT_STAGING_PATH
            or self.systemd_unit_path != PRODUCTION_SYSTEMD_UNIT_PATH
            or self.systemd_link_path != PRODUCTION_SYSTEMD_LINK_PATH
            or self.executions_root != PRODUCTION_EXECUTIONS_ROOT
            or self.store_secret_root != PRODUCTION_SECRET_ROOT
            or self.corrected_capsule_path
            != PRODUCTION_CORRECTED_CAPSULE_PATH
            or self.corrected_capsule_staging_path
            != PRODUCTION_CORRECTED_CAPSULE_STAGING_PATH
            or self.corrected_authority_state_path
            != PRODUCTION_CORRECTED_AUTHORITY_STATE_PATH
            or self.corrected_promotable_receipt_path
            != PRODUCTION_CORRECTED_PROMOTABLE_RECEIPT_PATH
            or self.corrected_promotable_receipt_staging_path
            != Path(
                str(PRODUCTION_CORRECTED_PROMOTABLE_RECEIPT_PATH)
                + ".publishing"
            )
            or self.corrected_executions_root
            != PRODUCTION_CORRECTED_EXECUTIONS_ROOT
            or self.corrected_secret_root != PRODUCTION_CORRECTED_SECRET_ROOT
            or self.corrected_store_spec_path
            != PRODUCTION_CORRECTED_STORE_SPEC_PATH
            or self.corrected_systemd_unit_path
            != PRODUCTION_CORRECTED_SYSTEMD_UNIT_PATH
            or self.corrected_systemd_link_path
            != PRODUCTION_CORRECTED_SYSTEMD_LINK_PATH
            or self.global_lock_path != PRODUCTION_GLOBAL_LOCK_PATH
            or self.live_guard_path != PRODUCTION_LIVE_GUARD_PATH
            or self.expected_uid != 0
            or self.expected_gid != 0
        ):
            raise StagedPrefixDispositionSecurityError(
                "staged_prefix_disposition_production_paths_invalid"
            )


@dataclass(frozen=True, slots=True)
class ReviewedStagedPrefixExpectation:
    contract_sha256: str
    disposition_id: str
    old_tag_ref: str
    old_tag_commit: str
    old_tag_tree: str
    failed_package_manifest_sha256: str
    failed_controller_runtime_receipt_sha256: str
    corrected_generation: str
    corrected_package_manifest_sha256: str
    corrected_controller_runtime_receipt_sha256: str

    def __post_init__(self) -> None:
        if (
            any(
                _HASH_RE.fullmatch(value) is None
                for value in (
                    self.contract_sha256,
                    self.failed_package_manifest_sha256,
                    self.failed_controller_runtime_receipt_sha256,
                    self.corrected_package_manifest_sha256,
                    self.corrected_controller_runtime_receipt_sha256,
                )
            )
            or _ID_RE.fullmatch(self.disposition_id) is None
            or _TAG_RE.fullmatch(self.old_tag_ref) is None
            or _GIT_RE.fullmatch(self.old_tag_commit) is None
            or _GIT_RE.fullmatch(self.old_tag_tree) is None
            or _GENERATION_RE.fullmatch(self.corrected_generation) is None
        ):
            raise StagedPrefixDispositionIntegrityError(
                "staged_prefix_disposition_expectation_invalid"
            )


class CommandRunner(Protocol):
    def run(self, argv: Sequence[str]) -> subprocess.CompletedProcess[bytes]: ...


@dataclass(frozen=True, slots=True)
class _StableFile:
    raw: bytes
    identity: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _StableDirectory:
    identity: tuple[int, ...]


class _ClosedCommandRunner:
    __slots__ = ("_allowed",)

    def __init__(self, allowed: frozenset[tuple[str, ...]]) -> None:
        self._allowed = allowed

    def run(self, argv: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
        command = tuple(argv)
        if command not in self._allowed:
            raise OSError("staged_prefix_disposition_command_refused")
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd="/",
            env={
                "PATH": "/usr/bin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
            },
            timeout=20.0,
            check=False,
            close_fds=True,
        )


def production_disposition_paths() -> StagedPrefixDispositionPaths:
    return StagedPrefixDispositionPaths(
        repository_root=PRODUCTION_REPOSITORY_ROOT,
        durable_contract_path=PRODUCTION_DURABLE_CONTRACT_PATH,
        tombstone_path=PRODUCTION_TOMBSTONE_PATH,
        old_permit_path=PRODUCTION_OLD_PERMIT_PATH,
        old_contract_path=PRODUCTION_OLD_CONTRACT_PATH,
        old_pre_effect_receipt_path=PRODUCTION_OLD_RECEIPT_PATH,
        staged_capsule_path=PRODUCTION_STAGED_CAPSULE_PATH,
        final_capsule_path=PRODUCTION_FINAL_CAPSULE_PATH,
        authority_state_path=PRODUCTION_AUTHORITY_STATE_PATH,
        promotable_receipt_path=PRODUCTION_PROMOTABLE_RECEIPT_PATH,
        promotable_receipt_staging_path=(
            PRODUCTION_PROMOTABLE_RECEIPT_STAGING_PATH
        ),
        systemd_unit_path=PRODUCTION_SYSTEMD_UNIT_PATH,
        systemd_link_path=PRODUCTION_SYSTEMD_LINK_PATH,
        executions_root=PRODUCTION_EXECUTIONS_ROOT,
        store_secret_root=PRODUCTION_SECRET_ROOT,
        corrected_capsule_path=PRODUCTION_CORRECTED_CAPSULE_PATH,
        corrected_capsule_staging_path=(
            PRODUCTION_CORRECTED_CAPSULE_STAGING_PATH
        ),
        corrected_authority_state_path=(
            PRODUCTION_CORRECTED_AUTHORITY_STATE_PATH
        ),
        corrected_promotable_receipt_path=(
            PRODUCTION_CORRECTED_PROMOTABLE_RECEIPT_PATH
        ),
        corrected_promotable_receipt_staging_path=Path(
            str(PRODUCTION_CORRECTED_PROMOTABLE_RECEIPT_PATH) + ".publishing"
        ),
        corrected_executions_root=PRODUCTION_CORRECTED_EXECUTIONS_ROOT,
        corrected_secret_root=PRODUCTION_CORRECTED_SECRET_ROOT,
        corrected_store_spec_path=PRODUCTION_CORRECTED_STORE_SPEC_PATH,
        corrected_systemd_unit_path=PRODUCTION_CORRECTED_SYSTEMD_UNIT_PATH,
        corrected_systemd_link_path=PRODUCTION_CORRECTED_SYSTEMD_LINK_PATH,
        global_lock_path=PRODUCTION_GLOBAL_LOCK_PATH,
        live_guard_path=PRODUCTION_LIVE_GUARD_PATH,
        expected_uid=0,
        expected_gid=0,
        production=True,
    )


def canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as error:
        raise StagedPrefixDispositionIntegrityError(
            "staged_prefix_disposition_document_invalid"
        ) from error


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def production_failed_prefix_identity_sha256() -> str:
    """Bind the exact immutable 000002 failed-prefix evidence family."""

    material = {
        "schema_version": _FAILED_PREFIX_IDENTITY_SCHEMA,
        "generation": PRODUCTION_FAILED_GENERATION,
        "tag_ref": PRODUCTION_OLD_TAG_REF,
        "tag_commit": PRODUCTION_OLD_TAG_COMMIT,
        "tag_tree": PRODUCTION_OLD_TAG_TREE,
        "package_manifest_sha256": PRODUCTION_FAILED_PACKAGE_MANIFEST_SHA256,
        "controller_runtime_receipt_sha256": (
            PRODUCTION_FAILED_RUNTIME_RECEIPT_SHA256
        ),
        "permit_sha256": PRODUCTION_OLD_PERMIT_SHA256,
        "contract_sha256": PRODUCTION_OLD_CONTRACT_SHA256,
        "receipt_sha256": PRODUCTION_OLD_RECEIPT_SHA256,
        "staged_capsule_sha256": PRODUCTION_STAGED_CAPSULE_SHA256,
    }
    return _sha(
        _FAILED_PREFIX_IDENTITY_DOMAIN + canonical_json_bytes(material)
    )


def production_corrected_attempt_identity_sha256(
    *,
    package_manifest_sha256: str,
    controller_runtime_receipt_sha256: str,
) -> str:
    """Bind P/R to the distinct 000003 paths, scopes, and resources."""

    if (
        type(package_manifest_sha256) is not str
        or _HASH_RE.fullmatch(package_manifest_sha256) is None
        or type(controller_runtime_receipt_sha256) is not str
        or _HASH_RE.fullmatch(controller_runtime_receipt_sha256) is None
    ):
        raise StagedPrefixDispositionIntegrityError(
            "staged_prefix_disposition_corrected_identity_inputs_invalid"
        )
    material = {
        "schema_version": _CORRECTED_ATTEMPT_IDENTITY_SCHEMA,
        "generation": PRODUCTION_CORRECTED_GENERATION,
        "physical_resource_generation": PRODUCTION_CORRECTED_GENERATION,
        "package_manifest_sha256": package_manifest_sha256,
        "controller_runtime_receipt_sha256": (
            controller_runtime_receipt_sha256
        ),
        "authority_state_path": str(
            PRODUCTION_CORRECTED_AUTHORITY_STATE_PATH
        ),
        "capsule_path": str(PRODUCTION_CORRECTED_CAPSULE_PATH),
        "authorization_namespace": (
            PRODUCTION_CORRECTED_AUTHORIZATION_NAMESPACE
        ),
        "install_scope_id": PRODUCTION_CORRECTED_INSTALL_SCOPE_ID,
        "rollback_scope_id": PRODUCTION_CORRECTED_ROLLBACK_SCOPE_ID,
    }
    return _sha(
        _CORRECTED_ATTEMPT_IDENTITY_DOMAIN + canonical_json_bytes(material)
    )


def _corrected_successor_document(
    expectation: ReviewedStagedPrefixExpectation,
) -> dict[str, object]:
    return {
        "generation": expectation.corrected_generation,
        "attempt_identity_sha256": (
            production_corrected_attempt_identity_sha256(
                package_manifest_sha256=(
                    expectation.corrected_package_manifest_sha256
                ),
                controller_runtime_receipt_sha256=(
                    expectation.corrected_controller_runtime_receipt_sha256
                ),
            )
        ),
        "package_manifest_sha256": (
            expectation.corrected_package_manifest_sha256
        ),
        "controller_runtime_receipt_sha256": (
            expectation.corrected_controller_runtime_receipt_sha256
        ),
        "authority_state_path": str(
            PRODUCTION_CORRECTED_AUTHORITY_STATE_PATH
        ),
        "capsule_path": str(PRODUCTION_CORRECTED_CAPSULE_PATH),
        "authorization_namespace": (
            PRODUCTION_CORRECTED_AUTHORIZATION_NAMESPACE
        ),
        "install_scope_id": PRODUCTION_CORRECTED_INSTALL_SCOPE_ID,
        "rollback_scope_id": PRODUCTION_CORRECTED_ROLLBACK_SCOPE_ID,
        "physical_resource_generation": PRODUCTION_CORRECTED_GENERATION,
    }


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def _canonical_object(raw: bytes, code: str) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeError) as error:
        raise StagedPrefixDispositionIntegrityError(code) from error
    if type(value) is not dict or canonical_json_bytes(value) != raw:
        raise StagedPrefixDispositionIntegrityError(code)
    return value


def _stable_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        stat.S_IMODE(value.st_mode),
        value.st_uid,
        value.st_gid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_regular(
    path: Path,
    *,
    mode: int,
    uid: int,
    gid: int,
    nlink: int,
    maximum: int,
) -> _StableFile:
    parent_fd = file_fd = -1
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow == 0:
        raise StagedPrefixDispositionSecurityError(
            "staged_prefix_disposition_nofollow_unavailable"
        )
    try:
        parent_fd = os.open(
            path.parent,
            os.O_RDONLY
            | os.O_CLOEXEC
            | nofollow
            | getattr(os, "O_DIRECTORY", 0),
        )
        parent = os.fstat(parent_fd)
        named_parent = path.parent.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or stat.S_IMODE(parent.st_mode) != 0o700
            or parent.st_uid != uid
            or parent.st_gid != gid
            or _stable_identity(parent) != _stable_identity(named_parent)
        ):
            raise StagedPrefixDispositionSecurityError(
                "staged_prefix_disposition_parent_invalid"
            )
        file_fd = os.open(
            path.name,
            os.O_RDONLY
            | os.O_CLOEXEC
            | nofollow
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_fd,
        )
        before = os.fstat(file_fd)
        named = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != mode
            or before.st_uid != uid
            or before.st_gid != gid
            or before.st_nlink != nlink
            or before.st_size <= 0
            or before.st_size > maximum
            or _stable_identity(before) != _stable_identity(named)
        ):
            raise StagedPrefixDispositionSecurityError(
                "staged_prefix_disposition_file_invalid"
            )
        content = bytearray()
        while len(content) <= maximum:
            block = os.read(file_fd, min(65536, maximum + 1 - len(content)))
            if not block:
                break
            content.extend(block)
        after = os.fstat(file_fd)
        named_after = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            len(content) != before.st_size
            or _stable_identity(after) != _stable_identity(before)
            or _stable_identity(named_after) != _stable_identity(before)
        ):
            raise StagedPrefixDispositionSecurityError(
                "staged_prefix_disposition_file_changed"
            )
        return _StableFile(bytes(content), _stable_identity(before))
    except StagedPrefixDispositionError:
        raise
    except OSError as error:
        raise StagedPrefixDispositionSecurityError(
            "staged_prefix_disposition_file_unavailable"
        ) from error
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _file_spec_matches(
    spec: object,
    *,
    role: str,
    path: Path,
    value: _StableFile,
    uid: int,
    gid: int,
) -> bool:
    identity = value.identity
    return bool(
        type(spec) is dict
        and set(spec) == _FILE_SPEC_KEYS
        and spec.get("role") == role
        and spec.get("path") == str(path)
        and spec.get("sha256") == _sha(value.raw)
        and spec.get("size") == len(value.raw)
        and spec.get("mode") == 0o400
        and spec.get("uid") == uid
        and spec.get("gid") == gid
        and spec.get("nlink") == 1
        and spec.get("device") == identity[0]
        and spec.get("inode") == identity[1]
    )


def _verify_empty_directory(
    spec: object,
    *,
    role: str,
    path: Path,
    uid: int,
    gid: int,
) -> _StableDirectory:
    descriptor = -1
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow == 0:
        raise StagedPrefixDispositionSecurityError(
            "staged_prefix_disposition_nofollow_unavailable"
        )
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | os.O_CLOEXEC
            | nofollow
            | getattr(os, "O_DIRECTORY", 0),
        )
        before = os.fstat(descriptor)
        named = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o700
            or before.st_uid != uid
            or before.st_gid != gid
            or _stable_identity(before) != _stable_identity(named)
        ):
            raise StagedPrefixDispositionSecurityError(
                "staged_prefix_disposition_empty_directory_invalid"
            )
        if os.listdir(descriptor) != []:
            raise StagedPrefixDispositionHostStateError(
                "staged_prefix_disposition_directory_not_empty"
            )
        if (
            type(spec) is not dict
            or set(spec) != _DIRECTORY_SPEC_KEYS
            or spec.get("role") != role
            or spec.get("path") != str(path)
            or spec.get("mode") != 0o700
            or spec.get("uid") != uid
            or spec.get("gid") != gid
            or spec.get("nlink") != before.st_nlink
            or spec.get("device") != before.st_dev
            or spec.get("inode") != before.st_ino
        ):
            raise StagedPrefixDispositionIntegrityError(
                "staged_prefix_disposition_empty_directory_invalid"
            )
        after = os.fstat(descriptor)
        named_after = path.stat(follow_symlinks=False)
        if (
            _stable_identity(after) != _stable_identity(before)
            or _stable_identity(named_after) != _stable_identity(before)
        ):
            raise StagedPrefixDispositionSecurityError(
                "staged_prefix_disposition_directory_changed"
            )
        return _StableDirectory(_stable_identity(before))
    except StagedPrefixDispositionError:
        raise
    except OSError as error:
        raise StagedPrefixDispositionSecurityError(
            "staged_prefix_disposition_directory_unavailable"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _path_absence_set(paths: StagedPrefixDispositionPaths) -> tuple[Path, ...]:
    authority = str(paths.authority_state_path)
    corrected_authority = str(paths.corrected_authority_state_path)
    return (
        paths.final_capsule_path,
        paths.authority_state_path,
        Path(authority + "-journal"),
        Path(authority + "-shm"),
        Path(authority + "-wal"),
        paths.promotable_receipt_path,
        paths.promotable_receipt_staging_path,
        paths.systemd_unit_path,
        paths.systemd_link_path,
        paths.corrected_capsule_path,
        paths.corrected_capsule_staging_path,
        paths.corrected_authority_state_path,
        Path(corrected_authority + "-journal"),
        Path(corrected_authority + "-shm"),
        Path(corrected_authority + "-wal"),
        paths.corrected_promotable_receipt_path,
        paths.corrected_promotable_receipt_staging_path,
        paths.corrected_executions_root,
        paths.corrected_secret_root,
        paths.corrected_store_spec_path,
        paths.corrected_systemd_unit_path,
        paths.corrected_systemd_link_path,
    )


def _expected_absent_resources(
    paths: StagedPrefixDispositionPaths,
) -> list[dict[str, str]]:
    result = [
        {"kind": "path", "identity": str(path)}
        for path in _path_absence_set(paths)
    ]
    for kind, identities in _DOCKER_IDENTITIES.items():
        result.extend({"kind": kind, "identity": value} for value in identities)
    result.extend(
        {"kind": "tcp_listener", "identity": value}
        for value in _TCP_IDENTITIES
    )
    result.extend(
        {"kind": "systemd_service", "identity": service}
        for service in _SYSTEMD_SERVICES
    )
    return sorted(result, key=lambda item: (item["kind"], item["identity"]))


def _git_command(paths: StagedPrefixDispositionPaths, *args: str) -> tuple[str, ...]:
    return (
        "/usr/bin/git",
        "-c",
        "safe.directory=" + str(paths.repository_root),
        "-C",
        str(paths.repository_root),
        *args,
    )


def _tcp_command(port: str) -> tuple[str, ...]:
    return (
        "/usr/bin/ss",
        "--tcp",
        "--listening",
        "--numeric",
        "--no-header",
        "sport",
        "=",
        ":" + port,
    )


def _systemd_command(service: str) -> tuple[str, ...]:
    return (
        "/usr/bin/systemctl",
        "show",
        service,
        "--property=LoadState",
        "--value",
    )


def _allowed_commands(
    paths: StagedPrefixDispositionPaths,
    expectation: ReviewedStagedPrefixExpectation,
) -> frozenset[tuple[str, ...]]:
    return frozenset(
        {
            _git_command(paths, "rev-parse", "--show-toplevel"),
            _git_command(paths, "cat-file", "-t", expectation.old_tag_ref),
            _git_command(
                paths, "rev-parse", "--verify", expectation.old_tag_ref
            ),
            _git_command(
                paths,
                "rev-parse",
                "--verify",
                expectation.old_tag_ref + "^{commit}",
            ),
            _git_command(
                paths,
                "rev-parse",
                "--verify",
                expectation.old_tag_ref + "^{tree}",
            ),
            *_DOCKER_COMMANDS.values(),
            *(_tcp_command(port) for port in _TCP_IDENTITIES.values()),
            *(_systemd_command(service) for service in _SYSTEMD_SERVICES),
        }
    )


def _command(
    runner: CommandRunner, argv: tuple[str, ...], *, code: str
) -> bytes:
    try:
        completed = runner.run(argv)
    except (OSError, subprocess.SubprocessError) as error:
        raise StagedPrefixDispositionHostStateError(code) from error
    if (
        type(completed) is not subprocess.CompletedProcess
        or completed.returncode != 0
        or type(completed.stdout) is not bytes
        or type(completed.stderr) is not bytes
        or completed.stderr != b""
        or len(completed.stdout) > MAX_COMMAND_OUTPUT_BYTES
    ):
        raise StagedPrefixDispositionHostStateError(code)
    return completed.stdout


def _verify_git_tag(
    paths: StagedPrefixDispositionPaths,
    expectation: ReviewedStagedPrefixExpectation,
    *,
    runner: CommandRunner,
) -> None:
    expected = {
        _git_command(paths, "rev-parse", "--show-toplevel"): (
            str(paths.repository_root) + "\n"
        ).encode("utf-8"),
        _git_command(paths, "cat-file", "-t", expectation.old_tag_ref): b"commit\n",
        _git_command(paths, "rev-parse", "--verify", expectation.old_tag_ref): (
            expectation.old_tag_commit + "\n"
        ).encode("ascii"),
        _git_command(
            paths,
            "rev-parse",
            "--verify",
            expectation.old_tag_ref + "^{commit}",
        ): (expectation.old_tag_commit + "\n").encode("ascii"),
        _git_command(
            paths,
            "rev-parse",
            "--verify",
            expectation.old_tag_ref + "^{tree}",
        ): (expectation.old_tag_tree + "\n").encode("ascii"),
    }
    for argv, output in expected.items():
        if _command(
            runner, argv, code="staged_prefix_disposition_git_tag_unproved"
        ) != output:
            raise StagedPrefixDispositionIntegrityError(
                "staged_prefix_disposition_git_tag_mismatch"
            )


def _verify_resource_subset_absence(
    resources: object,
    *,
    paths: StagedPrefixDispositionPaths,
    runner: CommandRunner,
) -> None:
    expected = _expected_absent_resources(paths)
    if (
        type(resources) is not list
        or any(
            type(item) is not dict
            or set(item) != _ABSENT_RESOURCE_KEYS
            or type(item.get("kind")) is not str
            or type(item.get("identity")) is not str
            for item in resources
        )
        or len({(item["kind"], item["identity"]) for item in resources})
        != len(resources)
        or any(item not in expected for item in resources)
    ):
        raise StagedPrefixDispositionIntegrityError(
            "staged_prefix_disposition_absence_contract_invalid"
        )
    selected = {
        (str(item["kind"]), str(item["identity"])) for item in resources
    }
    for path in (
        Path(identity)
        for kind, identity in selected
        if kind == "path"
    ):
        try:
            path.lstat()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise StagedPrefixDispositionHostStateError(
                "staged_prefix_disposition_absence_unproved"
            ) from error
        else:
            raise StagedPrefixDispositionHostStateError(
                "staged_prefix_disposition_host_effect_present"
            )
    inventories: dict[str, set[str]] = {}
    for kind, identities in _DOCKER_IDENTITIES.items():
        selected_identities = tuple(
            identity
            for identity in identities
            if (kind, identity) in selected
        )
        if not selected_identities:
            continue
        raw = _command(
            runner,
            _DOCKER_COMMANDS[kind],
            code="staged_prefix_disposition_absence_unproved",
        )
        try:
            lines = raw.decode("ascii").splitlines()
        except UnicodeError as error:
            raise StagedPrefixDispositionHostStateError(
                "staged_prefix_disposition_absence_unproved"
            ) from error
        if any(_RESOURCE_NAME_RE.fullmatch(line) is None for line in lines):
            raise StagedPrefixDispositionHostStateError(
                "staged_prefix_disposition_absence_unproved"
            )
        inventories[kind] = set(lines)
        if any(value in inventories[kind] for value in selected_identities):
            raise StagedPrefixDispositionHostStateError(
                "staged_prefix_disposition_host_effect_present"
            )
    for identity, port in _TCP_IDENTITIES.items():
        if ("tcp_listener", identity) not in selected:
            continue
        if _command(
            runner,
            _tcp_command(port),
            code="staged_prefix_disposition_absence_unproved",
        ) != b"":
            raise StagedPrefixDispositionHostStateError(
                "staged_prefix_disposition_host_effect_present"
            )
    for service in _SYSTEMD_SERVICES:
        if ("systemd_service", service) not in selected:
            continue
        if _command(
            runner,
            _systemd_command(service),
            code="staged_prefix_disposition_absence_unproved",
        ) != b"not-found\n":
            raise StagedPrefixDispositionHostStateError(
                "staged_prefix_disposition_host_effect_present"
            )


def _verify_absence(
    resources: object,
    *,
    paths: StagedPrefixDispositionPaths,
    runner: CommandRunner,
) -> str:
    expected = _expected_absent_resources(paths)
    if resources != expected:
        raise StagedPrefixDispositionIntegrityError(
            "staged_prefix_disposition_absence_contract_invalid"
        )
    _verify_resource_subset_absence(resources, paths=paths, runner=runner)
    return _sha(canonical_json_bytes(expected))


def _load_contract(
    paths: StagedPrefixDispositionPaths,
    expectation: ReviewedStagedPrefixExpectation,
) -> tuple[dict[str, object], _StableFile]:
    stable = _read_regular(
        paths.durable_contract_path,
        mode=0o400,
        uid=paths.expected_uid,
        gid=paths.expected_gid,
        nlink=1,
        maximum=MAX_CONTRACT_BYTES,
    )
    if _sha(stable.raw) != expectation.contract_sha256:
        raise StagedPrefixDispositionIntegrityError(
            "staged_prefix_disposition_contract_hash_mismatch"
        )
    contract = _canonical_object(
        stable.raw, "staged_prefix_disposition_contract_invalid"
    )
    failed = contract.get("failed_attempt")
    tag = failed.get("tag") if type(failed) is dict else None
    successor = contract.get("corrected_successor")
    if (
        set(contract) != _CONTRACT_KEYS
        or contract.get("schema_version") != CONTRACT_SCHEMA
        or contract.get("disposition_id") != expectation.disposition_id
        or contract.get("repository_contract_source")
        != PRODUCTION_REPOSITORY_CONTRACT_SOURCE
        or contract.get("durable_contract_path")
        != str(paths.durable_contract_path)
        or contract.get("tombstone_path") != str(paths.tombstone_path)
        or type(failed) is not dict
        or set(failed) != _FAILED_ATTEMPT_KEYS
        or failed.get("generation") != PRODUCTION_FAILED_GENERATION
        or failed.get("package_manifest_sha256")
        != expectation.failed_package_manifest_sha256
        or failed.get("controller_runtime_receipt_sha256")
        != expectation.failed_controller_runtime_receipt_sha256
        or type(tag) is not dict
        or set(tag) != _TAG_KEYS
        or tag
        != {
            "ref": expectation.old_tag_ref,
            "object_type": "commit",
            "commit": expectation.old_tag_commit,
            "tree": expectation.old_tag_tree,
        }
        or type(successor) is not dict
        or set(successor) != _SUCCESSOR_KEYS
        or successor != _corrected_successor_document(expectation)
        or expectation.corrected_generation != PRODUCTION_CORRECTED_GENERATION
    ):
        raise StagedPrefixDispositionIntegrityError(
            "staged_prefix_disposition_contract_binding_invalid"
        )
    return contract, stable


def _evidence_paths(
    paths: StagedPrefixDispositionPaths,
) -> Mapping[str, Path]:
    return MappingProxyType(
        {
            "old_contract": paths.old_contract_path,
            "old_permit": paths.old_permit_path,
            "old_pre_effect_receipt": paths.old_pre_effect_receipt_path,
            "staged_capsule": paths.staged_capsule_path,
        }
    )


def _verify_evidence(
    contract: Mapping[str, object],
    paths: StagedPrefixDispositionPaths,
) -> Mapping[str, _StableFile]:
    failed = contract["failed_attempt"]
    assert isinstance(failed, dict)
    specs = failed["evidence_files"]
    if (
        type(specs) is not list
        or [item.get("role") if type(item) is dict else None for item in specs]
        != list(_FILE_ROLES)
    ):
        raise StagedPrefixDispositionIntegrityError(
            "staged_prefix_disposition_evidence_contract_invalid"
        )
    result: dict[str, _StableFile] = {}
    for spec, role in zip(specs, _FILE_ROLES, strict=True):
        path = _evidence_paths(paths)[role]
        value = _read_regular(
            path,
            mode=0o400,
            uid=paths.expected_uid,
            gid=paths.expected_gid,
            nlink=1,
            maximum=MAX_EVIDENCE_BYTES,
        )
        if not _file_spec_matches(
            spec,
            role=role,
            path=path,
            value=value,
            uid=paths.expected_uid,
            gid=paths.expected_gid,
        ):
            raise StagedPrefixDispositionIntegrityError(
                "staged_prefix_disposition_evidence_mismatch"
            )
        result[role] = value
    return MappingProxyType(result)


def _verify_empty_directories(
    contract: Mapping[str, object],
    paths: StagedPrefixDispositionPaths,
) -> Mapping[str, _StableDirectory]:
    specs = contract["empty_directories"]
    if (
        type(specs) is not list
        or [item.get("role") if type(item) is dict else None for item in specs]
        != list(_DIRECTORY_ROLES)
    ):
        raise StagedPrefixDispositionIntegrityError(
            "staged_prefix_disposition_directory_contract_invalid"
        )
    directory_paths = {
        "executions_v2": paths.executions_root,
        "store_secret": paths.store_secret_root,
    }
    result = {
        role: _verify_empty_directory(
            spec,
            role=role,
            path=directory_paths[role],
            uid=paths.expected_uid,
            gid=paths.expected_gid,
        )
        for spec, role in zip(specs, _DIRECTORY_ROLES, strict=True)
    }
    return MappingProxyType(result)


def _tombstone_for(
    contract: Mapping[str, object],
    expectation: ReviewedStagedPrefixExpectation,
) -> dict[str, object]:
    failed = contract["failed_attempt"]
    assert isinstance(failed, dict)
    tag = failed["tag"]
    assert isinstance(tag, dict)
    value: dict[str, object] = {
        "schema_version": TOMBSTONE_SCHEMA,
        "result": RESULT,
        "disposition_id": expectation.disposition_id,
        "contract_sha256": expectation.contract_sha256,
        "failed_generation": failed["generation"],
        "failed_tag_ref": tag["ref"],
        "failed_tag_commit": tag["commit"],
        "failed_tag_tree": tag["tree"],
        "failed_package_manifest_sha256": (
            expectation.failed_package_manifest_sha256
        ),
        "failed_controller_runtime_receipt_sha256": (
            expectation.failed_controller_runtime_receipt_sha256
        ),
        "failed_prefix_identity_sha256": (
            production_failed_prefix_identity_sha256()
        ),
        "failed_evidence_files_sha256": _sha(
            canonical_json_bytes(failed["evidence_files"])
        ),
        "empty_directories_sha256": _sha(
            canonical_json_bytes(contract["empty_directories"])
        ),
        "absent_resources_sha256": _sha(
            canonical_json_bytes(contract["absent_resources"])
        ),
        "corrected_generation": expectation.corrected_generation,
        "corrected_package_manifest_sha256": (
            expectation.corrected_package_manifest_sha256
        ),
        "corrected_controller_runtime_receipt_sha256": (
            expectation.corrected_controller_runtime_receipt_sha256
        ),
        "corrected_attempt_identity_sha256": (
            production_corrected_attempt_identity_sha256(
                package_manifest_sha256=(
                    expectation.corrected_package_manifest_sha256
                ),
                controller_runtime_receipt_sha256=(
                    expectation.corrected_controller_runtime_receipt_sha256
                ),
            )
        ),
        "failed_evidence_preserved_in_place": True,
        "no_store_or_service_effects_proven": True,
        "deletion_performed": False,
        "provider_calls": 0,
        "production_data_read": False,
        "activation_performed": False,
    }
    value["tombstone_sha256"] = _sha(canonical_json_bytes(value))
    return value


def _read_tombstone(
    paths: StagedPrefixDispositionPaths,
    expected: Mapping[str, object],
) -> Mapping[str, object] | None:
    try:
        paths.tombstone_path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise StagedPrefixDispositionSecurityError(
            "staged_prefix_disposition_tombstone_presence_unproved"
        ) from error
    stable = _read_regular(
        paths.tombstone_path,
        mode=0o400,
        uid=paths.expected_uid,
        gid=paths.expected_gid,
        nlink=1,
        maximum=MAX_TOMBSTONE_BYTES,
    )
    document = _canonical_object(
        stable.raw, "staged_prefix_disposition_tombstone_invalid"
    )
    if document != expected:
        raise StagedPrefixDispositionIntegrityError(
            "staged_prefix_disposition_tombstone_conflict"
        )
    return MappingProxyType(document)


def _write_create_once(
    path: Path,
    raw: bytes,
    *,
    uid: int,
    gid: int,
) -> None:
    """Create the final inode directly; never rename, unlink, or overwrite."""

    parent_fd = file_fd = -1
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow == 0:
        raise StagedPrefixDispositionSecurityError(
            "staged_prefix_disposition_nofollow_unavailable"
        )
    try:
        parent_fd = os.open(
            path.parent,
            os.O_RDONLY
            | os.O_CLOEXEC
            | nofollow
            | getattr(os, "O_DIRECTORY", 0),
        )
        parent = os.fstat(parent_fd)
        named_parent = path.parent.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or stat.S_IMODE(parent.st_mode) != 0o700
            or parent.st_uid != uid
            or parent.st_gid != gid
            or _stable_identity(parent) != _stable_identity(named_parent)
        ):
            raise StagedPrefixDispositionSecurityError(
                "staged_prefix_disposition_tombstone_parent_invalid"
            )
        file_fd = os.open(
            path.name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | nofollow,
            0o400,
            dir_fd=parent_fd,
        )
        os.fchown(file_fd, uid, gid)
        os.fchmod(file_fd, 0o400)
        view = memoryview(raw)
        while view:
            written = os.write(file_fd, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(file_fd)
        created = os.fstat(file_fd)
        named = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(created.st_mode)
            or stat.S_IMODE(created.st_mode) != 0o400
            or created.st_uid != uid
            or created.st_gid != gid
            or created.st_nlink != 1
            or created.st_size != len(raw)
            or _stable_identity(created) != _stable_identity(named)
        ):
            raise StagedPrefixDispositionSecurityError(
                "staged_prefix_disposition_tombstone_publication_failed"
            )
        os.fsync(parent_fd)
    except FileExistsError as error:
        raise StagedPrefixDispositionIntegrityError(
            "staged_prefix_disposition_tombstone_conflict"
        ) from error
    except StagedPrefixDispositionError:
        raise
    except OSError as error:
        raise StagedPrefixDispositionSecurityError(
            "staged_prefix_disposition_tombstone_publication_failed"
        ) from error
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def verify_staged_prefix_tombstone(
    paths: StagedPrefixDispositionPaths,
    expectation: ReviewedStagedPrefixExpectation,
) -> Mapping[str, object] | None:
    """Read-only verification of the exact create-once tombstone."""

    if (
        type(paths) is not StagedPrefixDispositionPaths
        or type(expectation) is not ReviewedStagedPrefixExpectation
    ):
        raise StagedPrefixDispositionSecurityError(
            "staged_prefix_disposition_inputs_invalid"
        )
    contract, _ = _load_contract(paths, expectation)
    return _read_tombstone(paths, _tombstone_for(contract, expectation))


def execute_staged_prefix_disposition(
    paths: StagedPrefixDispositionPaths,
    expectation: ReviewedStagedPrefixExpectation,
    *,
    command_runner: CommandRunner | None = None,
) -> Mapping[str, object]:
    """Prove the failed staged-only prefix and create/reverify its fence."""

    if (
        type(paths) is not StagedPrefixDispositionPaths
        or type(expectation) is not ReviewedStagedPrefixExpectation
    ):
        raise StagedPrefixDispositionSecurityError(
            "staged_prefix_disposition_inputs_invalid"
        )
    if paths.production:
        if (
            os.geteuid() != 0
            or os.getegid() != 0
            or command_runner is not None
            or expectation.old_tag_ref != PRODUCTION_OLD_TAG_REF
            or expectation.contract_sha256 != PRODUCTION_CONTRACT_SHA256
            or expectation.old_tag_commit != PRODUCTION_OLD_TAG_COMMIT
            or expectation.old_tag_tree != PRODUCTION_OLD_TAG_TREE
            or expectation.failed_package_manifest_sha256
            != PRODUCTION_FAILED_PACKAGE_MANIFEST_SHA256
            or expectation.failed_controller_runtime_receipt_sha256
            != PRODUCTION_FAILED_RUNTIME_RECEIPT_SHA256
            or expectation.corrected_generation
            != PRODUCTION_CORRECTED_GENERATION
        ):
            raise StagedPrefixDispositionSecurityError(
                "staged_prefix_disposition_production_authority_invalid"
            )
    selected_runner = command_runner or _ClosedCommandRunner(
        _allowed_commands(paths, expectation)
    )
    try:
        live_guard = GlobalExecutionLock(
            paths.live_guard_path,
            expected_uid=paths.expected_uid,
            expected_gid=paths.expected_gid,
        )
    except ExecutionLockError as error:
        raise StagedPrefixDispositionSecurityError(
            "staged_prefix_disposition_live_guard_unavailable"
        ) from error
    with live_guard:
        try:
            global_lock = GlobalExecutionLock(
                paths.global_lock_path,
                expected_uid=paths.expected_uid,
                expected_gid=paths.expected_gid,
            )
        except ExecutionLockError as error:
            raise StagedPrefixDispositionSecurityError(
                "staged_prefix_disposition_global_lock_unavailable"
            ) from error
        with global_lock:
            contract, contract_before = _load_contract(paths, expectation)
            expected_tombstone = _tombstone_for(contract, expectation)
            existing = _read_tombstone(paths, expected_tombstone)
            evidence_before = _verify_evidence(contract, paths)
            directories_before = _verify_empty_directories(contract, paths)
            _verify_git_tag(paths, expectation, runner=selected_runner)
            absence_sha256 = _verify_absence(
                contract["absent_resources"],
                paths=paths,
                runner=selected_runner,
            )
            if absence_sha256 != expected_tombstone["absent_resources_sha256"]:
                raise StagedPrefixDispositionIntegrityError(
                    "staged_prefix_disposition_absence_hash_mismatch"
                )
            if existing is None:
                _write_create_once(
                    paths.tombstone_path,
                    canonical_json_bytes(expected_tombstone),
                    uid=paths.expected_uid,
                    gid=paths.expected_gid,
                )
            contract_after_document, contract_after = _load_contract(
                paths, expectation
            )
            evidence_after = _verify_evidence(contract_after_document, paths)
            directories_after = _verify_empty_directories(
                contract_after_document, paths
            )
            _verify_git_tag(paths, expectation, runner=selected_runner)
            if (
                contract_after != contract_before
                or evidence_after != evidence_before
                or directories_after != directories_before
                or _verify_absence(
                    contract_after_document["absent_resources"],
                    paths=paths,
                    runner=selected_runner,
                )
                != absence_sha256
            ):
                raise StagedPrefixDispositionSecurityError(
                    "staged_prefix_disposition_evidence_changed"
                )
            verified = _read_tombstone(paths, expected_tombstone)
            if verified is None:
                raise StagedPrefixDispositionIntegrityError(
                    "staged_prefix_disposition_tombstone_missing"
                )
            return verified


__all__ = [
    "CONTRACT_SCHEMA",
    "RESULT",
    "ReviewedStagedPrefixExpectation",
    "StagedPrefixDispositionError",
    "StagedPrefixDispositionHostStateError",
    "StagedPrefixDispositionIntegrityError",
    "StagedPrefixDispositionPaths",
    "StagedPrefixDispositionSecurityError",
    "TOMBSTONE_SCHEMA",
    "canonical_json_bytes",
    "execute_staged_prefix_disposition",
    "production_corrected_attempt_identity_sha256",
    "production_disposition_paths",
    "production_failed_prefix_identity_sha256",
    "verify_staged_prefix_tombstone",
]
