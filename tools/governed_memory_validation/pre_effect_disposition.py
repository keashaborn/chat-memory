from __future__ import annotations

"""Fail-closed disposition of a claimed attempt that made no host effects.

The controller never repairs, deletes, renames, or opens predecessor evidence
for writing.  Under the same two locks used by the live proof it verifies an
exact repository-pinned review contract, the predecessor capsule signatures,
the complete authority database, the empty execution artifacts, and exact host
absence.  It then creates one deterministic receipt.  Old entrypoints must call
``refuse_retired_predecessor_entrypoint`` while holding their live-proof guard;
successors must call ``execute_pre_effect_disposition`` immediately before
their first effect.  The reviewed expectation is intentionally supplied by a
sealed repository caller rather than read from the live host.
"""

import base64
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
from types import MappingProxyType
from typing import Final, Mapping, Protocol, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from tools.governed_memory_install.authority_state import (
    STATE_APPLICATION_ID,
    STATE_SCHEMA_VERSION,
    nonce_sha256,
    operation_sha256,
)
from tools.governed_memory_install.execution_lock import (
    ExecutionLockError,
    GlobalExecutionLock,
)


CONTRACT_SCHEMA: Final = "governed-memory-pre-effect-disposition-contract-v1"
RECEIPT_SCHEMA: Final = "governed-memory-pre-effect-disposition-receipt-v1"
RESULT: Final = "predecessor_pre_effect_attempt_fenced_for_successor"

PRODUCTION_STATE_ROOT: Final = Path("/var/lib/governed-memory-controller")
PRODUCTION_EXECUTIONS_ROOT: Final = PRODUCTION_STATE_ROOT / "executions"
PRODUCTION_CONTRACT_ROOT: Final = PRODUCTION_STATE_ROOT / "pre-effect-dispositions"
PRODUCTION_CONTRACT_PATH: Final = (
    PRODUCTION_CONTRACT_ROOT
    / "phase9-v5-pre-effect-to-v6-000002.contract.json"
)
PRODUCTION_OLD_STORE_SPEC_PATH: Final = Path(
    "/etc/governed-memory-controller/store_spec.json"
)
PRODUCTION_GLOBAL_LOCK_PATH: Final = Path(
    "/run/lock/governed-memory-controller/execution.lock"
)
PRODUCTION_LIVE_GUARD_PATH: Final = Path(
    "/run/lock/governed-memory-controller/phase9-disposable-live-proof.lock"
)
PRODUCTION_PREDECESSOR_CAPSULE_PATH: Final = (
    PRODUCTION_STATE_ROOT / "phase9-disposable-proof-recovery-capsule.json"
)
PRODUCTION_PREDECESSOR_AUTHORITY_STATE_PATH: Final = (
    PRODUCTION_STATE_ROOT / "authority-state.sqlite3"
)
PRODUCTION_PREDECESSOR_EXECUTION_ID: Final = (
    "02e09b8837ad4dcdc85bf10dedf2a0f42ff50ac53ae138d562ab74becc2b654d"
)
PRODUCTION_PREDECESSOR_EXECUTION_DIRECTORY: Final = (
    PRODUCTION_EXECUTIONS_ROOT / PRODUCTION_PREDECESSOR_EXECUTION_ID
)
PRODUCTION_SUCCESSOR_AUTHORITY_STATE_PATH: Final = (
    PRODUCTION_STATE_ROOT / "authority-state-v2.sqlite3"
)
PRODUCTION_SUCCESSOR_CAPSULE_PATH: Final = (
    PRODUCTION_STATE_ROOT / "phase9-disposable-proof-recovery-capsule-v3.json"
)
PRODUCTION_SUCCESSOR_PROMOTABLE_RECEIPT_PATH: Final = (
    PRODUCTION_STATE_ROOT
    / "phase9-disposable-live-proof-promotable-receipt-000002.json"
)
PRODUCTION_SUCCESSOR_PROMOTABLE_RECEIPT_STAGING_PATH: Final = Path(
    str(PRODUCTION_SUCCESSOR_PROMOTABLE_RECEIPT_PATH) + ".publishing"
)
PRODUCTION_DISPOSITION_ID: Final = "phase9-v5-pre-effect-to-v6-000002"
PRODUCTION_CONTRACT_SHA256: Final[str | None] = (
    "4b5d4b9c41294247a3087d08c5278436e2a6bdfd317ab1b960a5a95189f2cacb"
)
PRODUCTION_PREDECESSOR_ATTEMPT_IDENTITY_SHA256: Final[str | None] = (
    "e814fd3ea9e10ebb2cc84f87c7a8a73f6d3d29e8da1f05944c81522b8db6efd9"
)
PRODUCTION_AUTHORIZATION_TEXT_SHA256: Final = (
    "063891fc0189b3c4a0fe393200ae50f59b04f5488800dc27a5ce3020880f3e44"
)
PRODUCTION_SUCCESSOR_GENERATION: Final = "000002"
PRODUCTION_SUCCESSOR_AUTHORIZATION_NAMESPACE: Final = (
    "governed-memory-phase9-live-proof-v2"
)
PRODUCTION_SUCCESSOR_INSTALL_SCOPE_ID: Final = (
    "phase9-disposable-live-install-000002"
)
PRODUCTION_SUCCESSOR_ROLLBACK_SCOPE_ID: Final = (
    "phase9-disposable-live-rollback-000002"
)
_SUCCESSOR_ATTEMPT_IDENTITY_SCHEMA: Final = (
    "governed-memory-pre-effect-successor-attempt-identity-v1"
)
_SUCCESSOR_ATTEMPT_IDENTITY_DOMAIN: Final = (
    b"governed-memory-pre-effect-successor-attempt-identity-v1\x00"
)
_PREDECESSOR_ATTEMPT_IDENTITY_SCHEMA: Final = (
    "governed-memory-pre-effect-predecessor-attempt-identity-v1"
)
_PREDECESSOR_ATTEMPT_IDENTITY_DOMAIN: Final = (
    b"governed-memory-pre-effect-predecessor-attempt-identity-v1\x00"
)

EMPTY_SHA256: Final = hashlib.sha256(b"").hexdigest()
MAX_CONTRACT_BYTES: Final = 256 * 1024
MAX_CAPSULE_BYTES: Final = 256 * 1024
MAX_DATABASE_BYTES: Final = 32 * 1024 * 1024
MAX_RECEIPT_BYTES: Final = 64 * 1024
MAX_DOCKER_OUTPUT_BYTES: Final = 128 * 1024
MAX_SOCKET_OUTPUT_BYTES: Final = 128 * 1024

_HASH_RE: Final = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_COMMIT_RE: Final = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_ID_RE: Final = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}\Z", re.ASCII)
_GENERATION_RE: Final = re.compile(r"[0-9]{6}\Z", re.ASCII)
_DOCKER_NAME_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z", re.ASCII)
_TIME_RE: Final = re.compile(
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z\Z",
    re.ASCII,
)

_CONTRACT_KEYS: Final = {
    "schema_version",
    "disposition_id",
    "contract_path",
    "receipt_path",
    "authorization",
    "predecessor",
    "successor",
    "host_resources",
}
_AUTHORIZATION_KEYS: Final = {
    "authorization_id",
    "thread_id",
    "authorization_text_sha256",
    "authority_materialized_at",
}
_PREDECESSOR_KEYS: Final = {
    "attempt_identity_sha256",
    "generation",
    "capsule",
    "authority_state",
    "execution",
}
_CAPSULE_CONTRACT_KEYS: Final = {
    "path",
    "sha256",
    "scalar_bindings",
    "install_authorization_payload_sha256",
    "rollback_delegation_payload_sha256",
}
_STATE_CONTRACT_KEYS: Final = {
    "path",
    "sha256",
    "application_id",
    "user_version",
    "schema_sha256",
    "nonce_claims",
    "claim_roles",
    "rollback_nonce_sha256",
    "journal_anchors",
    "resource_ledger_anchors",
    "filesystem_identity_seals",
    "state_identity",
    "database_sidecars",
}
_EXECUTION_CONTRACT_KEYS: Final = {
    "path",
    "execution_id",
    "journal_binding_sha256",
    "resource_ledger_binding_sha256",
    "members",
    "expected_absent_receipts",
}
_SUCCESSOR_KEYS: Final = {
    "attempt_identity_sha256",
    "generation",
    "authority_state_path",
    "capsule_path",
    "authorization_namespace",
    "install_scope_id",
    "rollback_scope_id",
    "physical_resources",
}
_HOST_RESOURCE_KEYS: Final = {"kind", "identity", "owner", "required_state"}
_PHYSICAL_RESOURCE_KEYS: Final = {"kind", "identity", "generation"}
_MEMBER_KEYS: Final = {
    "artifact_kind",
    "name",
    "sha256",
    "size",
    "mode",
    "device",
    "inode",
}

_NONCE_COLUMNS: Final = (
    "nonce_sha256",
    "operation_sha256",
    "execution_sha256",
    "authorization_sha256",
    "scope_sha256",
    "trust_bundle_sha256",
    "claim_sha256",
)
_SEAL_COLUMNS: Final = (
    "binding_sha256",
    "artifact_kind",
    "path_sha256",
    "directory_device",
    "directory_inode",
    "file_device",
    "file_inode",
)
_STATE_IDENTITY_COLUMNS: Final = (
    "singleton",
    "path_sha256",
    "directory_device",
    "directory_inode",
    "database_device",
    "database_inode",
)
_AUTHORITY_TABLES: Final = {
    "nonce_claim_v1",
    "journal_anchor_v1",
    "resource_ledger_anchor_v1",
    "filesystem_identity_seal_v1",
    "authority_state_identity_v1",
}
_EXPECTED_EXECUTION_MEMBERS: Final = {
    "journal": "journal.jsonl",
    "resource_ledger": "resources.jsonl",
}
_CLAIM_DOMAIN: Final = b"governed-memory-authority-claim-v1\x00"
_CLAIM_OPERATIONS: Final = {
    "install": "dormant_install",
    "recovery_reservation": "empty_store_rollback_recovery_reservation",
}
_EXPECTED_ABSENT_RECEIPTS: Final = tuple(
    sorted(
        {
            "install-receipt.json",
            "empty-rollback-eligibility-receipt.json",
            "empty-rollback-receipt.json",
            "postgres-native-install-i11-receipt.json",
            "postgres-native-install-i12-receipt.json",
            "postgres-native-install-i13-receipt.json",
            "postgres-native-install-i14-receipt.json",
            "postgres-native-rollback-i14-receipt.json",
            "postgres-native-rollback-i13-receipt.json",
            "postgres-native-rollback-i12-receipt.json",
            "postgres-native-rollback-i11-receipt.json",
        }
    )
)
_REQUIRED_TCP_LISTENERS: Final = MappingProxyType(
    {
        "127.0.0.1:55432@000001": ("predecessor", "55432"),
        "127.0.0.1:55433@000002": ("successor", "55433"),
        "127.0.0.1:6343@000001": ("predecessor", "6343"),
        "127.0.0.1:6344@000002": ("successor", "6344"),
    }
)
_PRODUCTION_SUCCESSOR_ROTATED_PHYSICAL_IDENTITIES: Final = frozenset(
    {
        ("path", "/etc/governed-memory-controller/store_spec-v2.json"),
        ("path", "/etc/systemd/system/governed-memory-stores-v2.service"),
        (
            "path",
            "/etc/systemd/system/multi-user.target.wants/"
            "governed-memory-stores-v2.service",
        ),
        ("path", str(PRODUCTION_SUCCESSOR_AUTHORITY_STATE_PATH)),
        ("path", str(PRODUCTION_STATE_ROOT / "executions-v2")),
        ("path", str(PRODUCTION_SUCCESSOR_PROMOTABLE_RECEIPT_PATH)),
        ("path", str(PRODUCTION_SUCCESSOR_CAPSULE_PATH)),
    }
)
_PRODUCTION_SUCCESSOR_TRANSIENT_REQUIRED_ABSENT_IDENTITIES: Final = frozenset(
    {
        (
            "path",
            str(PRODUCTION_SUCCESSOR_PROMOTABLE_RECEIPT_STAGING_PATH),
        )
    }
)


class PreEffectDispositionError(RuntimeError):
    """Base class for content-free disposition refusals."""


class PreEffectDispositionSecurityError(PreEffectDispositionError):
    """A path, lock, identity, or authority boundary is unsafe."""


class PreEffectDispositionIntegrityError(PreEffectDispositionError):
    """Reviewed bindings do not exactly match predecessor evidence."""


class PreEffectDispositionHostStateError(PreEffectDispositionError):
    """An exact host resource is present or cannot be proved absent."""


class PredecessorRetiredError(PreEffectDispositionError):
    """The old entrypoint is terminally fenced by a valid receipt."""


@dataclass(frozen=True, slots=True)
class DispositionPaths:
    contract_path: Path
    receipt_path: Path
    predecessor_capsule_path: Path
    predecessor_authority_state_path: Path
    predecessor_execution_directory: Path
    global_lock_path: Path
    live_guard_path: Path
    expected_uid: int
    expected_gid: int
    production: bool = False

    def __post_init__(self) -> None:
        paths = (
            self.contract_path,
            self.receipt_path,
            self.predecessor_capsule_path,
            self.predecessor_authority_state_path,
            self.predecessor_execution_directory,
            self.global_lock_path,
            self.live_guard_path,
        )
        if (
            any(not isinstance(value, Path) or not value.is_absolute() for value in paths)
            or len({str(value) for value in paths}) != len(paths)
            or type(self.expected_uid) is not int
            or self.expected_uid < 0
            or type(self.expected_gid) is not int
            or self.expected_gid < 0
            or type(self.production) is not bool
        ):
            raise PreEffectDispositionSecurityError("pre_effect_disposition_paths_invalid")
        if self.production:
            if (
                self.expected_uid != 0
                or self.expected_gid != 0
                or self.global_lock_path != PRODUCTION_GLOBAL_LOCK_PATH
                or self.live_guard_path != PRODUCTION_LIVE_GUARD_PATH
                or self.contract_path.parent != PRODUCTION_CONTRACT_ROOT
                or self.receipt_path != PRODUCTION_OLD_STORE_SPEC_PATH
                or self.predecessor_capsule_path.parent != PRODUCTION_STATE_ROOT
                or self.predecessor_authority_state_path.parent
                != PRODUCTION_STATE_ROOT
                or self.predecessor_execution_directory.parent
                != PRODUCTION_EXECUTIONS_ROOT
            ):
                raise PreEffectDispositionSecurityError(
                    "pre_effect_disposition_production_paths_invalid"
                )


@dataclass(frozen=True, slots=True)
class ReviewedDispositionExpectation:
    contract_sha256: str
    disposition_id: str
    authorization_text_sha256: str
    predecessor_attempt_identity_sha256: str
    successor_attempt_identity_sha256: str
    successor_generation: str

    def __post_init__(self) -> None:
        if (
            any(
                _HASH_RE.fullmatch(value) is None
                for value in (
                    self.contract_sha256,
                    self.authorization_text_sha256,
                    self.predecessor_attempt_identity_sha256,
                    self.successor_attempt_identity_sha256,
                )
            )
            or _ID_RE.fullmatch(self.disposition_id) is None
            or _GENERATION_RE.fullmatch(self.successor_generation) is None
        ):
            raise PreEffectDispositionIntegrityError(
                "pre_effect_disposition_expectation_invalid"
            )


class HostCommandRunner(Protocol):
    def run(self, argv: Sequence[str]) -> subprocess.CompletedProcess[bytes]: ...


class _ClosedHostCommandRunner:
    """Read-only fixed Docker and TCP-listener inventory transport."""

    __slots__ = ()

    def run(self, argv: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
        command = tuple(argv)
        if command not in _CLOSED_HOST_COMMANDS:
            raise OSError("pre_effect_disposition_host_command_refused")
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd="/",
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            timeout=15.0,
            check=False,
            close_fds=True,
        )


@dataclass(frozen=True, slots=True)
class _StableFile:
    raw: bytes
    identity: tuple[int, ...]


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
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_document_invalid"
        ) from error


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def predecessor_attempt_identity_sha256(
    *,
    generation: str,
    capsule_sha256: str,
    authority_state_sha256: str,
    execution_id: str,
    scalar_bindings: Mapping[str, object],
) -> str:
    """Derive the predecessor identity from its sealed C/T/P/R evidence."""

    candidate_git_commit = scalar_bindings.get("candidate_git_commit")
    candidate_git_tree = scalar_bindings.get("candidate_git_tree")
    package_manifest_sha256 = scalar_bindings.get("package_manifest_sha256")
    controller_runtime_receipt_sha256 = scalar_bindings.get(
        "controller_runtime_receipt_sha256"
    )
    if (
        type(generation) is not str
        or _GENERATION_RE.fullmatch(generation) is None
        or type(capsule_sha256) is not str
        or _HASH_RE.fullmatch(capsule_sha256) is None
        or type(authority_state_sha256) is not str
        or _HASH_RE.fullmatch(authority_state_sha256) is None
        or type(execution_id) is not str
        or _HASH_RE.fullmatch(execution_id) is None
        or type(candidate_git_commit) is not str
        or _COMMIT_RE.fullmatch(candidate_git_commit) is None
        or type(candidate_git_tree) is not str
        or _COMMIT_RE.fullmatch(candidate_git_tree) is None
        or type(package_manifest_sha256) is not str
        or _HASH_RE.fullmatch(package_manifest_sha256) is None
        or type(controller_runtime_receipt_sha256) is not str
        or _HASH_RE.fullmatch(controller_runtime_receipt_sha256) is None
    ):
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_predecessor_identity_inputs_invalid"
        )
    material = {
        "schema_version": _PREDECESSOR_ATTEMPT_IDENTITY_SCHEMA,
        "generation": generation,
        "candidate_git_commit": candidate_git_commit,
        "candidate_git_tree": candidate_git_tree,
        "package_manifest_sha256": package_manifest_sha256,
        "controller_runtime_receipt_sha256": (
            controller_runtime_receipt_sha256
        ),
        "capsule_sha256": capsule_sha256,
        "authority_state_sha256": authority_state_sha256,
        "execution_id": execution_id,
    }
    return _sha(
        _PREDECESSOR_ATTEMPT_IDENTITY_DOMAIN + canonical_json_bytes(material)
    )


def production_successor_attempt_identity_sha256(
    *,
    package_manifest_sha256: str,
    controller_runtime_receipt_sha256: str,
) -> str:
    """Bind the successor without creating a tracked C/T self-reference."""

    if (
        type(package_manifest_sha256) is not str
        or _HASH_RE.fullmatch(package_manifest_sha256) is None
        or type(controller_runtime_receipt_sha256) is not str
        or _HASH_RE.fullmatch(controller_runtime_receipt_sha256) is None
    ):
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_successor_identity_inputs_invalid"
        )
    material = {
        "schema_version": _SUCCESSOR_ATTEMPT_IDENTITY_SCHEMA,
        "generation": PRODUCTION_SUCCESSOR_GENERATION,
        "physical_resource_generation": PRODUCTION_SUCCESSOR_GENERATION,
        "package_manifest_sha256": package_manifest_sha256,
        "controller_runtime_receipt_sha256": (
            controller_runtime_receipt_sha256
        ),
        "authority_state_path": str(PRODUCTION_SUCCESSOR_AUTHORITY_STATE_PATH),
        "capsule_path": str(PRODUCTION_SUCCESSOR_CAPSULE_PATH),
        "authorization_namespace": (
            PRODUCTION_SUCCESSOR_AUTHORIZATION_NAMESPACE
        ),
        "install_scope_id": PRODUCTION_SUCCESSOR_INSTALL_SCOPE_ID,
        "rollback_scope_id": PRODUCTION_SUCCESSOR_ROLLBACK_SCOPE_ID,
    }
    return _sha(
        _SUCCESSOR_ATTEMPT_IDENTITY_DOMAIN + canonical_json_bytes(material)
    )


def production_disposition_paths() -> DispositionPaths:
    """Return the one exact v5-to-v6 production disposition path set."""

    return DispositionPaths(
        contract_path=PRODUCTION_CONTRACT_PATH,
        receipt_path=PRODUCTION_OLD_STORE_SPEC_PATH,
        predecessor_capsule_path=PRODUCTION_PREDECESSOR_CAPSULE_PATH,
        predecessor_authority_state_path=(
            PRODUCTION_PREDECESSOR_AUTHORITY_STATE_PATH
        ),
        predecessor_execution_directory=(
            PRODUCTION_PREDECESSOR_EXECUTION_DIRECTORY
        ),
        global_lock_path=PRODUCTION_GLOBAL_LOCK_PATH,
        live_guard_path=PRODUCTION_LIVE_GUARD_PATH,
        expected_uid=0,
        expected_gid=0,
        production=True,
    )


def _is_hash(value: object) -> bool:
    return type(value) is str and _HASH_RE.fullmatch(value) is not None


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
    expected_uid: int,
    expected_gid: int,
    maximum: int,
    allow_empty: bool = False,
) -> _StableFile:
    parent_fd = file_fd = -1
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow == 0:
        raise PreEffectDispositionSecurityError(
            "pre_effect_disposition_nofollow_unavailable"
        )
    try:
        parent_fd = os.open(
            path.parent,
            os.O_RDONLY | os.O_CLOEXEC | nofollow | getattr(os, "O_DIRECTORY", 0),
        )
        parent = os.fstat(parent_fd)
        named_parent = path.parent.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or stat.S_IMODE(parent.st_mode) != 0o700
            or parent.st_uid != expected_uid
            or parent.st_gid != expected_gid
            or _stable_identity(parent) != _stable_identity(named_parent)
        ):
            raise PreEffectDispositionSecurityError(
                "pre_effect_disposition_parent_invalid"
            )
        file_fd = os.open(path.name, os.O_RDONLY | os.O_CLOEXEC | nofollow, dir_fd=parent_fd)
        opened = os.fstat(file_fd)
        named = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != mode
            or opened.st_uid != expected_uid
            or opened.st_gid != expected_gid
            or opened.st_nlink != 1
            or opened.st_size > maximum
            or (opened.st_size == 0 and not allow_empty)
            or _stable_identity(opened) != _stable_identity(named)
        ):
            raise PreEffectDispositionSecurityError(
                "pre_effect_disposition_file_invalid"
            )
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(file_fd, min(remaining, 64 * 1024))
            if not chunk:
                raise PreEffectDispositionSecurityError(
                    "pre_effect_disposition_file_changed"
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(file_fd)
        named_after = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        parent_after = os.fstat(parent_fd)
        named_parent_after = path.parent.stat(follow_symlinks=False)
        if (
            _stable_identity(after) != _stable_identity(opened)
            or _stable_identity(named_after) != _stable_identity(opened)
            or _stable_identity(parent_after) != _stable_identity(parent)
            or _stable_identity(named_parent_after) != _stable_identity(parent)
        ):
            raise PreEffectDispositionSecurityError(
                "pre_effect_disposition_file_changed"
            )
        return _StableFile(b"".join(chunks), _stable_identity(opened))
    except PreEffectDispositionError:
        raise
    except OSError as error:
        raise PreEffectDispositionSecurityError(
            "pre_effect_disposition_file_unavailable"
        ) from error
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _parse_canonical_object(raw: bytes, code: str) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PreEffectDispositionIntegrityError(code) from error
    if type(value) is not dict or canonical_json_bytes(value) != raw:
        raise PreEffectDispositionIntegrityError(code)
    return value


def _canonical_absolute_path(value: object, code: str) -> Path:
    if type(value) is not str:
        raise PreEffectDispositionIntegrityError(code)
    selected = Path(value)
    if not selected.is_absolute() or str(selected) != value or any(
        part in {"", ".", ".."} for part in selected.parts[1:]
    ):
        raise PreEffectDispositionIntegrityError(code)
    return selected


def _parse_time(value: object) -> datetime:
    if type(value) is not str or _TIME_RE.fullmatch(value) is None:
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_authorization_invalid"
        )
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_authorization_invalid"
        ) from error


def _verify_contract(
    raw: bytes,
    *,
    paths: DispositionPaths,
    expectation: ReviewedDispositionExpectation,
) -> dict[str, object]:
    if _sha(raw) != expectation.contract_sha256:
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_contract_hash_mismatch"
        )
    document = _parse_canonical_object(raw, "pre_effect_disposition_contract_invalid")
    authorization = document.get("authorization")
    predecessor = document.get("predecessor")
    successor = document.get("successor")
    host_resources = document.get("host_resources")
    if (
        set(document) != _CONTRACT_KEYS
        or document.get("schema_version") != CONTRACT_SCHEMA
        or document.get("disposition_id") != expectation.disposition_id
        or document.get("contract_path") != str(paths.contract_path)
        or document.get("receipt_path") != str(paths.receipt_path)
        or type(authorization) is not dict
        or set(authorization) != _AUTHORIZATION_KEYS
        or _ID_RE.fullmatch(str(authorization.get("authorization_id"))) is None
        or _ID_RE.fullmatch(str(authorization.get("thread_id"))) is None
        or authorization.get("authorization_text_sha256")
        != expectation.authorization_text_sha256
        or type(predecessor) is not dict
        or set(predecessor) != _PREDECESSOR_KEYS
        or predecessor.get("attempt_identity_sha256")
        != expectation.predecessor_attempt_identity_sha256
        or _GENERATION_RE.fullmatch(str(predecessor.get("generation"))) is None
        or type(successor) is not dict
        or set(successor) != _SUCCESSOR_KEYS
        or successor.get("attempt_identity_sha256")
        != expectation.successor_attempt_identity_sha256
        or successor.get("generation") != expectation.successor_generation
        or predecessor.get("generation") == successor.get("generation")
        or type(host_resources) is not list
        or not host_resources
    ):
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_contract_binding_invalid"
        )
    _parse_time(authorization["authority_materialized_at"])

    capsule = predecessor.get("capsule")
    state_contract = predecessor.get("authority_state")
    execution = predecessor.get("execution")
    if (
        type(capsule) is not dict
        or set(capsule) != _CAPSULE_CONTRACT_KEYS
        or capsule.get("path") != str(paths.predecessor_capsule_path)
        or not _is_hash(capsule.get("sha256"))
        or type(capsule.get("scalar_bindings")) is not dict
        or not _is_hash(capsule.get("install_authorization_payload_sha256"))
        or not _is_hash(capsule.get("rollback_delegation_payload_sha256"))
        or type(state_contract) is not dict
        or set(state_contract) != _STATE_CONTRACT_KEYS
        or state_contract.get("path") != str(paths.predecessor_authority_state_path)
        or not _is_hash(state_contract.get("sha256"))
        or state_contract.get("application_id") != STATE_APPLICATION_ID
        or state_contract.get("user_version") != STATE_SCHEMA_VERSION
        or not _is_hash(state_contract.get("schema_sha256"))
        or type(execution) is not dict
        or set(execution) != _EXECUTION_CONTRACT_KEYS
        or execution.get("path") != str(paths.predecessor_execution_directory)
        or execution.get("execution_id") != paths.predecessor_execution_directory.name
        or not _is_hash(execution.get("execution_id"))
        or not _is_hash(execution.get("journal_binding_sha256"))
        or not _is_hash(execution.get("resource_ledger_binding_sha256"))
    ):
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_predecessor_contract_invalid"
        )
    if predecessor["attempt_identity_sha256"] != predecessor_attempt_identity_sha256(
        generation=str(predecessor["generation"]),
        capsule_sha256=str(capsule["sha256"]),
        authority_state_sha256=str(state_contract["sha256"]),
        execution_id=str(execution["execution_id"]),
        scalar_bindings=capsule["scalar_bindings"],
    ):
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_predecessor_identity_invalid"
        )

    successor_state = _canonical_absolute_path(
        successor.get("authority_state_path"),
        "pre_effect_disposition_successor_contract_invalid",
    )
    successor_capsule = _canonical_absolute_path(
        successor.get("capsule_path"),
        "pre_effect_disposition_successor_contract_invalid",
    )
    physical = successor.get("physical_resources")
    old_scalar = capsule["scalar_bindings"]
    if (
        successor_state == paths.predecessor_authority_state_path
        or successor_capsule == paths.predecessor_capsule_path
        or type(physical) is not list
        or not physical
        or any(
            type(successor.get(key)) is not str
            or _ID_RE.fullmatch(str(successor[key])) is None
            for key in ("authorization_namespace", "install_scope_id", "rollback_scope_id")
        )
        or any(
            successor.get(successor_key) == old_scalar.get(old_key)
            for successor_key, old_key in (
                ("authorization_namespace", "authorization_namespace"),
                ("install_scope_id", "install_scope_id"),
                ("rollback_scope_id", "rollback_scope_id"),
            )
        )
    ):
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_successor_contract_invalid"
        )
    physical_identities: set[tuple[str, str]] = set()
    for item in physical:
        if (
            type(item) is not dict
            or set(item) != _PHYSICAL_RESOURCE_KEYS
            or _ID_RE.fullmatch(str(item.get("kind"))) is None
            or type(item.get("identity")) is not str
            or not item["identity"]
            or item.get("generation") != expectation.successor_generation
            or (
                expectation.successor_generation not in str(item["identity"])
                and (
                    not paths.production
                    or (str(item["kind"]), str(item["identity"]))
                    not in _PRODUCTION_SUCCESSOR_ROTATED_PHYSICAL_IDENTITIES
                )
            )
        ):
            raise PreEffectDispositionIntegrityError(
                "pre_effect_disposition_successor_resource_invalid"
            )
        identity = (str(item["kind"]), str(item["identity"]))
        if identity in physical_identities:
            raise PreEffectDispositionIntegrityError(
                "pre_effect_disposition_successor_resource_invalid"
            )
        physical_identities.add(identity)
    if physical != sorted(physical, key=lambda item: (str(item["kind"]), str(item["identity"]))):
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_successor_resource_invalid"
        )
    probed_successor_identities = {
        (str(item.get("kind")), str(item.get("identity")))
        for item in host_resources
        if type(item) is dict and item.get("owner") == "successor"
    }
    if not physical_identities.issubset(probed_successor_identities):
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_successor_resource_unprobed"
        )
    if paths.production and (
        probed_successor_identities - physical_identities
        != _PRODUCTION_SUCCESSOR_TRANSIENT_REQUIRED_ABSENT_IDENTITIES
    ):
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_successor_resource_incomplete"
        )
    return document


def _decode_canonical_base64(value: object, size: int, code: str) -> bytes:
    try:
        if type(value) is not str:
            raise ValueError
        raw = base64.b64decode(value, validate=True)
        if len(raw) != size or base64.b64encode(raw).decode("ascii") != value:
            raise ValueError
        return raw
    except (ValueError, TypeError) as error:
        raise PreEffectDispositionIntegrityError(code) from error


def _verify_signed_payload(
    envelope: object,
    *,
    public_key: Ed25519PublicKey,
    key_id: str,
    expected_payload_sha256: str,
    code: str,
) -> dict[str, object]:
    if type(envelope) is not dict or set(envelope) != {
        "schema_version",
        "payload",
        "signature",
    }:
        raise PreEffectDispositionIntegrityError(code)
    payload = envelope.get("payload")
    signature = envelope.get("signature")
    if (
        type(envelope.get("schema_version")) is not str
        or not envelope["schema_version"]
        or type(payload) is not dict
        or type(signature) is not dict
        or set(signature) != {"algorithm", "key_id", "value_base64"}
        or signature.get("algorithm") != "Ed25519"
        or signature.get("key_id") != key_id
        or _sha(canonical_json_bytes(payload)) != expected_payload_sha256
    ):
        raise PreEffectDispositionIntegrityError(code)
    raw_signature = _decode_canonical_base64(signature.get("value_base64"), 64, code)
    try:
        public_key.verify(raw_signature, canonical_json_bytes(payload))
    except InvalidSignature as error:
        raise PreEffectDispositionIntegrityError(code) from error
    return payload


def _verify_capsule(
    raw: bytes,
    *,
    contract: Mapping[str, object],
    expectation: ReviewedDispositionExpectation,
) -> tuple[dict[str, object], dict[str, object]]:
    predecessor = contract["predecessor"]
    assert isinstance(predecessor, dict)
    capsule_contract = predecessor["capsule"]
    execution_contract = predecessor["execution"]
    assert isinstance(capsule_contract, dict)
    assert isinstance(execution_contract, dict)
    if _sha(raw) != capsule_contract["sha256"]:
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_capsule_hash_mismatch"
        )
    document = _parse_canonical_object(raw, "pre_effect_disposition_capsule_invalid")
    scalar_bindings = {
        key: value
        for key, value in document.items()
        if type(value) in {str, int, bool} or value is None
    }
    if (
        scalar_bindings != capsule_contract["scalar_bindings"]
        or document.get("authorization_text_sha256")
        != expectation.authorization_text_sha256
        or document.get("single_use") is not True
    ):
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_capsule_binding_mismatch"
        )
    public_raw = _decode_canonical_base64(
        document.get("public_key_base64"), 32, "pre_effect_disposition_capsule_key_invalid"
    )
    key_id = _sha(public_raw)
    trust = document.get("trust_bundle")
    if (
        document.get("key_id") != key_id
        or type(trust) is not dict
        or set(trust) != {"schema_version", "authorization_namespace", "keys"}
        or trust.get("authorization_namespace") != document.get("authorization_namespace")
        or trust.get("keys")
        != [
            {
                "key_id": key_id,
                "algorithm": "Ed25519",
                "public_key_base64": document["public_key_base64"],
            }
        ]
    ):
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_capsule_key_invalid"
        )
    public_key = Ed25519PublicKey.from_public_bytes(public_raw)
    install_payload = _verify_signed_payload(
        document.get("install_authorization"),
        public_key=public_key,
        key_id=key_id,
        expected_payload_sha256=str(
            capsule_contract["install_authorization_payload_sha256"]
        ),
        code="pre_effect_disposition_install_authorization_invalid",
    )
    rollback_payload = _verify_signed_payload(
        document.get("rollback_delegation"),
        public_key=public_key,
        key_id=key_id,
        expected_payload_sha256=str(capsule_contract["rollback_delegation_payload_sha256"]),
        code="pre_effect_disposition_rollback_delegation_invalid",
    )
    install_scope = document.get("install_scope")
    if (
        type(install_scope) is not dict
        or install_payload.get("authorization_namespace")
        != document.get("authorization_namespace")
        or install_payload.get("thread_id") != document.get("thread_id")
        or install_payload.get("scope_id") != document.get("install_scope_id")
        or install_payload.get("scope_sha256") != _sha(canonical_json_bytes(install_scope))
        or install_payload.get("key_id") != key_id
        or install_payload.get("single_use") is not True
        or type(install_payload.get("nonce")) is not str
        or len(str(install_payload["nonce"])) < 32
    ):
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_install_authorization_invalid"
        )
    if (
        rollback_payload.get("authorization_namespace")
        != document.get("authorization_namespace")
        or rollback_payload.get("thread_id") != document.get("thread_id")
        or rollback_payload.get("install_scope_id") != document.get("install_scope_id")
        or rollback_payload.get("rollback_scope_id") != document.get("rollback_scope_id")
        or rollback_payload.get("authorization_text_sha256")
        != expectation.authorization_text_sha256
        or rollback_payload.get("installation_execution_id")
        != execution_contract["execution_id"]
        or rollback_payload.get("key_id") != key_id
        or rollback_payload.get("single_use") is not True
        or rollback_payload.get("empty_only") is not True
        or rollback_payload.get("activation_allowed") is not False
        or rollback_payload.get("production_data_read") is not False
        or rollback_payload.get("provider_calls") != 0
        or rollback_payload.get("source_postgres_read_count") != 0
        or any(
            type(rollback_payload.get(key)) is not str
            or len(str(rollback_payload[key])) < 32
            for key in ("rollback_nonce", "recovery_reservation_nonce")
        )
    ):
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_rollback_delegation_invalid"
        )
    for payload in (install_payload, rollback_payload):
        issued = _parse_time(payload.get("issued_at"))
        not_before = _parse_time(payload.get("not_before"))
        expires = _parse_time(payload.get("expires_at"))
        if not issued <= not_before < expires:
            raise PreEffectDispositionIntegrityError(
                "pre_effect_disposition_capsule_time_invalid"
            )
    return install_payload, rollback_payload


def _schema_rows(connection: sqlite3.Connection) -> list[dict[str, object]]:
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    return [
        {"type": row[0], "name": row[1], "table": row[2], "sql": row[3]}
        for row in rows
    ]


def _dict_rows(
    connection: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
) -> list[dict[str, object]]:
    selected = connection.execute(
        f"SELECT {', '.join(columns)} FROM {table}"  # fixed internal identifiers
    ).fetchall()
    rows = [dict(zip(columns, row, strict=True)) for row in selected]
    return sorted(rows, key=canonical_json_bytes)


def _verify_authority_state(
    path: Path,
    *,
    contract: Mapping[str, object],
    paths: DispositionPaths,
    install_payload: Mapping[str, object],
    rollback_payload: Mapping[str, object],
) -> _StableFile:
    predecessor = contract["predecessor"]
    assert isinstance(predecessor, dict)
    state_contract = predecessor["authority_state"]
    assert isinstance(state_contract, dict)
    if state_contract.get("database_sidecars") != []:
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_database_sidecar_contract_invalid"
        )
    for suffix in ("-journal", "-wal", "-shm"):
        try:
            (Path(str(path) + suffix)).lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise PreEffectDispositionSecurityError(
                "pre_effect_disposition_database_sidecar_invalid"
            ) from error
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_database_sidecar_present"
        )
    opened = _read_regular(
        path,
        mode=0o600,
        expected_uid=paths.expected_uid,
        expected_gid=paths.expected_gid,
        maximum=MAX_DATABASE_BYTES,
    )
    if _sha(opened.raw) != state_contract["sha256"]:
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_authority_state_hash_mismatch"
        )
    uri = path.as_uri() + "?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True, isolation_level=None)
        try:
            connection.execute("PRAGMA query_only=ON")
            application_id = connection.execute("PRAGMA application_id").fetchone()[0]
            user_version = connection.execute("PRAGMA user_version").fetchone()[0]
            quick_check = connection.execute("PRAGMA quick_check").fetchall()
            schema_rows = _schema_rows(connection)
            table_names = {
                str(item["name"]) for item in schema_rows if item["type"] == "table"
            }
            if (
                application_id != state_contract["application_id"]
                or user_version != state_contract["user_version"]
                or quick_check != [("ok",)]
                or table_names != _AUTHORITY_TABLES
                or any(item["type"] != "table" for item in schema_rows)
                or _sha(canonical_json_bytes(schema_rows))
                != state_contract["schema_sha256"]
            ):
                raise PreEffectDispositionIntegrityError(
                    "pre_effect_disposition_authority_state_schema_invalid"
                )
            claims = _dict_rows(connection, "nonce_claim_v1", _NONCE_COLUMNS)
            journal_anchors = _dict_rows(
                connection,
                "journal_anchor_v1",
                ("binding_sha256", "sequence", "head_sha256"),
            )
            resource_anchors = _dict_rows(
                connection,
                "resource_ledger_anchor_v1",
                ("binding_sha256", "sequence", "head_sha256"),
            )
            seals = _dict_rows(connection, "filesystem_identity_seal_v1", _SEAL_COLUMNS)
            identities = _dict_rows(
                connection, "authority_state_identity_v1", _STATE_IDENTITY_COLUMNS
            )
        finally:
            connection.close()
    except PreEffectDispositionError:
        raise
    except sqlite3.DatabaseError as error:
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_authority_state_invalid"
        ) from error
    if (
        journal_anchors
        or resource_anchors
        or state_contract.get("journal_anchors") != []
        or state_contract.get("resource_ledger_anchors") != []
        or claims != state_contract.get("nonce_claims")
        or seals != state_contract.get("filesystem_identity_seals")
        or len(identities) != 1
        or identities[0] != state_contract.get("state_identity")
    ):
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_authority_state_rows_invalid"
        )
    if (
        any(
            not all(_is_hash(row.get(column)) for column in _NONCE_COLUMNS)
            for row in claims
        )
        or any(
            row.get("artifact_kind") not in {"journal", "resource_ledger"}
            or not _is_hash(row.get("binding_sha256"))
            or not _is_hash(row.get("path_sha256"))
            for row in seals
        )
    ):
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_authority_state_rows_invalid"
        )
    for row in claims:
        material = b"\x00".join(
            str(row[column]).encode("ascii") for column in _NONCE_COLUMNS[:-1]
        )
        if row["claim_sha256"] != _sha(_CLAIM_DOMAIN + material):
            raise PreEffectDispositionIntegrityError(
                "pre_effect_disposition_authority_claim_integrity_invalid"
            )
    claim_roles = state_contract.get("claim_roles")
    expected_roles = {
        "install": nonce_sha256(str(install_payload["nonce"])),
        "recovery_reservation": nonce_sha256(
            str(rollback_payload["recovery_reservation_nonce"])
        ),
    }
    rollback_hash = nonce_sha256(str(rollback_payload["rollback_nonce"]))
    observed_nonce_hashes = {str(row["nonce_sha256"]) for row in claims}
    if (
        claim_roles != expected_roles
        or observed_nonce_hashes != set(expected_roles.values())
        or state_contract.get("rollback_nonce_sha256") != rollback_hash
        or rollback_hash in observed_nonce_hashes
    ):
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_authority_claim_set_invalid"
        )
    by_nonce = {str(row["nonce_sha256"]): row for row in claims}
    if any(
        by_nonce[nonce_hash]["operation_sha256"]
        != operation_sha256(_CLAIM_OPERATIONS[role])
        for role, nonce_hash in expected_roles.items()
    ):
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_authority_claim_role_invalid"
        )
    identity = identities[0]
    parent = path.parent.stat(follow_symlinks=False)
    database = path.stat(follow_symlinks=False)
    if identity != {
        "singleton": 1,
        "path_sha256": _sha(str(path).encode("utf-8")),
        "directory_device": parent.st_dev,
        "directory_inode": parent.st_ino,
        "database_device": database.st_dev,
        "database_inode": database.st_ino,
    }:
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_authority_state_identity_invalid"
        )
    after = _read_regular(
        path,
        mode=0o600,
        expected_uid=paths.expected_uid,
        expected_gid=paths.expected_gid,
        maximum=MAX_DATABASE_BYTES,
    )
    if after.identity != opened.identity or after.raw != opened.raw:
        raise PreEffectDispositionSecurityError(
            "pre_effect_disposition_authority_state_changed"
        )
    return opened


def _verify_execution(
    *,
    contract: Mapping[str, object],
    paths: DispositionPaths,
) -> tuple[tuple[int, ...], tuple[tuple[str, tuple[int, ...]], ...]]:
    predecessor = contract["predecessor"]
    assert isinstance(predecessor, dict)
    execution = predecessor["execution"]
    state_contract = predecessor["authority_state"]
    assert isinstance(execution, dict)
    assert isinstance(state_contract, dict)
    directory = paths.predecessor_execution_directory
    directory_fd = -1
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(
            directory,
            os.O_RDONLY | os.O_CLOEXEC | nofollow | getattr(os, "O_DIRECTORY", 0),
        )
        opened_dir = os.fstat(directory_fd)
        named_dir = directory.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened_dir.st_mode)
            or stat.S_IMODE(opened_dir.st_mode) != 0o700
            or opened_dir.st_uid != paths.expected_uid
            or opened_dir.st_gid != paths.expected_gid
            or _stable_identity(opened_dir) != _stable_identity(named_dir)
        ):
            raise PreEffectDispositionSecurityError(
                "pre_effect_disposition_execution_directory_invalid"
            )
        names = sorted(os.listdir(directory_fd))
        if names != sorted(_EXPECTED_EXECUTION_MEMBERS.values()):
            raise PreEffectDispositionIntegrityError(
                "pre_effect_disposition_execution_members_invalid"
            )
        members = execution.get("members")
        if (
            type(members) is not list
            or len(members) != 2
            or execution.get("expected_absent_receipts")
            != list(_EXPECTED_ABSENT_RECEIPTS)
        ):
            raise PreEffectDispositionIntegrityError(
                "pre_effect_disposition_execution_contract_invalid"
            )
        by_kind: dict[str, dict[str, object]] = {}
        for item in members:
            if type(item) is not dict or set(item) != _MEMBER_KEYS:
                raise PreEffectDispositionIntegrityError(
                    "pre_effect_disposition_execution_contract_invalid"
                )
            by_kind[str(item.get("artifact_kind"))] = item
        if set(by_kind) != set(_EXPECTED_EXECUTION_MEMBERS):
            raise PreEffectDispositionIntegrityError(
                "pre_effect_disposition_execution_contract_invalid"
            )
        observed_members: list[tuple[str, tuple[int, ...]]] = []
        seals = state_contract.get("filesystem_identity_seals")
        if type(seals) is not list or len(seals) != 2:
            raise PreEffectDispositionIntegrityError(
                "pre_effect_disposition_filesystem_seals_invalid"
            )
        for kind, name in _EXPECTED_EXECUTION_MEMBERS.items():
            expected = by_kind[kind]
            target = directory / name
            stable = _read_regular(
                target,
                mode=0o600,
                expected_uid=paths.expected_uid,
                expected_gid=paths.expected_gid,
                maximum=1,
                allow_empty=True,
            )
            file_stat = target.stat(follow_symlinks=False)
            if expected != {
                "artifact_kind": kind,
                "name": name,
                "sha256": EMPTY_SHA256,
                "size": 0,
                "mode": 0o600,
                "device": file_stat.st_dev,
                "inode": file_stat.st_ino,
            } or stable.raw != b"":
                raise PreEffectDispositionIntegrityError(
                    "pre_effect_disposition_execution_not_empty"
                )
            matching_seals = [row for row in seals if row.get("artifact_kind") == kind]
            binding_key = (
                "journal_binding_sha256"
                if kind == "journal"
                else "resource_ledger_binding_sha256"
            )
            if len(matching_seals) != 1 or matching_seals[0] != {
                "binding_sha256": execution[binding_key],
                "artifact_kind": kind,
                "path_sha256": _sha(str(target).encode("utf-8")),
                "directory_device": opened_dir.st_dev,
                "directory_inode": opened_dir.st_ino,
                "file_device": file_stat.st_dev,
                "file_inode": file_stat.st_ino,
            }:
                raise PreEffectDispositionIntegrityError(
                    "pre_effect_disposition_filesystem_seals_invalid"
                )
            observed_members.append((kind, stable.identity))
        named_after = directory.stat(follow_symlinks=False)
        if _stable_identity(named_after) != _stable_identity(opened_dir):
            raise PreEffectDispositionSecurityError(
                "pre_effect_disposition_execution_directory_changed"
            )
        return _stable_identity(opened_dir), tuple(observed_members)
    except PreEffectDispositionError:
        raise
    except OSError as error:
        raise PreEffectDispositionSecurityError(
            "pre_effect_disposition_execution_unavailable"
        ) from error
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)


_DOCKER_COMMANDS: Final = {
    "docker_container": (
        "/usr/bin/docker",
        "container",
        "ls",
        "--all",
        "--no-trunc",
        "--format",
        "{{.Names}}",
    ),
    "docker_volume": (
        "/usr/bin/docker",
        "volume",
        "ls",
        "--format",
        "{{.Name}}",
    ),
    "docker_network": (
        "/usr/bin/docker",
        "network",
        "ls",
        "--no-trunc",
        "--format",
        "{{.Name}}",
    ),
}
_TCP_LISTENER_COMMANDS: Final = MappingProxyType(
    {
        identity: (
            "/usr/bin/ss",
            "--tcp",
            "--listening",
            "--numeric",
            "--no-header",
            "sport",
            "=",
            f":{port}",
        )
        for identity, (_, port) in _REQUIRED_TCP_LISTENERS.items()
    }
)
_CLOSED_HOST_COMMANDS: Final = frozenset(
    (*_DOCKER_COMMANDS.values(), *_TCP_LISTENER_COMMANDS.values())
)


def _verify_host_absence(
    resources: object,
    *,
    runner: HostCommandRunner,
) -> str:
    if type(resources) is not list or not resources:
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_host_contract_invalid"
        )
    ordered = sorted(resources, key=lambda item: (str(item.get("kind")), str(item.get("identity"))))
    if resources != ordered:
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_host_contract_invalid"
        )
    inventories: dict[str, set[str]] = {}
    seen: set[tuple[str, str]] = set()
    reviewed_tcp_listeners: dict[str, str] = {}
    for item in resources:
        if (
            type(item) is not dict
            or set(item) != _HOST_RESOURCE_KEYS
            or item.get("owner") not in {"predecessor", "successor"}
            or item.get("required_state") != "absent"
            or item.get("kind") not in {"path", "tcp_listener", *_DOCKER_COMMANDS}
            or type(item.get("identity")) is not str
            or not item["identity"]
        ):
            raise PreEffectDispositionIntegrityError(
                "pre_effect_disposition_host_contract_invalid"
            )
        identity = (str(item["kind"]), str(item["identity"]))
        if identity in seen:
            raise PreEffectDispositionIntegrityError(
                "pre_effect_disposition_host_contract_invalid"
            )
        seen.add(identity)
        if item["kind"] == "tcp_listener":
            listener_identity = str(item["identity"])
            expected = _REQUIRED_TCP_LISTENERS.get(listener_identity)
            if expected is None or item["owner"] != expected[0]:
                raise PreEffectDispositionIntegrityError(
                    "pre_effect_disposition_host_contract_invalid"
                )
            reviewed_tcp_listeners[listener_identity] = str(item["owner"])
            try:
                completed = runner.run(_TCP_LISTENER_COMMANDS[listener_identity])
            except (OSError, subprocess.SubprocessError) as error:
                raise PreEffectDispositionHostStateError(
                    "pre_effect_disposition_host_absence_unproved"
                ) from error
            if (
                type(completed) is not subprocess.CompletedProcess
                or completed.returncode != 0
                or type(completed.stdout) is not bytes
                or type(completed.stderr) is not bytes
                or completed.stderr != b""
                or len(completed.stdout) > MAX_SOCKET_OUTPUT_BYTES
            ):
                raise PreEffectDispositionHostStateError(
                    "pre_effect_disposition_host_absence_unproved"
                )
            if completed.stdout != b"":
                raise PreEffectDispositionHostStateError(
                    "pre_effect_disposition_host_resource_present"
                )
            continue
        if item["kind"] == "path":
            path = _canonical_absolute_path(
                item["identity"], "pre_effect_disposition_host_contract_invalid"
            )
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise PreEffectDispositionHostStateError(
                    "pre_effect_disposition_host_absence_unproved"
                ) from error
            raise PreEffectDispositionHostStateError(
                "pre_effect_disposition_host_resource_present"
            )
        kind = str(item["kind"])
        name = str(item["identity"])
        if _DOCKER_NAME_RE.fullmatch(name) is None:
            raise PreEffectDispositionIntegrityError(
                "pre_effect_disposition_host_contract_invalid"
            )
        if kind not in inventories:
            try:
                completed = runner.run(_DOCKER_COMMANDS[kind])
            except (OSError, subprocess.SubprocessError) as error:
                raise PreEffectDispositionHostStateError(
                    "pre_effect_disposition_host_absence_unproved"
                ) from error
            if (
                type(completed) is not subprocess.CompletedProcess
                or completed.returncode != 0
                or type(completed.stdout) is not bytes
                or type(completed.stderr) is not bytes
                or completed.stderr != b""
                or len(completed.stdout) > MAX_DOCKER_OUTPUT_BYTES
            ):
                raise PreEffectDispositionHostStateError(
                    "pre_effect_disposition_host_absence_unproved"
                )
            try:
                lines = completed.stdout.decode("ascii").splitlines()
            except UnicodeError as error:
                raise PreEffectDispositionHostStateError(
                    "pre_effect_disposition_host_absence_unproved"
                ) from error
            if any(_DOCKER_NAME_RE.fullmatch(line) is None for line in lines):
                raise PreEffectDispositionHostStateError(
                    "pre_effect_disposition_host_absence_unproved"
                )
            inventories[kind] = set(lines)
        if name in inventories[kind]:
            raise PreEffectDispositionHostStateError(
                "pre_effect_disposition_host_resource_present"
            )
    if reviewed_tcp_listeners != {
        identity: owner
        for identity, (owner, _) in _REQUIRED_TCP_LISTENERS.items()
    }:
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_host_contract_invalid"
        )
    return _sha(canonical_json_bytes(resources))


def _receipt_for(
    contract: Mapping[str, object],
    *,
    contract_sha256: str,
    host_resources_sha256: str,
) -> dict[str, object]:
    predecessor = contract["predecessor"]
    successor = contract["successor"]
    authorization = contract["authorization"]
    assert isinstance(predecessor, dict)
    assert isinstance(successor, dict)
    assert isinstance(authorization, dict)
    capsule = predecessor["capsule"]
    state_contract = predecessor["authority_state"]
    execution = predecessor["execution"]
    assert isinstance(capsule, dict)
    assert isinstance(state_contract, dict)
    assert isinstance(execution, dict)
    receipt: dict[str, object] = {
        "schema_version": RECEIPT_SCHEMA,
        "result": RESULT,
        "disposition_id": contract["disposition_id"],
        "contract_sha256": contract_sha256,
        "authorization_text_sha256": authorization["authorization_text_sha256"],
        "predecessor_attempt_identity_sha256": predecessor[
            "attempt_identity_sha256"
        ],
        "predecessor_capsule_sha256": capsule["sha256"],
        "predecessor_authority_state_sha256": state_contract["sha256"],
        "predecessor_execution_id": execution["execution_id"],
        "successor_attempt_identity_sha256": successor["attempt_identity_sha256"],
        "successor_generation": successor["generation"],
        "successor_authority_state_path": successor["authority_state_path"],
        "successor_capsule_path": successor["capsule_path"],
        "successor_authorization_namespace": successor["authorization_namespace"],
        "successor_install_scope_id": successor["install_scope_id"],
        "successor_rollback_scope_id": successor["rollback_scope_id"],
        "successor_physical_resources_sha256": _sha(
            canonical_json_bytes(successor["physical_resources"])
        ),
        "host_resources_sha256": host_resources_sha256,
        "no_host_effects_proven": True,
        "predecessor_evidence_preserved_in_place": True,
        "predecessor_state_mutated": False,
        "deletion_performed": False,
        "provider_calls": 0,
        "production_data_read": False,
        "activation_performed": False,
    }
    receipt["receipt_sha256"] = _sha(canonical_json_bytes(receipt))
    return receipt


def _write_create_once(
    path: Path,
    raw: bytes,
    *,
    expected_uid: int,
    expected_gid: int,
) -> None:
    parent_fd = file_fd = -1
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    staging_name = "." + path.name + ".publishing"

    def read_member(
        name: str, allowed_links: frozenset[int]
    ) -> tuple[bytes, tuple[int, int]] | None:
        descriptor = -1
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | nofollow
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            return None
        except OSError as error:
            raise PreEffectDispositionSecurityError(
                "pre_effect_disposition_receipt_publication_conflict"
            ) from error
        try:
            before = os.fstat(descriptor)
            named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(before.st_mode)
                or stat.S_IMODE(before.st_mode) != 0o400
                or before.st_uid != expected_uid
                or before.st_gid != expected_gid
                or before.st_nlink not in allowed_links
                or before.st_size <= 0
                or before.st_size > MAX_RECEIPT_BYTES
                or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
            ):
                raise PreEffectDispositionSecurityError(
                    "pre_effect_disposition_receipt_publication_conflict"
                )
            content = bytearray()
            while len(content) <= MAX_RECEIPT_BYTES:
                block = os.read(
                    descriptor,
                    min(65536, MAX_RECEIPT_BYTES + 1 - len(content)),
                )
                if not block:
                    break
                content.extend(block)
            after = os.fstat(descriptor)
            named_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                len(content) != before.st_size
                or _stable_identity(after) != _stable_identity(before)
                or _stable_identity(named_after) != _stable_identity(before)
            ):
                raise PreEffectDispositionSecurityError(
                    "pre_effect_disposition_receipt_publication_conflict"
                )
            return bytes(content), (before.st_dev, before.st_ino)
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def unlink_staging(expected_inode: tuple[int, int]) -> None:
        try:
            named = os.stat(
                staging_name, dir_fd=parent_fd, follow_symlinks=False
            )
            if (named.st_dev, named.st_ino) != expected_inode:
                raise PreEffectDispositionSecurityError(
                    "pre_effect_disposition_receipt_publication_conflict"
                )
            os.unlink(staging_name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except PreEffectDispositionError:
            raise
        except OSError as error:
            raise PreEffectDispositionSecurityError(
                "pre_effect_disposition_receipt_write_failed"
            ) from error

    def fsync_member(
        name: str,
        expected_inode: tuple[int, int],
        *,
        expected_link_count: int,
    ) -> None:
        descriptor = -1
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | nofollow
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=parent_fd,
            )
            before = os.fstat(descriptor)
            named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(before.st_mode)
                or stat.S_IMODE(before.st_mode) != 0o400
                or before.st_uid != expected_uid
                or before.st_gid != expected_gid
                or before.st_nlink != expected_link_count
                or (before.st_dev, before.st_ino) != expected_inode
                or (named.st_dev, named.st_ino) != expected_inode
            ):
                raise PreEffectDispositionSecurityError(
                    "pre_effect_disposition_receipt_publication_conflict"
                )
            os.fsync(descriptor)
            after = os.fstat(descriptor)
            named_after = os.stat(
                name, dir_fd=parent_fd, follow_symlinks=False
            )
            if (
                _stable_identity(after) != _stable_identity(before)
                or _stable_identity(named_after) != _stable_identity(before)
            ):
                raise PreEffectDispositionSecurityError(
                    "pre_effect_disposition_receipt_publication_conflict"
                )
            closing_fd = descriptor
            descriptor = -1
            os.close(closing_fd)
        except PreEffectDispositionError:
            raise
        except OSError as error:
            raise PreEffectDispositionSecurityError(
                "pre_effect_disposition_receipt_write_failed"
            ) from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    try:
        parent_fd = os.open(
            path.parent,
            os.O_RDONLY | os.O_CLOEXEC | nofollow | getattr(os, "O_DIRECTORY", 0),
        )
        parent = os.fstat(parent_fd)
        named_parent = path.parent.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or stat.S_IMODE(parent.st_mode) != 0o700
            or parent.st_uid != expected_uid
            or parent.st_gid != expected_gid
            or _stable_identity(parent) != _stable_identity(named_parent)
        ):
            raise PreEffectDispositionSecurityError(
                "pre_effect_disposition_receipt_parent_invalid"
            )
        final = read_member(path.name, frozenset({1, 2}))
        staging = read_member(
            staging_name,
            frozenset({2}) if final is not None else frozenset({1}),
        )
        if final is not None:
            final_raw, final_inode = final
            if final_raw != raw:
                raise PreEffectDispositionIntegrityError(
                    "pre_effect_disposition_receipt_conflict"
                )
            if staging is None:
                if os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False).st_nlink != 1:
                    raise PreEffectDispositionSecurityError(
                        "pre_effect_disposition_receipt_publication_conflict"
                    )
                return
            staging_raw, staging_inode = staging
            if staging_raw != raw or staging_inode != final_inode:
                raise PreEffectDispositionSecurityError(
                    "pre_effect_disposition_receipt_publication_conflict"
                )
            fsync_member(path.name, final_inode, expected_link_count=2)
            os.fsync(parent_fd)
            unlink_staging(staging_inode)
            if read_member(path.name, frozenset({1})) != (raw, final_inode):
                raise PreEffectDispositionSecurityError(
                    "pre_effect_disposition_receipt_write_failed"
                )
            return
        if staging is not None:
            staging_raw, staging_inode = staging
            if staging_raw != raw:
                raise PreEffectDispositionSecurityError(
                    "pre_effect_disposition_receipt_publication_conflict"
                )
            fsync_member(staging_name, staging_inode, expected_link_count=1)
            os.fsync(parent_fd)
            try:
                os.link(
                    staging_name, path.name,
                    src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as error:
                raise PreEffectDispositionSecurityError(
                    "pre_effect_disposition_receipt_publication_conflict"
                ) from error
            os.fsync(parent_fd)
            if read_member(path.name, frozenset({2})) != (raw, staging_inode):
                raise PreEffectDispositionSecurityError(
                    "pre_effect_disposition_receipt_write_failed"
                )
            unlink_staging(staging_inode)
            if read_member(path.name, frozenset({1})) != (raw, staging_inode):
                raise PreEffectDispositionSecurityError(
                    "pre_effect_disposition_receipt_write_failed"
                )
            return
        file_fd = os.open(
            staging_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | nofollow,
            0o400,
            dir_fd=parent_fd,
        )
        os.fchown(file_fd, expected_uid, expected_gid)
        os.fchmod(file_fd, 0o400)
        view = memoryview(raw)
        while view:
            written = os.write(file_fd, view)
            if written <= 0:
                raise OSError
            view = view[written:]
        os.fsync(file_fd)
        staged = os.fstat(file_fd)
        staging_inode = (staged.st_dev, staged.st_ino)
        closing_fd = file_fd
        file_fd = -1
        os.close(closing_fd)
        os.fsync(parent_fd)
        if read_member(staging_name, frozenset({1})) != (raw, staging_inode):
            raise PreEffectDispositionSecurityError(
                "pre_effect_disposition_receipt_write_failed"
            )
        try:
            os.link(
                staging_name, path.name,
                src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError as error:
            raise PreEffectDispositionSecurityError(
                "pre_effect_disposition_receipt_publication_conflict"
            ) from error
        os.fsync(parent_fd)
        if read_member(path.name, frozenset({2})) != (raw, staging_inode):
            raise PreEffectDispositionSecurityError(
                "pre_effect_disposition_receipt_write_failed"
            )
        unlink_staging(staging_inode)
        if read_member(path.name, frozenset({1})) != (raw, staging_inode):
            raise PreEffectDispositionSecurityError(
                "pre_effect_disposition_receipt_write_failed"
            )
    except PreEffectDispositionError:
        raise
    except OSError as error:
        raise PreEffectDispositionSecurityError(
            "pre_effect_disposition_receipt_write_failed"
        ) from error
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _load_contract_and_expected_receipt(
    paths: DispositionPaths,
    expectation: ReviewedDispositionExpectation,
) -> tuple[dict[str, object], dict[str, object]]:
    contract_file = _read_regular(
        paths.contract_path,
        mode=0o400,
        expected_uid=paths.expected_uid,
        expected_gid=paths.expected_gid,
        maximum=MAX_CONTRACT_BYTES,
    )
    contract = _verify_contract(contract_file.raw, paths=paths, expectation=expectation)
    receipt = _receipt_for(
        contract,
        contract_sha256=expectation.contract_sha256,
        host_resources_sha256=_sha(canonical_json_bytes(contract["host_resources"])),
    )
    return contract, receipt


def verify_disposition_receipt(
    paths: DispositionPaths,
    expectation: ReviewedDispositionExpectation,
) -> Mapping[str, object] | None:
    """Verify a fixed receipt without creating one; invalid presence fails closed."""

    contract, expected = _load_contract_and_expected_receipt(paths, expectation)
    del contract
    try:
        paths.receipt_path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise PreEffectDispositionSecurityError(
            "pre_effect_disposition_receipt_presence_unproved"
        ) from error
    received = _read_regular(
        paths.receipt_path,
        mode=0o400,
        expected_uid=paths.expected_uid,
        expected_gid=paths.expected_gid,
        maximum=MAX_RECEIPT_BYTES,
    )
    document = _parse_canonical_object(
        received.raw, "pre_effect_disposition_receipt_invalid"
    )
    if document != expected:
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_receipt_binding_invalid"
        )
    return MappingProxyType(document)


def require_production_disposition_receipt(
    *,
    package_manifest_sha256: str,
    controller_runtime_receipt_sha256: str,
) -> Mapping[str, object]:
    """Read-only mandatory fence before any v6 proof-state mutation.

    Contract authority is repository-pinned and cannot be supplied by the
    operator.  C/T are proved independently by the issuer; this identity binds
    exact P/R plus the fixed successor namespace, scopes, paths, and generation.
    """

    contract_sha256 = PRODUCTION_CONTRACT_SHA256
    predecessor_attempt_identity_sha256 = (
        PRODUCTION_PREDECESSOR_ATTEMPT_IDENTITY_SHA256
    )
    if (
        not _is_hash(contract_sha256)
        or not _is_hash(predecessor_attempt_identity_sha256)
    ):
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_production_binding_not_sealed"
        )
    assert isinstance(contract_sha256, str)
    assert isinstance(predecessor_attempt_identity_sha256, str)
    successor_identity = production_successor_attempt_identity_sha256(
        package_manifest_sha256=package_manifest_sha256,
        controller_runtime_receipt_sha256=(
            controller_runtime_receipt_sha256
        ),
    )
    expectation = ReviewedDispositionExpectation(
        contract_sha256=contract_sha256,
        disposition_id=PRODUCTION_DISPOSITION_ID,
        authorization_text_sha256=PRODUCTION_AUTHORIZATION_TEXT_SHA256,
        predecessor_attempt_identity_sha256=(
            predecessor_attempt_identity_sha256
        ),
        successor_attempt_identity_sha256=successor_identity,
        successor_generation=PRODUCTION_SUCCESSOR_GENERATION,
    )
    receipt = verify_disposition_receipt(
        production_disposition_paths(), expectation
    )
    expected = {
        "schema_version": RECEIPT_SCHEMA,
        "result": RESULT,
        "contract_sha256": contract_sha256,
        "authorization_text_sha256": PRODUCTION_AUTHORIZATION_TEXT_SHA256,
        "predecessor_attempt_identity_sha256": (
            predecessor_attempt_identity_sha256
        ),
        "predecessor_execution_id": PRODUCTION_PREDECESSOR_EXECUTION_ID,
        "successor_attempt_identity_sha256": successor_identity,
        "successor_generation": PRODUCTION_SUCCESSOR_GENERATION,
        "successor_authority_state_path": str(
            PRODUCTION_SUCCESSOR_AUTHORITY_STATE_PATH
        ),
        "successor_capsule_path": str(PRODUCTION_SUCCESSOR_CAPSULE_PATH),
        "successor_authorization_namespace": (
            PRODUCTION_SUCCESSOR_AUTHORIZATION_NAMESPACE
        ),
        "successor_install_scope_id": PRODUCTION_SUCCESSOR_INSTALL_SCOPE_ID,
        "successor_rollback_scope_id": PRODUCTION_SUCCESSOR_ROLLBACK_SCOPE_ID,
        "no_host_effects_proven": True,
        "predecessor_evidence_preserved_in_place": True,
        "predecessor_state_mutated": False,
        "deletion_performed": False,
        "provider_calls": 0,
        "production_data_read": False,
        "activation_performed": False,
    }
    if receipt is None or any(receipt.get(key) != value for key, value in expected.items()):
        raise PreEffectDispositionIntegrityError(
            "pre_effect_disposition_production_receipt_required"
        )
    return receipt


def refuse_retired_predecessor_entrypoint(
    paths: DispositionPaths,
    expectation: ReviewedDispositionExpectation,
) -> None:
    """Mandatory old-entrypoint fence, called while its guard is held.

    Absence allows the reviewed predecessor to continue.  A valid receipt
    terminally refuses it.  Any malformed or substituted receipt also refuses
    through a fail-closed verification error.
    """

    if verify_disposition_receipt(paths, expectation) is not None:
        raise PredecessorRetiredError("pre_effect_predecessor_retired")


def execute_pre_effect_disposition(
    paths: DispositionPaths,
    expectation: ReviewedDispositionExpectation,
    *,
    host_command_runner: HostCommandRunner | None = None,
) -> Mapping[str, object]:
    """Prove exact no-effect state and create/reverify one terminal receipt."""

    if type(paths) is not DispositionPaths or type(expectation) is not ReviewedDispositionExpectation:
        raise PreEffectDispositionSecurityError("pre_effect_disposition_inputs_invalid")
    if paths.production:
        if os.geteuid() != 0 or os.getegid() != 0 or host_command_runner is not None:
            raise PreEffectDispositionSecurityError("pre_effect_disposition_root_required")
        selected_runner: HostCommandRunner = _ClosedHostCommandRunner()
    else:
        selected_runner = host_command_runner or _ClosedHostCommandRunner()
    try:
        guard = GlobalExecutionLock(
            paths.live_guard_path,
            expected_uid=paths.expected_uid,
            expected_gid=paths.expected_gid,
        )
    except ExecutionLockError as error:
        raise PreEffectDispositionSecurityError(
            "pre_effect_disposition_live_guard_unavailable"
        ) from error
    with guard:
        try:
            global_lock = GlobalExecutionLock(
                paths.global_lock_path,
                expected_uid=paths.expected_uid,
                expected_gid=paths.expected_gid,
            )
        except ExecutionLockError as error:
            raise PreEffectDispositionSecurityError(
                "pre_effect_disposition_global_lock_unavailable"
            ) from error
        with global_lock:
            existing = verify_disposition_receipt(paths, expectation)
            if existing is not None:
                # The exact create-once old-store-spec tombstone is terminal
                # authority.  An unpatched predecessor may subsequently add
                # control-only journal records before its I03 preflight sees
                # this foreign slot and fails closed; do not let that harmless
                # drift turn the established fence into a successor DoS.
                return existing
            contract, expected_receipt = _load_contract_and_expected_receipt(
                paths, expectation
            )
            capsule = _read_regular(
                paths.predecessor_capsule_path,
                mode=0o400,
                expected_uid=paths.expected_uid,
                expected_gid=paths.expected_gid,
                maximum=MAX_CAPSULE_BYTES,
            )
            install_payload, rollback_payload = _verify_capsule(
                capsule.raw,
                contract=contract,
                expectation=expectation,
            )
            database = _verify_authority_state(
                paths.predecessor_authority_state_path,
                contract=contract,
                paths=paths,
                install_payload=install_payload,
                rollback_payload=rollback_payload,
            )
            directory_identity, member_identities = _verify_execution(
                contract=contract, paths=paths
            )
            host_hash = _verify_host_absence(
                contract["host_resources"], runner=selected_runner
            )
            if host_hash != expected_receipt["host_resources_sha256"]:
                raise PreEffectDispositionIntegrityError(
                    "pre_effect_disposition_host_contract_mismatch"
                )
            receipt_raw = canonical_json_bytes(expected_receipt)
            _write_create_once(
                paths.receipt_path,
                receipt_raw,
                expected_uid=paths.expected_uid,
                expected_gid=paths.expected_gid,
            )
            # Reprove old bytes and host absence after publication.  Only the
            # new receipt and lock files may have changed.
            capsule_after = _read_regular(
                paths.predecessor_capsule_path,
                mode=0o400,
                expected_uid=paths.expected_uid,
                expected_gid=paths.expected_gid,
                maximum=MAX_CAPSULE_BYTES,
            )
            database_after = _read_regular(
                paths.predecessor_authority_state_path,
                mode=0o600,
                expected_uid=paths.expected_uid,
                expected_gid=paths.expected_gid,
                maximum=MAX_DATABASE_BYTES,
            )
            directory_after, members_after = _verify_execution(
                contract=contract, paths=paths
            )
            if (
                capsule_after != capsule
                or database_after != database
                or directory_after != directory_identity
                or members_after != member_identities
                or _verify_host_absence(contract["host_resources"], runner=selected_runner)
                != host_hash
            ):
                raise PreEffectDispositionSecurityError(
                    "pre_effect_disposition_evidence_changed"
                )
            verified = verify_disposition_receipt(paths, expectation)
            if verified is None:
                raise PreEffectDispositionIntegrityError(
                    "pre_effect_disposition_receipt_missing"
                )
            return verified


__all__ = [
    "CONTRACT_SCHEMA",
    "DispositionPaths",
    "EMPTY_SHA256",
    "PreEffectDispositionError",
    "PreEffectDispositionHostStateError",
    "PreEffectDispositionIntegrityError",
    "PreEffectDispositionSecurityError",
    "PredecessorRetiredError",
    "RECEIPT_SCHEMA",
    "RESULT",
    "ReviewedDispositionExpectation",
    "canonical_json_bytes",
    "execute_pre_effect_disposition",
    "predecessor_attempt_identity_sha256",
    "production_disposition_paths",
    "production_successor_attempt_identity_sha256",
    "refuse_retired_predecessor_entrypoint",
    "require_production_disposition_receipt",
    "verify_disposition_receipt",
]
