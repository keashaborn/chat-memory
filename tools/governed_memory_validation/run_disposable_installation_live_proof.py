#!/usr/bin/env python3
from __future__ import annotations

"""Run the externally permitted Phase 9 disposable store proof.

The outer process accepts only four immutable identities. Authority comes
from one fixed root-owned permit containing pre-signed install documents and
a narrow rollback-signing delegation. The supervising issuer retains the
private key and signs only an exact ledger-derived rollback request over two
fixed inherited pipes; this runner never receives a private key.

This program never imports provider clients, reads production data or provider
credentials, accepts a path/command/SQL/URL/resource name, or performs a name-based
cleanup.  If rollback authority has been minted, failure recovery can only
resume the exact public ledger-bound rollback.
"""

import sys

_DONT_WRITE_BYTECODE_AT_START = sys.dont_write_bytecode
if __name__ == "__main__" and not _DONT_WRITE_BYTECODE_AT_START:
    raise SystemExit("phase9_live_proof_bytecode_writes_not_disabled")

import argparse
import base64
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import stat
import time
from types import MappingProxyType
from typing import Final, Mapping, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

_INVOKED_RELEASE_PACKAGE_SHA256: str | None = None
if sys.flags.isolated and __name__ == "__main__":
    _invoked_runner = Path(__file__)
    _resolved_runner = _invoked_runner.resolve(strict=True)
    _release_root = _resolved_runner.parents[2]
    if (
        not _invoked_runner.is_absolute()
        or _invoked_runner != _resolved_runner
        or _release_root.parent
        != Path("/opt/governed-memory-controller/releases")
        or re.fullmatch(r"[0-9a-f]{64}", _release_root.name, re.ASCII) is None
        or _resolved_runner.relative_to(_release_root).as_posix()
        != "tools/governed_memory_validation/run_disposable_installation_live_proof.py"
    ):
        raise SystemExit("phase9_live_proof_release_invocation_invalid")
    _INVOKED_RELEASE_PACKAGE_SHA256 = _release_root.name
    sys.path.insert(0, str(_release_root))

from tools.governed_memory_install import authority
from tools.governed_memory_install.authority_state import (
    AuthorityState,
    nonce_sha256 as authority_nonce_sha256,
    operation_sha256 as authority_operation_sha256,
)
from tools.governed_memory_install.controller_runtime import (
    verified_controller_runtime_evidence,
    verify_controller_runtime_capability,
)
from tools.governed_memory_install.durable_receipts import (
    DurableReceiptStore,
    ReceiptArtifact,
)
from tools.governed_memory_install.execution_authority import KernelUtcClock
from tools.governed_memory_install.execution_lock import GlobalExecutionLock
from tools.governed_memory_install.host_boundary import CommandRunner
from tools.governed_memory_install.image_preflight import (
    expectations_from_store_spec,
    inspect_exact_store_image_set,
)
from tools.governed_memory_install.install_backend import InstallPrerequisites
from tools.governed_memory_install.install_entrypoint import (
    run_authorized_dormant_store_install,
)
from tools.governed_memory_install.linux_plan import validate_store_spec
from tools.governed_memory_install.package_capability import (
    verified_package_evidence,
    verify_install_package_capability,
)
from tools.governed_memory_install.receipts import (
    verify_empty_rollback_receipt,
    verify_install_receipt,
)
from tools.governed_memory_install.resource_identity import (
    load_ledger,
    resource_ledger_binding_sha256,
)
from tools.governed_memory_install.rollback import (
    build_empty_rollback_eligibility_receipt,
    derive_exact_rollback_resources_from_ledger,
    empty_rollback_plan_sha256,
    verified_rollback_resource_parts,
)
from tools.governed_memory_install.rollback_authority import (
    AUTHORIZATION_PAYLOAD_SCHEMA_VERSION as ROLLBACK_AUTHORIZATION_PAYLOAD_SCHEMA,
    AUTHORIZATION_SCHEMA_VERSION as ROLLBACK_AUTHORIZATION_SCHEMA,
    EmptyRollbackExpectedBindings,
    ROLLBACK_OPERATION,
    SCOPE_SCHEMA_VERSION as ROLLBACK_SCOPE_SCHEMA,
    TRUST_BUNDLE_SCHEMA_VERSION as ROLLBACK_TRUST_SCHEMA,
    verify_empty_rollback_execution_capability,
)
from tools.governed_memory_install.rollback_entrypoint import (
    AUTHORITY_STATE_PATH,
    GLOBAL_LOCK_PATH,
    run_authorized_empty_store_rollback,
)


THREAD_ID: Final = "019fe927-8367-7f52-86f2-e2b5b43a2390"
AUTHORIZED_TEXT: Final = "Authorized to move on and finish nine whatever it takes"
AUTHORIZED_TEXT_SHA256: Final = hashlib.sha256(
    AUTHORIZED_TEXT.encode("utf-8")
).hexdigest()
PERMIT_PATH: Final = Path(
    "/run/governed-memory-controller/phase9-disposable-proof-permit.json"
)
ROLLBACK_REQUEST_FD: Final = 3
ROLLBACK_RESPONSE_FD: Final = 4
PERMIT_SCHEMA: Final = "governed-memory-phase9-disposable-proof-permit-v1"
AUTHORIZATION_NAMESPACE: Final = "governed-memory-phase9-live-proof-v1"
INSTALL_SCOPE_ID: Final = "phase9-disposable-live-install-000001"
ROLLBACK_SCOPE_ID: Final = "phase9-disposable-live-rollback-000001"
LIVE_PROOF_RECEIPT_SCHEMA: Final = "governed-memory-phase9-live-proof-receipt-v1"
LIVE_PROOF_RECEIPT_SCHEMA_RELATIVE: Final = (
    "ops/governed_memory/installation/current/live_proof_receipt.schema.json"
)
INSTALL_NONCE_DOMAIN: Final = b"governed-memory-phase9-install-nonce-v1\x00"
ROLLBACK_NONCE_DOMAIN: Final = b"governed-memory-phase9-rollback-nonce-v1\x00"

EXECUTIONS_ROOT: Final = Path("/var/lib/governed-memory-controller/executions")
CONTROLLER_STATE_ROOT: Final = Path("/var/lib/governed-memory-controller")
LOCK_ROOT: Final = Path("/run/lock/governed-memory-controller")
CONTROLLER_CONFIG_ROOT: Final = Path("/etc/governed-memory-controller")
STORE_SECRET_PARENT: Final = Path("/etc/governed-memory-stores")
STORE_SECRET_ROOT: Final = Path(
    "/etc/governed-memory-stores/9a54cf123493-000001"
)
RESOLVED_STORE_SPEC_PATH: Final = Path(
    "/etc/governed-memory-controller/store_spec.json"
)
RUNTIME_RECEIPT_ROOT: Final = Path(
    "/var/lib/governed-memory-controller/runtime-receipts"
)
RELEASE_ROOT_PREFIX: Final = Path("/opt/governed-memory-controller/releases")

INSTALL_KILL_STEP: Final = "I04_WRITE_RESOLVED_STORE_SPEC_AND_GENERATE_FRESH_STORE_SECRETS"
ROLLBACK_KILL_STEP: Final = "R05_DISABLE_AND_REMOVE_STORES_SUPERVISOR"
JOURNAL_APPLIED_EVENT: Final = "applied"
BOUNDARY_TIMEOUT_SECONDS: Final = 180.0
POLL_INTERVAL_SECONDS: Final = 0.002
MAX_WORKER_RESULT_BYTES: Final = 1024 * 1024
MAX_ARTIFACT_BYTES: Final = 8 * 1024 * 1024

_HASH_RE: Final = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_COMMIT_RE: Final = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_EXECUTION_DIRECTORY_RE: Final = _HASH_RE

_LIVE_PROOF_RECEIPT_KEYS: Final = frozenset(
    {
        "schema_version",
        "result",
        "observation_scope",
        "candidate_git_commit",
        "candidate_git_tree",
        "package_manifest_sha256",
        "controller_runtime_receipt_sha256",
        "live_proof_receipt_schema_sha256",
        "external_proof_permit_sha256",
        "authorization_text_sha256",
        "installation_execution_id",
        "installation_receipt_sha256",
        "image_identity_set_sha256",
        "empty_rollback_execution_id",
        "empty_rollback_receipt_sha256",
        "retained_audit_set_sha256",
        "install_process_death_boundary_sha256",
        "rollback_process_death_boundary_sha256",
        "install_process_death_observed",
        "install_exact_resume_completed",
        "cold_controller_process_restart_terminal_postflight_verified",
        "cold_restart_scope",
        "host_reboot_proven",
        "persistent_store_restart_supervision_and_boot_recovery_proven",
        "rollback_process_death_observed",
        "rollback_exact_resume_completed",
        "public_install_entrypoint_used",
        "public_empty_rollback_entrypoint_used",
        "fresh_r06_semantic_empty_recheck_required",
        "completed_public_rollback_replayed",
        "exact_targets_absent_count",
        "exact_resources_absent_at_terminal_observation",
        "terminal_absence_is_continuous_guarantee",
        "stores_installed_at_terminal_observation",
        "stores_supervisor_installed_at_terminal_observation",
        "controller_runtime_capability_reverified",
        "source_postgres_read_count",
        "source_postgres_write_count",
        "provider_calls",
        "production_data_read",
        "application_services_installed",
        "activation_performed",
        "issuer_or_host_death_durable_cleanup_proven",
        "receipt_sha256",
    }
)


class LiveProofError(RuntimeError):
    """Content-free refusal from the disposable live proof runner."""


class WorkerMode(str, Enum):
    INSTALL = "install"
    RESUME_INSTALL = "resume-install"
    ROLLBACK = "rollback"
    RESUME_ROLLBACK = "resume-rollback"
    VERIFY_ABSENCE = "verify-absence"


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
        raise LiveProofError("phase9_live_proof_json_invalid") from error


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _document_sha(value: object) -> str:
    return _sha(_canonical(value))


def _require_hash(value: str, *, commit: bool = False) -> str:
    pattern = _COMMIT_RE if commit else _HASH_RE
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise LiveProofError("phase9_live_proof_identity_invalid")
    return value


@dataclass(frozen=True, slots=True)
class ProofInputs:
    candidate_git_commit: str
    candidate_git_tree: str
    package_manifest_sha256: str
    controller_runtime_receipt_sha256: str

    def __post_init__(self) -> None:
        _require_hash(self.candidate_git_commit, commit=True)
        _require_hash(self.candidate_git_tree, commit=True)
        _require_hash(self.package_manifest_sha256)
        _require_hash(self.controller_runtime_receipt_sha256)
        if (
            _INVOKED_RELEASE_PACKAGE_SHA256 is not None
            and self.package_manifest_sha256
            != _INVOKED_RELEASE_PACKAGE_SHA256
        ):
            raise LiveProofError("phase9_live_proof_release_identity_mismatch")


@dataclass(frozen=True, slots=True)
class VerifiedPermit:
    raw: bytes
    document: Mapping[str, object]
    install_documents: InstallDocuments
    rollback_delegation: Mapping[str, object]
    key_id: str
    rollback_nonce: str


@dataclass(frozen=True, slots=True)
class InstallDocuments:
    scope: bytes
    authorization: bytes
    trust_bundle: bytes
    key_id: str


@dataclass(frozen=True, slots=True)
class RollbackDocuments:
    scope: bytes
    authorization: bytes
    trust_bundle: bytes
    key_id: str
    eligibility: Mapping[str, object]
    resources: object
    resolved_store_spec: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ProofContext:
    inputs: ProofInputs
    package_manifest: bytes
    artifacts: Mapping[str, bytes]
    runtime_receipt: bytes
    install_documents: InstallDocuments
    verified_scope_capability: object
    verified_package_capability: object
    verified_runtime_capability: object
    permit: VerifiedPermit
    rollback_documents: RollbackDocuments | None = None


def _read_regular_no_follow(path: Path, *, maximum: int) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        named = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
            or before.st_uid != 0
            or before.st_nlink != 1
            or not 1 <= before.st_size <= maximum
        ):
            raise LiveProofError("phase9_live_proof_file_identity_invalid")
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, min(65536, maximum + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > maximum:
                raise LiveProofError("phase9_live_proof_file_too_large")
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
        ):
            raise LiveProofError("phase9_live_proof_file_changed")
        return b"".join(chunks)
    except LiveProofError:
        raise
    except OSError as error:
        raise LiveProofError("phase9_live_proof_file_unavailable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _parse_canonical_object(raw: bytes, code: str) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise LiveProofError(code) from error
    if type(value) is not dict or _canonical(value) != raw:
        raise LiveProofError(code)
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise LiveProofError("phase9_live_proof_json_duplicate_key")
        result[key] = value
    return result


def _reject_nonfinite(unused: str) -> None:
    raise LiveProofError("phase9_live_proof_json_nonfinite")


def _parse_exact_hashed_json_object(raw: bytes, code: str) -> dict[str, object]:
    """Parse exact hash-bound release bytes without imposing whitespace form."""

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
    except LiveProofError as error:
        raise LiveProofError(code) from error
    except (UnicodeError, json.JSONDecodeError) as error:
        raise LiveProofError(code) from error
    if type(value) is not dict:
        raise LiveProofError(code)
    return value


_PERMIT_KEYS: Final = frozenset(
    {
        "schema_version",
        "thread_id",
        "authorization_text_sha256",
        "authorization_namespace",
        "install_scope_id",
        "rollback_scope_id",
        "candidate_git_commit",
        "candidate_git_tree",
        "package_manifest_sha256",
        "controller_runtime_receipt_sha256",
        "controller_contract_sha256",
        "execution_plan_sha256",
        "exact_targets_sha256",
        "public_key_base64",
        "key_id",
        "permit_nonce",
        "issued_at",
        "not_before",
        "expires_at",
        "single_use",
        "install_scope",
        "install_authorization",
        "trust_bundle",
        "rollback_delegation",
    }
)


def _parse_utc(value: object) -> datetime:
    if type(value) is not str or re.fullmatch(
        r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
        r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z",
        value,
        re.ASCII,
    ) is None:
        raise LiveProofError("phase9_live_proof_permit_time_invalid")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise LiveProofError("phase9_live_proof_permit_time_invalid") from error


def _decode_public_key(document: Mapping[str, object]) -> tuple[Ed25519PublicKey, str]:
    try:
        encoded = document.get("public_key_base64")
        if type(encoded) is not str:
            raise ValueError
        public = base64.b64decode(encoded, validate=True)
        if len(public) != 32 or base64.b64encode(public).decode("ascii") != encoded:
            raise ValueError
        key_id = _sha(public)
        if document.get("key_id") != key_id:
            raise ValueError
        return Ed25519PublicKey.from_public_bytes(public), key_id
    except (ValueError, TypeError) as error:
        raise LiveProofError("phase9_live_proof_permit_public_key_invalid") from error


def _verify_rollback_delegation(
    document: Mapping[str, object], public_key: Ed25519PublicKey, key_id: str
) -> Mapping[str, object]:
    envelope = document.get("rollback_delegation")
    if type(envelope) is not dict or set(envelope) != {
        "schema_version", "payload", "signature"
    }:
        raise LiveProofError("phase9_live_proof_rollback_delegation_invalid")
    payload = envelope.get("payload")
    signature = envelope.get("signature")
    if (
        envelope.get("schema_version")
        != "governed-memory-phase9-rollback-delegation-envelope-v1"
        or type(payload) is not dict
        or type(signature) is not dict
        or set(signature) != {"algorithm", "key_id", "value_base64"}
        or signature.get("algorithm") != "Ed25519"
        or signature.get("key_id") != key_id
    ):
        raise LiveProofError("phase9_live_proof_rollback_delegation_invalid")
    expected_payload = {
        "schema_version": "governed-memory-phase9-rollback-delegation-v1",
        "authorization_namespace": AUTHORIZATION_NAMESPACE,
        "thread_id": THREAD_ID,
        "install_scope_id": INSTALL_SCOPE_ID,
        "rollback_scope_id": ROLLBACK_SCOPE_ID,
        "authorization_text_sha256": AUTHORIZED_TEXT_SHA256,
        "candidate_git_commit": document["candidate_git_commit"],
        "candidate_git_tree": document["candidate_git_tree"],
        "package_manifest_sha256": document["package_manifest_sha256"],
        "controller_runtime_receipt_sha256": document[
            "controller_runtime_receipt_sha256"
        ],
        "controller_contract_sha256": document["controller_contract_sha256"],
        "execution_plan_sha256": document["execution_plan_sha256"],
        "exact_targets_sha256": document["exact_targets_sha256"],
        "permit_nonce": document["permit_nonce"],
        "key_id": key_id,
        "issued_at": document["issued_at"],
        "not_before": document["not_before"],
        "expires_at": document["expires_at"],
        "single_use": True,
        "empty_only": True,
        "source_postgres_read_count": 0,
        "production_data_read": False,
        "provider_calls": 0,
        "activation_allowed": False,
    }
    if payload != expected_payload:
        raise LiveProofError("phase9_live_proof_rollback_delegation_invalid")
    try:
        raw_signature = base64.b64decode(str(signature["value_base64"]), validate=True)
        if len(raw_signature) != 64:
            raise ValueError
        public_key.verify(raw_signature, _canonical(payload))
    except (InvalidSignature, ValueError, TypeError) as error:
        raise LiveProofError("phase9_live_proof_rollback_delegation_invalid") from error
    return MappingProxyType(dict(envelope))


def _load_verified_permit(
    inputs: ProofInputs,
    artifacts: Mapping[str, bytes],
) -> VerifiedPermit:
    descriptor = -1
    try:
        descriptor = os.open(
            PERMIT_PATH,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        named = PERMIT_PATH.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o400
            or opened.st_uid != 0
            or opened.st_gid != 0
            or opened.st_nlink != 1
            or opened.st_size < 1
            or opened.st_size > 64 * 1024
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise LiveProofError("phase9_live_proof_permit_identity_invalid")
        raw = os.read(descriptor, opened.st_size + 1)
        if len(raw) != opened.st_size or os.fstat(descriptor) != opened:
            raise LiveProofError("phase9_live_proof_permit_changed")
    except LiveProofError:
        raise
    except OSError as error:
        raise LiveProofError("phase9_live_proof_permit_unavailable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    document = _parse_canonical_object(raw, "phase9_live_proof_permit_invalid")
    contract_raw = artifacts.get(
        "ops/governed_memory/installation/current/contract.json"
    )
    plan_raw = artifacts.get(
        "ops/governed_memory/installation/current/controller_plan.json"
    )
    if type(contract_raw) is not bytes or type(plan_raw) is not bytes:
        raise LiveProofError("phase9_live_proof_package_artifact_missing")
    contract = _parse_exact_hashed_json_object(
        contract_raw, "phase9_live_proof_contract_invalid"
    )
    exact_targets = contract.get("exact_targets")
    expected = {
        "schema_version": PERMIT_SCHEMA,
        "thread_id": THREAD_ID,
        "authorization_text_sha256": AUTHORIZED_TEXT_SHA256,
        "authorization_namespace": AUTHORIZATION_NAMESPACE,
        "install_scope_id": INSTALL_SCOPE_ID,
        "rollback_scope_id": ROLLBACK_SCOPE_ID,
        "candidate_git_commit": inputs.candidate_git_commit,
        "candidate_git_tree": inputs.candidate_git_tree,
        "package_manifest_sha256": inputs.package_manifest_sha256,
        "controller_runtime_receipt_sha256": (
            inputs.controller_runtime_receipt_sha256
        ),
        "controller_contract_sha256": _sha(contract_raw),
        "execution_plan_sha256": _sha(plan_raw),
        "exact_targets_sha256": _document_sha(exact_targets),
    }
    if (
        set(document) != _PERMIT_KEYS
        or type(exact_targets) is not dict
        or any(document.get(key) != value for key, value in expected.items())
        or document.get("single_use") is not True
        or type(document.get("permit_nonce")) is not str
        or _HASH_RE.fullmatch(str(document["permit_nonce"])) is None
    ):
        raise LiveProofError("phase9_live_proof_permit_binding_invalid")
    issued = _parse_utc(document["issued_at"])
    not_before = _parse_utc(document["not_before"])
    expires = _parse_utc(document["expires_at"])
    now = datetime.now(timezone.utc)
    if (
        not_before < issued - timedelta(seconds=10)
        or expires <= not_before
        or (expires - not_before).total_seconds() > 15 * 60
        or now < not_before
        or now >= expires
    ):
        raise LiveProofError("phase9_live_proof_permit_expired")
    public_key, key_id = _decode_public_key(document)
    permit_nonce = str(document["permit_nonce"])
    install_nonce = _sha(INSTALL_NONCE_DOMAIN + permit_nonce.encode("ascii"))
    rollback_nonce = _sha(ROLLBACK_NONCE_DOMAIN + permit_nonce.encode("ascii"))
    install_scope = document.get("install_scope")
    install_authorization = document.get("install_authorization")
    trust_bundle = document.get("trust_bundle")
    if (
        install_scope != _install_scope(inputs, artifacts)
        or type(install_authorization) is not dict
        or type(trust_bundle) is not dict
        or trust_bundle
        != {
            "schema_version": authority.TRUST_BUNDLE_SCHEMA_VERSION,
            "authorization_namespace": AUTHORIZATION_NAMESPACE,
            "keys": [
                {
                    "key_id": key_id,
                    "algorithm": "Ed25519",
                    "public_key_base64": document["public_key_base64"],
                }
            ],
        }
    ):
        raise LiveProofError("phase9_live_proof_install_delegation_invalid")
    scope_raw = _canonical(install_scope)
    auth_payload = install_authorization.get("payload")
    if (
        set(install_authorization)
        != {"schema_version", "payload", "signature"}
        or install_authorization.get("schema_version")
        != authority.AUTHORIZATION_SCHEMA_VERSION
        or type(auth_payload) is not dict
        or auth_payload
        != {
            "schema_version": authority.AUTHORIZATION_PAYLOAD_SCHEMA_VERSION,
            "authorization_id": "phase9-disposable-live-install-auth-000001",
            "authorization_namespace": AUTHORIZATION_NAMESPACE,
            "thread_id": THREAD_ID,
            "scope_id": INSTALL_SCOPE_ID,
            "scope_sha256": _sha(scope_raw),
            "key_id": key_id,
            "approval_phrase": (
                "APPROVE GOVERNED MEMORY DORMANT STORE INSTALL " + _sha(scope_raw)
            ),
            "nonce": install_nonce,
            "issued_at": document["issued_at"],
            "not_before": document["not_before"],
            "expires_at": document["expires_at"],
            "single_use": True,
        }
    ):
        raise LiveProofError("phase9_live_proof_install_delegation_invalid")
    delegation = _verify_rollback_delegation(document, public_key, key_id)
    return VerifiedPermit(
        raw=raw,
        document=MappingProxyType(document),
        install_documents=InstallDocuments(
            scope=scope_raw,
            authorization=_canonical(install_authorization),
            trust_bundle=_canonical(trust_bundle),
            key_id=key_id,
        ),
        rollback_delegation=delegation,
        key_id=key_id,
        rollback_nonce=rollback_nonce,
    )


def _safe_release_member(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if (
        type(relative) is not str
        or not relative
        or pure.is_absolute()
        or str(pure) != relative
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise LiveProofError("phase9_live_proof_manifest_path_invalid")
    return root.joinpath(*pure.parts)


def _load_release(inputs: ProofInputs) -> tuple[bytes, Mapping[str, bytes]]:
    release_root = RELEASE_ROOT_PREFIX / inputs.package_manifest_sha256
    manifest_path = release_root / (
        "ops/governed_memory/installation/current/package_manifest.json"
    )
    manifest_raw = _read_regular_no_follow(
        manifest_path, maximum=MAX_ARTIFACT_BYTES
    )
    if _sha(manifest_raw) != inputs.package_manifest_sha256:
        raise LiveProofError("phase9_live_proof_package_identity_mismatch")
    manifest = _parse_exact_hashed_json_object(
        manifest_raw, "phase9_live_proof_package_manifest_invalid"
    )
    index = manifest.get("artifacts")
    if type(index) is not dict or not index:
        raise LiveProofError("phase9_live_proof_package_manifest_invalid")
    artifacts: dict[str, bytes] = {}
    for relative in sorted(index):
        expected = index[relative]
        if (
            type(relative) is not str
            or type(expected) is not str
            or _HASH_RE.fullmatch(expected) is None
        ):
            raise LiveProofError("phase9_live_proof_package_manifest_invalid")
        raw = _read_regular_no_follow(
            _safe_release_member(release_root, relative),
            maximum=MAX_ARTIFACT_BYTES,
        )
        if _sha(raw) != expected:
            raise LiveProofError("phase9_live_proof_package_artifact_mismatch")
        artifacts[relative] = raw
    return manifest_raw, MappingProxyType(artifacts)


def _load_runtime_receipt(inputs: ProofInputs) -> bytes:
    raw = _read_regular_no_follow(
        RUNTIME_RECEIPT_ROOT
        / (inputs.controller_runtime_receipt_sha256 + ".json"),
        maximum=128 * 1024,
    )
    if _sha(raw) != inputs.controller_runtime_receipt_sha256:
        raise LiveProofError("phase9_live_proof_runtime_receipt_mismatch")
    return raw


def _utc_seconds(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _install_scope(
    inputs: ProofInputs,
    artifacts: Mapping[str, bytes],
) -> dict[str, object]:
    contract_raw = artifacts.get(
        "ops/governed_memory/installation/current/contract.json"
    )
    plan_raw = artifacts.get(
        "ops/governed_memory/installation/current/controller_plan.json"
    )
    if type(contract_raw) is not bytes or type(plan_raw) is not bytes:
        raise LiveProofError("phase9_live_proof_package_artifact_missing")
    contract = _parse_exact_hashed_json_object(
        contract_raw, "phase9_live_proof_contract_invalid"
    )
    exact_targets = contract.get("exact_targets")
    if type(exact_targets) is not dict:
        raise LiveProofError("phase9_live_proof_contract_invalid")
    return {
        "schema_version": authority.SCOPE_SCHEMA_VERSION,
        "operation": authority.AUTHORIZATION_OPERATION,
        "authorization_namespace": AUTHORIZATION_NAMESPACE,
        "thread_id": THREAD_ID,
        "scope_id": INSTALL_SCOPE_ID,
        "candidate_git_commit": inputs.candidate_git_commit,
        "candidate_git_tree": inputs.candidate_git_tree,
        "package_manifest_sha256": inputs.package_manifest_sha256,
        "controller_contract_sha256": _sha(contract_raw),
        "execution_plan_sha256": _sha(plan_raw),
        "exact_targets_sha256": _document_sha(exact_targets),
        "controller_runtime_receipt_sha256": (
            inputs.controller_runtime_receipt_sha256
        ),
        "source_boundary": {
            "source_postgres_connection_count": 0,
            "source_postgres_read_count": 0,
            "source_postgres_write_count": 0,
            "source_preparation_phase": (
                "separate_source_preparation_authorization_required"
            ),
        },
        "store_policy": {
            "postgresql": "fresh_isolated_empty",
            "qdrant": "fresh_isolated_empty",
            "legacy_imports_allowed": False,
            "snapshot_restore_allowed": False,
            "unprocessed_prefill_allowed": False,
            "initial_postgresql_user_row_count": 0,
            "initial_qdrant_point_count": 0,
        },
        "secret_policy": {
            "allowed_secret_names": list(authority.ALLOWED_SECRET_NAMES),
            "forbidden_secret_classes": list(
                authority.FORBIDDEN_SECRET_CLASSES
            ),
            "runtime_secret_count": 0,
            "provider_secret_count": 0,
            "supabase_secret_count": 0,
            "service_secret_count": 0,
            "pilot_secret_count": 0,
            "secret_values_present": False,
        },
    }


def _verified_install_context(inputs: ProofInputs) -> tuple[ProofContext, VerifiedPermit]:
    package_manifest, artifacts = _load_release(inputs)
    runtime_receipt = _load_runtime_receipt(inputs)
    permit = _load_verified_permit(inputs, artifacts)
    documents = permit.install_documents
    scope_document = _parse_canonical_object(
        documents.scope, "phase9_live_proof_scope_invalid"
    )
    bindings = authority.DormantInstallExpectedBindings(
        candidate_git_commit=inputs.candidate_git_commit,
        candidate_git_tree=inputs.candidate_git_tree,
        package_manifest_sha256=inputs.package_manifest_sha256,
        controller_contract_sha256=str(
            scope_document["controller_contract_sha256"]
        ),
        execution_plan_sha256=str(scope_document["execution_plan_sha256"]),
        exact_targets_sha256=str(scope_document["exact_targets_sha256"]),
        controller_runtime_receipt_sha256=(
            inputs.controller_runtime_receipt_sha256
        ),
    )
    scope_capability = authority.verify_dormant_install_execution_capability(
        documents.scope,
        documents.authorization,
        documents.trust_bundle,
        expected_namespace=AUTHORIZATION_NAMESPACE,
        expected_thread_id=THREAD_ID,
        expected_scope_id=INSTALL_SCOPE_ID,
        expected_key_id=documents.key_id,
        expected_trust_bundle_sha256=_sha(documents.trust_bundle),
        expected_bindings=bindings,
    )
    package_capability = verify_install_package_capability(
        scope_capability,
        signed_scope_json=documents.scope,
        package_manifest_json=package_manifest,
        artifact_bytes=artifacts,
    )
    runtime_capability = verify_controller_runtime_capability(
        package_capability,
        runtime_build_receipt_json=runtime_receipt,
    )
    return (
        ProofContext(
            inputs=inputs,
            package_manifest=package_manifest,
            artifacts=artifacts,
            runtime_receipt=runtime_receipt,
            install_documents=documents,
            verified_scope_capability=scope_capability,
            verified_package_capability=package_capability,
            verified_runtime_capability=runtime_capability,
            permit=permit,
        ),
        permit,
    )


def _ensure_root_directory(path: Path, mode: int) -> None:
    try:
        os.mkdir(path, mode)
        os.chmod(path, mode, follow_symlinks=False)
        os.chown(path, 0, 0, follow_symlinks=False)
    except FileExistsError:
        pass
    except OSError as error:
        raise LiveProofError("phase9_live_proof_substrate_create_failed") from error
    try:
        observed = path.stat(follow_symlinks=False)
    except OSError as error:
        raise LiveProofError("phase9_live_proof_substrate_invalid") from error
    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != 0
        or observed.st_gid != 0
        or stat.S_IMODE(observed.st_mode) != mode
    ):
        raise LiveProofError("phase9_live_proof_substrate_invalid")


def _prepare_fixed_substrate() -> None:
    if os.geteuid() != 0:
        raise LiveProofError("phase9_live_proof_root_required")
    _ensure_root_directory(LOCK_ROOT, 0o700)
    _ensure_root_directory(CONTROLLER_STATE_ROOT, 0o700)
    _ensure_root_directory(EXECUTIONS_ROOT, 0o700)
    _ensure_root_directory(CONTROLLER_CONFIG_ROOT, 0o700)
    _ensure_root_directory(STORE_SECRET_PARENT, 0o755)
    _ensure_root_directory(STORE_SECRET_ROOT, 0o700)


def _authority_state() -> AuthorityState:
    try:
        metadata = AUTHORITY_STATE_PATH.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise LiveProofError("phase9_live_proof_authority_state_invalid")
        create = False
    except FileNotFoundError:
        create = True
    return AuthorityState(
        AUTHORITY_STATE_PATH,
        expected_uid=0,
        create=create,
    )


def _install_prerequisites(context: ProofContext) -> InstallPrerequisites:
    store_raw = context.artifacts.get(
        "ops/governed_memory/installation/store_spec.json"
    )
    if type(store_raw) is not bytes:
        raise LiveProofError("phase9_live_proof_store_spec_missing")
    store_spec = validate_store_spec(
        _parse_exact_hashed_json_object(
            store_raw, "phase9_live_proof_store_spec_invalid"
        ),
        allow_placeholders=True,
    )
    expectations = expectations_from_store_spec(store_spec)
    local_images = inspect_exact_store_image_set(
        CommandRunner(command_profile="image_inspect"), expectations
    )
    runtime = verified_controller_runtime_evidence(
        context.verified_runtime_capability
    )
    package = verified_package_evidence(context.verified_package_capability)
    return InstallPrerequisites(
        store_spec_sha256=package.store_spec_sha256,
        controller_runtime_receipt_sha256=(
            runtime.controller_runtime_receipt_sha256
        ),
        controller_runtime_root=runtime.runtime_root,
        controller_runtime_tree_sha256=runtime.runtime_tree_sha256,
        controller_release_root=runtime.release_root,
        controller_release_tree_sha256=runtime.release_tree_sha256,
        controller_release_package_manifest_path=runtime.package_manifest_path,
        controller_runtime_interpreter_path=runtime.interpreter_path,
        controller_runtime_interpreter_sha256=runtime.interpreter_sha256,
        controller_runtime_inventory_path=runtime.inventory_path,
        controller_runtime_inventory_sha256=(
            runtime.installed_distribution_inventory_sha256
        ),
        controller_requirements_lock_sha256=(
            runtime.controller_requirements_lock_sha256
        ),
        supervisor_launcher_path=runtime.supervisor_launcher_path,
        supervisor_launcher_sha256=runtime.supervisor_launcher_sha256,
        local_images=local_images,
        expected_images=expectations,
    )


def _run_install_worker(context: ProofContext) -> Mapping[str, object]:
    state = _authority_state()
    receipt_store = DurableReceiptStore.production()
    prerequisites = _install_prerequisites(context)
    with GlobalExecutionLock(GLOBAL_LOCK_PATH, expected_uid=0) as lock:
        held = lock.held_capability()
        return run_authorized_dormant_store_install(
            verified_scope_capability=context.verified_scope_capability,
            verified_package_capability=context.verified_package_capability,
            verified_controller_runtime_capability=(
                context.verified_runtime_capability
            ),
            authority_state=state,
            clock=KernelUtcClock(),
            held_lock=held,
            prerequisites=prerequisites,
            receipt_store=receipt_store,
        )


def _expected_install_execution_id(context: ProofContext) -> str:
    """Derive the exact install execution ID before the first worker fork."""

    scope = context.install_documents.scope
    authorization = context.install_documents.authorization
    trust_bundle = context.install_documents.trust_bundle
    envelope = _parse_canonical_object(
        authorization, "phase9_live_proof_install_authorization_invalid"
    )
    payload = envelope.get("payload")
    if type(payload) is not dict or type(payload.get("nonce")) is not str:
        raise LiveProofError("phase9_live_proof_install_authorization_invalid")
    operation = authority.AUTHORIZATION_OPERATION
    scope_hash = _sha(scope)
    authorization_hash = _sha(authorization)
    trust_hash = _sha(trust_bundle)
    operation_hash = authority_operation_sha256(operation)
    execution_material = b"\x00".join(
        value.encode("ascii")
        for value in (
            operation,
            scope_hash,
            authorization_hash,
            trust_hash,
        )
    )
    execution_sha256 = _sha(
        b"governed-memory-execution-binding-v1\x00" + execution_material
    )
    nonce_hash = authority_nonce_sha256(str(payload["nonce"]))
    claim_material = b"\x00".join(
        value.encode("ascii")
        for value in (
            nonce_hash,
            operation_hash,
            execution_sha256,
            authorization_hash,
            scope_hash,
            trust_hash,
        )
    )
    claim_sha256 = _sha(
        b"governed-memory-authority-claim-v1\x00" + claim_material
    )
    execution_id_material = {
        "claim_sha256": claim_sha256,
        "execution_sha256": execution_sha256,
        "operation_sha256": operation_hash,
    }
    return _sha(
        b"governed-memory-dormant_store_install-execution-id-v1\x00"
        + _canonical(execution_id_material)
    )


def _resolved_store_spec() -> Mapping[str, object]:
    raw = _read_regular_no_follow(
        RESOLVED_STORE_SPEC_PATH, maximum=16 * 1024 * 1024
    )
    if not raw.endswith(b"\n"):
        raise LiveProofError("phase9_live_proof_resolved_store_spec_invalid")
    document = _parse_canonical_object(
        raw[:-1], "phase9_live_proof_resolved_store_spec_invalid"
    )
    return MappingProxyType(
        validate_store_spec(document, allow_placeholders=False)
    )


def _load_install_resources(
    *,
    install_receipt: Mapping[str, object],
    resolved_store_spec: Mapping[str, object],
) -> object:
    verified = verify_install_receipt(install_receipt)
    execution_id = str(verified["execution_id"])
    binding = resolved_store_spec.get("execution_binding")
    if type(binding) is not dict or binding.get("execution_id") != execution_id:
        raise LiveProofError("phase9_live_proof_install_binding_mismatch")
    journal_binding = binding.get("binding_sha256")
    if type(journal_binding) is not str or _HASH_RE.fullmatch(journal_binding) is None:
        raise LiveProofError("phase9_live_proof_install_binding_mismatch")
    ledger_binding = resource_ledger_binding_sha256(journal_binding)
    records = load_ledger(
        EXECUTIONS_ROOT / execution_id / "resources.jsonl",
        expected_binding_sha256=ledger_binding,
        expected_uid=0,
    )
    if (
        not records
        or records[-1].entry_sha256
        != verified["resource_ledger_head_sha256"]
        or len(records) != verified["resource_ledger_sequence"]
    ):
        raise LiveProofError("phase9_live_proof_install_ledger_mismatch")
    return derive_exact_rollback_resources_from_ledger(
        records,
        package_manifest_sha256=str(verified["package_manifest_sha256"]),
        expected_ledger_head_sha256=str(
            verified["resource_ledger_head_sha256"]
        ),
    )


def _rollback_eligibility_seed(
    *,
    install_receipt: Mapping[str, object],
    resources: object,
) -> Mapping[str, object]:
    install = verify_install_receipt(install_receipt)
    evidence, unused = verified_rollback_resource_parts(resources)
    expectation_sha256 = _document_sha(
        {
            "schema_version": (
                "governed-memory-phase9-zero-state-expectation-v1"
            ),
            "installation_receipt_sha256": install["receipt_sha256"],
            "pilot_ever_started": False,
            "postgresql_user_rows": 0,
            "projection_queue_rows": 0,
            "qdrant_points": 0,
            "active_clients": 0,
            "legacy_imports": 0,
            "source_postgres_read_count": 0,
            "production_data_read": False,
            "provider_calls": 0,
        }
    )
    return MappingProxyType(
        build_empty_rollback_eligibility_receipt(
            candidate_git_commit=str(install["candidate_git_commit"]),
            candidate_git_tree=str(install["candidate_git_tree"]),
            package_manifest_sha256=str(install["package_manifest_sha256"]),
            installation_execution_id=str(install["execution_id"]),
            installation_receipt_sha256=str(install["receipt_sha256"]),
            exact_targets_sha256=evidence.exact_targets_sha256,
            resource_ledger_head_sha256=(
                evidence.resource_ledger_head_sha256
            ),
            observation_set_sha256=expectation_sha256,
            pilot_ever_started=False,
            postgresql_user_rows=0,
            projection_queue_rows=0,
            qdrant_points=0,
            active_clients=0,
            legacy_imports=0,
            application_services_installed=False,
            source_postgres_read_count=0,
            production_data_read=False,
            provider_calls=0,
        )
    )


def _write_pipe_document(descriptor: int, document: Mapping[str, object]) -> None:
    raw = _canonical(dict(document)) + b"\n"
    if len(raw) > MAX_WORKER_RESULT_BYTES:
        raise LiveProofError("phase9_live_proof_signing_request_too_large")
    view = memoryview(raw)
    try:
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError
            view = view[written:]
    except OSError as error:
        raise LiveProofError("phase9_live_proof_signing_channel_failed") from error


def _read_pipe_document(descriptor: int) -> dict[str, object]:
    data = bytearray()
    try:
        while len(data) <= MAX_WORKER_RESULT_BYTES:
            block = os.read(descriptor, min(65536, MAX_WORKER_RESULT_BYTES + 1 - len(data)))
            if not block:
                break
            data.extend(block)
            if b"\n" in block:
                break
    except OSError as error:
        raise LiveProofError("phase9_live_proof_signing_channel_failed") from error
    if not data.endswith(b"\n") or b"\n" in data[:-1]:
        raise LiveProofError("phase9_live_proof_signing_response_invalid")
    return _parse_canonical_object(
        bytes(data[:-1]), "phase9_live_proof_signing_response_invalid"
    )


def _request_rollback_authorization(
    *, request: Mapping[str, object], key_id: str
) -> bytes:
    request_raw = _canonical(dict(request))
    request_sha256 = _sha(request_raw)
    _write_pipe_document(ROLLBACK_REQUEST_FD, request)
    response = _read_pipe_document(ROLLBACK_RESPONSE_FD)
    signature = response.get("signature")
    if (
        set(response) != {"schema_version", "result", "request_sha256", "signature"}
        or response.get("schema_version")
        != "governed-memory-phase9-rollback-signing-response-v1"
        or response.get("result") != "exact_delegated_rollback_signed"
        or response.get("request_sha256") != request_sha256
        or type(signature) is not dict
        or set(signature) != {"algorithm", "key_id", "value_base64"}
        or signature.get("algorithm") != "Ed25519"
        or signature.get("key_id") != key_id
    ):
        raise LiveProofError("phase9_live_proof_signing_response_invalid")
    try:
        decoded = base64.b64decode(str(signature["value_base64"]), validate=True)
    except (ValueError, TypeError) as error:
        raise LiveProofError("phase9_live_proof_signing_response_invalid") from error
    if len(decoded) != 64:
        raise LiveProofError("phase9_live_proof_signing_response_invalid")
    try:
        payload_raw = base64.b64decode(
            str(request["rollback_authorization_payload_base64"]), validate=True
        )
        payload = _parse_canonical_object(
            payload_raw, "phase9_live_proof_signing_response_invalid"
        )
    except (ValueError, TypeError) as error:
        raise LiveProofError("phase9_live_proof_signing_response_invalid") from error
    return _canonical(
        {
            "schema_version": ROLLBACK_AUTHORIZATION_SCHEMA,
            "payload": payload,
            "signature": signature,
        }
    )


def _build_rollback_documents(
    *,
    context: ProofContext,
    install_receipt: Mapping[str, object],
) -> RollbackDocuments:
    install = verify_install_receipt(install_receipt)
    resolved = _resolved_store_spec()
    resources = _load_install_resources(
        install_receipt=install, resolved_store_spec=resolved
    )
    eligibility = _rollback_eligibility_seed(
        install_receipt=install, resources=resources
    )
    plan_sha256 = empty_rollback_plan_sha256(eligibility, resources)
    evidence, unused = verified_rollback_resource_parts(resources)
    scope = {
        "schema_version": ROLLBACK_SCOPE_SCHEMA,
        "operation": ROLLBACK_OPERATION,
        "authorization_namespace": AUTHORIZATION_NAMESPACE,
        "thread_id": THREAD_ID,
        "scope_id": ROLLBACK_SCOPE_ID,
        "candidate_git_commit": context.inputs.candidate_git_commit,
        "candidate_git_tree": context.inputs.candidate_git_tree,
        "package_manifest_sha256": context.inputs.package_manifest_sha256,
        "controller_runtime_receipt_sha256": (
            context.inputs.controller_runtime_receipt_sha256
        ),
        "installation_execution_id": install["execution_id"],
        "installation_receipt_sha256": install["receipt_sha256"],
        "rollback_plan_sha256": plan_sha256,
        "exact_targets_sha256": evidence.exact_targets_sha256,
        "eligibility_receipt_sha256": eligibility["receipt_sha256"],
        "resource_ledger_head_sha256": evidence.resource_ledger_head_sha256,
        "empty_only_policy": {
            "pilot_ever_started": False,
            "postgresql_user_rows": 0,
            "projection_queue_rows": 0,
            "qdrant_points": 0,
            "active_clients": 0,
            "legacy_imports": 0,
        },
        "retention_policy": {
            "authorization_nonce_retained": True,
            "execution_journal_retained": True,
            "authority_anchor_retained": True,
            "resource_identity_ledger_retained": True,
            "install_and_rollback_receipts_retained": True,
        },
    }
    scope_raw = _canonical(scope)
    scope_sha256 = _sha(scope_raw)
    permit = context.permit
    key_id = permit.key_id
    delegated = permit.document
    payload = {
        "schema_version": ROLLBACK_AUTHORIZATION_PAYLOAD_SCHEMA,
        "authorization_id": "phase9-disposable-live-rollback-auth-000001",
        "authorization_namespace": AUTHORIZATION_NAMESPACE,
        "thread_id": THREAD_ID,
        "scope_id": ROLLBACK_SCOPE_ID,
        "scope_sha256": scope_sha256,
        "key_id": key_id,
        "approval_phrase": (
            "APPROVE GOVERNED MEMORY EMPTY STORE ROLLBACK " + scope_sha256
        ),
        "nonce": permit.rollback_nonce,
        "issued_at": delegated["issued_at"],
        "not_before": delegated["not_before"],
        "expires_at": delegated["expires_at"],
        "single_use": True,
    }
    request = {
        "schema_version": "governed-memory-phase9-rollback-signing-request-v1",
        "permit_nonce": delegated["permit_nonce"],
        "candidate_git_commit": context.inputs.candidate_git_commit,
        "candidate_git_tree": context.inputs.candidate_git_tree,
        "package_manifest_sha256": context.inputs.package_manifest_sha256,
        "controller_runtime_receipt_sha256": (
            context.inputs.controller_runtime_receipt_sha256
        ),
        "exact_targets_sha256": evidence.exact_targets_sha256,
        "installation_execution_id": install["execution_id"],
        "installation_receipt_sha256": install["receipt_sha256"],
        "resource_ledger_head_sha256": evidence.resource_ledger_head_sha256,
        "eligibility_receipt_sha256": eligibility["receipt_sha256"],
        "rollback_scope_base64": base64.b64encode(scope_raw).decode("ascii"),
        "rollback_authorization_payload_base64": base64.b64encode(
            _canonical(payload)
        ).decode("ascii"),
        "eligibility_receipt": dict(eligibility),
    }
    authorization_raw = _request_rollback_authorization(
        request=request,
        key_id=key_id,
    )
    return RollbackDocuments(
        scope=scope_raw,
        authorization=authorization_raw,
        trust_bundle=permit.install_documents.trust_bundle,
        key_id=key_id,
        eligibility=eligibility,
        resources=resources,
        resolved_store_spec=resolved,
    )


def _verified_rollback_capability(context: ProofContext) -> object:
    documents = context.rollback_documents
    if documents is None:
        raise LiveProofError("phase9_live_proof_rollback_context_absent")
    eligibility = documents.eligibility
    resource_evidence, unused = verified_rollback_resource_parts(
        documents.resources
    )
    bindings = EmptyRollbackExpectedBindings(
        candidate_git_commit=context.inputs.candidate_git_commit,
        candidate_git_tree=context.inputs.candidate_git_tree,
        package_manifest_sha256=context.inputs.package_manifest_sha256,
        controller_runtime_receipt_sha256=(
            context.inputs.controller_runtime_receipt_sha256
        ),
        installation_execution_id=str(
            eligibility["installation_execution_id"]
        ),
        installation_receipt_sha256=str(
            eligibility["installation_receipt_sha256"]
        ),
        rollback_plan_sha256=empty_rollback_plan_sha256(
            eligibility, documents.resources
        ),
        exact_targets_sha256=resource_evidence.exact_targets_sha256,
        eligibility_receipt_sha256=str(eligibility["receipt_sha256"]),
        resource_ledger_head_sha256=(
            resource_evidence.resource_ledger_head_sha256
        ),
    )
    return verify_empty_rollback_execution_capability(
        documents.scope,
        documents.authorization,
        documents.trust_bundle,
        expected_namespace=AUTHORIZATION_NAMESPACE,
        expected_thread_id=THREAD_ID,
        expected_scope_id=ROLLBACK_SCOPE_ID,
        expected_key_id=documents.key_id,
        expected_trust_bundle_sha256=_sha(documents.trust_bundle),
        expected_bindings=bindings,
        verified_package_capability=context.verified_package_capability,
        verified_controller_runtime_capability=(
            context.verified_runtime_capability
        ),
    )


def _run_rollback_worker(context: ProofContext) -> Mapping[str, object]:
    documents = context.rollback_documents
    if documents is None:
        raise LiveProofError("phase9_live_proof_rollback_context_absent")
    capability = _verified_rollback_capability(context)
    state = _authority_state()
    receipt_store = DurableReceiptStore.production()
    with GlobalExecutionLock(GLOBAL_LOCK_PATH, expected_uid=0) as lock:
        held = lock.held_capability()

        receipt = run_authorized_empty_store_rollback(
            verified_rollback_capability=capability,
            verified_package_capability=context.verified_package_capability,
            verified_controller_runtime_capability=(
                context.verified_runtime_capability
            ),
            eligibility_receipt=documents.eligibility,
            resources=documents.resources,
            authority_state=state,
            clock=KernelUtcClock(),
            held_lock=held,
            resolved_store_spec=documents.resolved_store_spec,
            receipt_store=receipt_store,
        )
        return dict(receipt.canonical_receipt)


def _run_absence_worker(context: ProofContext) -> Mapping[str, object]:
    # A completed public rollback replay does not trust the earlier receipt.
    # It reacquires the exact ledger-bound composition and re-proves terminal
    # resource absence before returning the same durable receipt.
    replay_receipt = _run_rollback_worker(context)
    replay = verify_empty_rollback_receipt(replay_receipt)
    retained_runtime = verify_controller_runtime_capability(
        context.verified_package_capability,
        runtime_build_receipt_json=context.runtime_receipt,
    )
    result = {
        "completed_public_rollback_replayed": True,
        "exact_resources_absent": replay["exact_resources_absent"],
        "stores_installed": replay["stores_installed"],
        "stores_supervisor_installed": replay[
            "stores_supervisor_installed"
        ],
        "controller_runtime_capability_reverified": (
            retained_runtime is not None
        ),
    }
    if (
        result["exact_resources_absent"] is not True
        or result["stores_installed"] is not False
        or result["stores_supervisor_installed"] is not False
        or result["controller_runtime_capability_reverified"] is not True
    ):
        raise LiveProofError("phase9_live_proof_terminal_absence_failed")
    return MappingProxyType(result)


def _dispatch_worker(
    mode: WorkerMode, context: ProofContext
) -> Mapping[str, object]:
    if mode in {WorkerMode.INSTALL, WorkerMode.RESUME_INSTALL}:
        return _run_install_worker(context)
    if mode in {WorkerMode.ROLLBACK, WorkerMode.RESUME_ROLLBACK}:
        return _run_rollback_worker(context)
    if mode is WorkerMode.VERIFY_ABSENCE:
        return _run_absence_worker(context)
    raise LiveProofError("phase9_live_proof_worker_mode_refused")


def _child_write(descriptor: int, value: Mapping[str, object]) -> None:
    raw = _canonical(value)
    if len(raw) > MAX_WORKER_RESULT_BYTES:
        raise LiveProofError("phase9_live_proof_worker_result_too_large")
    view = memoryview(raw)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise LiveProofError("phase9_live_proof_worker_pipe_failed")
        view = view[written:]


def _fork_worker(
    mode: WorkerMode, context: ProofContext
) -> tuple[int, int]:
    if not hasattr(os, "fork"):
        raise LiveProofError("phase9_live_proof_linux_fork_required")
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        for signing_fd in (ROLLBACK_REQUEST_FD, ROLLBACK_RESPONSE_FD):
            if signing_fd != write_fd:
                try:
                    os.close(signing_fd)
                except OSError:
                    pass
        exit_code = 1
        try:
            result = _dispatch_worker(mode, context)
            _child_write(write_fd, {"ok": True, "result": dict(result)})
            exit_code = 0
        except BaseException as error:
            code = str(error)
            if not code or any(character in code for character in "\r\n\x00"):
                code = "phase9_live_proof_worker_failed"
            try:
                _child_write(write_fd, {"ok": False, "error_code": code})
            except BaseException:
                pass
        finally:
            os.close(write_fd)
            os._exit(exit_code)
    os.close(write_fd)
    return pid, read_fd


def _read_pipe(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            block = os.read(
                descriptor, min(65536, MAX_WORKER_RESULT_BYTES + 1 - total)
            )
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > MAX_WORKER_RESULT_BYTES:
                raise LiveProofError("phase9_live_proof_worker_result_too_large")
    finally:
        os.close(descriptor)
    return b"".join(chunks)


def _wait_worker(pid: int, read_fd: int) -> Mapping[str, object]:
    raw = _read_pipe(read_fd)
    waited, status = os.waitpid(pid, 0)
    if waited != pid:
        raise LiveProofError("phase9_live_proof_worker_wait_failed")
    try:
        document = _parse_canonical_object(
            raw, "phase9_live_proof_worker_result_invalid"
        )
    except LiveProofError:
        raise LiveProofError("phase9_live_proof_worker_failed") from None
    if (
        not os.WIFEXITED(status)
        or os.WEXITSTATUS(status) != 0
        or document.get("ok") is not True
        or type(document.get("result")) is not dict
    ):
        code = document.get("error_code")
        raise LiveProofError(
            code if type(code) is str else "phase9_live_proof_worker_failed"
        )
    return MappingProxyType(dict(document["result"]))


def _execution_directories() -> frozenset[str]:
    names: set[str] = set()
    try:
        with os.scandir(EXECUTIONS_ROOT) as entries:
            for entry in entries:
                if (
                    _EXECUTION_DIRECTORY_RE.fullmatch(entry.name) is not None
                    and entry.is_dir(follow_symlinks=False)
                ):
                    names.add(entry.name)
    except OSError as error:
        raise LiveProofError("phase9_live_proof_execution_root_unavailable") from error
    return frozenset(names)


def _journal_has_boundary(path: Path, step_id: str) -> bool:
    try:
        raw = _read_regular_no_follow(path, maximum=16 * 1024 * 1024)
    except LiveProofError:
        return False
    if raw and not raw.endswith(b"\n"):
        return False
    for line in raw.splitlines():
        try:
            record = json.loads(line.decode("ascii"))
        except (UnicodeError, json.JSONDecodeError):
            return False
        if type(record) is not dict or _canonical(record) != line:
            return False
        if (
            record.get("step_id") == step_id
            and record.get("event") == JOURNAL_APPLIED_EVENT
        ):
            return True
    return False


def _kill_after_new_durable_boundary(
    *,
    pid: int,
    read_fd: int,
    prior_directories: frozenset[str],
    journal_name: str,
    step_id: str,
) -> str:
    deadline = time.monotonic() + BOUNDARY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        waited, status = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            raw = _read_pipe(read_fd)
            try:
                detail = _parse_canonical_object(
                    raw, "phase9_live_proof_worker_result_invalid"
                )
                code = detail.get("error_code")
            except LiveProofError:
                code = None
            raise LiveProofError(
                code
                if type(code) is str
                else "phase9_live_proof_worker_exited_before_boundary"
            )
        new_directories = _execution_directories() - prior_directories
        if len(new_directories) > 1:
            raise LiveProofError(
                "phase9_live_proof_parallel_execution_detected"
            )
        if len(new_directories) == 1:
            execution_id = next(iter(new_directories))
            journal_path = EXECUTIONS_ROOT / execution_id / journal_name
            if _journal_has_boundary(journal_path, step_id):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError as error:
                    raise LiveProofError(
                        "phase9_live_proof_worker_exited_at_boundary"
                    ) from error
                waited, status = os.waitpid(pid, 0)
                os.close(read_fd)
                if (
                    waited != pid
                    or not os.WIFSIGNALED(status)
                    or os.WTERMSIG(status) != signal.SIGKILL
                ):
                    raise LiveProofError(
                        "phase9_live_proof_process_death_unproved"
                    )
                return execution_id
        time.sleep(POLL_INTERVAL_SECONDS)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    os.waitpid(pid, 0)
    os.close(read_fd)
    raise LiveProofError("phase9_live_proof_boundary_timeout")


def _boundary_sha256(step_id: str) -> str:
    return _document_sha(
        {
            "event": JOURNAL_APPLIED_EVENT,
            "step_id": step_id,
        }
    )


def verify_live_proof_receipt(
    receipt: Mapping[str, object],
) -> dict[str, object]:
    """Strictly verify the content-free live proof receipt shape and digest."""

    if type(receipt) is not dict or set(receipt) != _LIVE_PROOF_RECEIPT_KEYS:
        raise LiveProofError("phase9_live_proof_receipt_invalid")
    if (
        receipt.get("schema_version") != LIVE_PROOF_RECEIPT_SCHEMA
        or receipt.get("result")
        != "disposable_install_crash_resume_and_empty_rollback_proved"
        or receipt.get("observation_scope")
        != "one_bounded_disposable_linux_execution_terminal_snapshot"
        or receipt.get("cold_restart_scope")
        != "controller_worker_process_only"
        or receipt.get("exact_targets_absent_count") != 15
        or receipt.get("source_postgres_read_count") != 0
        or receipt.get("source_postgres_write_count") != 0
        or receipt.get("provider_calls") != 0
        or receipt.get("production_data_read") is not False
        or receipt.get("application_services_installed") is not False
        or receipt.get("activation_performed") is not False
        or receipt.get("host_reboot_proven") is not False
        or receipt.get(
            "persistent_store_restart_supervision_and_boot_recovery_proven"
        )
        is not False
        or receipt.get("issuer_or_host_death_durable_cleanup_proven")
        is not False
        or receipt.get("terminal_absence_is_continuous_guarantee")
        is not False
    ):
        raise LiveProofError("phase9_live_proof_receipt_invalid")
    required_true = (
        "install_process_death_observed",
        "install_exact_resume_completed",
        "cold_controller_process_restart_terminal_postflight_verified",
        "rollback_process_death_observed",
        "rollback_exact_resume_completed",
        "public_install_entrypoint_used",
        "public_empty_rollback_entrypoint_used",
        "fresh_r06_semantic_empty_recheck_required",
        "completed_public_rollback_replayed",
        "exact_resources_absent_at_terminal_observation",
        "controller_runtime_capability_reverified",
    )
    required_false = (
        "stores_installed_at_terminal_observation",
        "stores_supervisor_installed_at_terminal_observation",
    )
    if any(receipt.get(key) is not True for key in required_true) or any(
        receipt.get(key) is not False for key in required_false
    ):
        raise LiveProofError("phase9_live_proof_receipt_invalid")
    for key in (
        "package_manifest_sha256",
        "controller_runtime_receipt_sha256",
        "live_proof_receipt_schema_sha256",
        "external_proof_permit_sha256",
        "authorization_text_sha256",
        "installation_execution_id",
        "installation_receipt_sha256",
        "image_identity_set_sha256",
        "empty_rollback_execution_id",
        "empty_rollback_receipt_sha256",
        "retained_audit_set_sha256",
        "install_process_death_boundary_sha256",
        "rollback_process_death_boundary_sha256",
    ):
        if type(receipt.get(key)) is not str or _HASH_RE.fullmatch(
            str(receipt[key])
        ) is None:
            raise LiveProofError("phase9_live_proof_receipt_invalid")
    for key in ("candidate_git_commit", "candidate_git_tree"):
        if type(receipt.get(key)) is not str or _COMMIT_RE.fullmatch(
            str(receipt[key])
        ) is None:
            raise LiveProofError("phase9_live_proof_receipt_invalid")
    expected_digest = _document_sha(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    if receipt.get("receipt_sha256") != expected_digest:
        raise LiveProofError("phase9_live_proof_receipt_invalid")
    return dict(receipt)


def _proof_receipt(
    *,
    context: ProofContext,
    install_receipt: Mapping[str, object],
    rollback_receipt: Mapping[str, object],
    absence: Mapping[str, object],
) -> dict[str, object]:
    install = verify_install_receipt(install_receipt)
    rollback = verify_empty_rollback_receipt(rollback_receipt)
    schema_raw = context.artifacts.get(LIVE_PROOF_RECEIPT_SCHEMA_RELATIVE)
    if type(schema_raw) is not bytes:
        raise LiveProofError("phase9_live_proof_receipt_schema_missing")
    if (
        install["source_postgres_read_count"] != 0
        or install["source_postgres_write_count"] != 0
        or install["provider_calls"] != 0
        or install["production_data_read"] is not False
        or install["application_services_installed"] is not False
        or install["activation_performed"] is not False
        or rollback["source_postgres_read_count"] != 0
        or rollback["source_postgres_write_count"] != 0
        or rollback["provider_calls"] != 0
        or rollback["production_data_read"] is not False
        or rollback["application_services_installed"] is not False
        or rollback["activation_performed"] is not False
        or rollback["exact_targets_absent_count"] != 15
    ):
        raise LiveProofError("phase9_live_proof_receipt_effect_boundary_invalid")
    receipt: dict[str, object] = {
        "schema_version": LIVE_PROOF_RECEIPT_SCHEMA,
        "result": "disposable_install_crash_resume_and_empty_rollback_proved",
        "observation_scope": (
            "one_bounded_disposable_linux_execution_terminal_snapshot"
        ),
        "candidate_git_commit": context.inputs.candidate_git_commit,
        "candidate_git_tree": context.inputs.candidate_git_tree,
        "package_manifest_sha256": context.inputs.package_manifest_sha256,
        "controller_runtime_receipt_sha256": (
            context.inputs.controller_runtime_receipt_sha256
        ),
        "live_proof_receipt_schema_sha256": _sha(schema_raw),
        "external_proof_permit_sha256": _sha(context.permit.raw),
        "authorization_text_sha256": AUTHORIZED_TEXT_SHA256,
        "installation_execution_id": install["execution_id"],
        "installation_receipt_sha256": install["receipt_sha256"],
        "image_identity_set_sha256": install["image_identity_set_sha256"],
        "empty_rollback_execution_id": rollback["execution_id"],
        "empty_rollback_receipt_sha256": rollback["receipt_sha256"],
        "retained_audit_set_sha256": rollback[
            "retained_audit_set_sha256"
        ],
        "install_process_death_boundary_sha256": _boundary_sha256(
            INSTALL_KILL_STEP
        ),
        "rollback_process_death_boundary_sha256": _boundary_sha256(
            ROLLBACK_KILL_STEP
        ),
        "install_process_death_observed": True,
        "install_exact_resume_completed": True,
        "cold_controller_process_restart_terminal_postflight_verified": True,
        "cold_restart_scope": "controller_worker_process_only",
        "host_reboot_proven": False,
        "persistent_store_restart_supervision_and_boot_recovery_proven": False,
        "rollback_process_death_observed": True,
        "rollback_exact_resume_completed": True,
        "public_install_entrypoint_used": True,
        "public_empty_rollback_entrypoint_used": True,
        "fresh_r06_semantic_empty_recheck_required": True,
        "completed_public_rollback_replayed": absence[
            "completed_public_rollback_replayed"
        ],
        "exact_targets_absent_count": rollback[
            "exact_targets_absent_count"
        ],
        "exact_resources_absent_at_terminal_observation": absence[
            "exact_resources_absent"
        ],
        "terminal_absence_is_continuous_guarantee": False,
        "stores_installed_at_terminal_observation": absence[
            "stores_installed"
        ],
        "stores_supervisor_installed_at_terminal_observation": absence[
            "stores_supervisor_installed"
        ],
        "controller_runtime_capability_reverified": absence[
            "controller_runtime_capability_reverified"
        ],
        "source_postgres_read_count": rollback[
            "source_postgres_read_count"
        ],
        "source_postgres_write_count": rollback[
            "source_postgres_write_count"
        ],
        "provider_calls": rollback["provider_calls"],
        "production_data_read": rollback["production_data_read"],
        "application_services_installed": rollback[
            "application_services_installed"
        ],
        "activation_performed": rollback["activation_performed"],
        "issuer_or_host_death_durable_cleanup_proven": False,
    }
    receipt["receipt_sha256"] = _document_sha(receipt)
    return verify_live_proof_receipt(receipt)


def _complete_exact_rollback_recovery(
    *,
    context: ProofContext,
    install_receipt: Mapping[str, object],
    rollback_context: ProofContext | None,
) -> None:
    """Recover only the exact installed ledger through public rollback."""

    selected = rollback_context
    if selected is None:
        documents = _build_rollback_documents(
            context=context,
            install_receipt=install_receipt,
        )
        selected = replace(context, rollback_documents=documents)
    _verified_rollback_capability(selected)
    pid, descriptor = _fork_worker(WorkerMode.RESUME_ROLLBACK, selected)
    recovered = verify_empty_rollback_receipt(
        _wait_worker(pid, descriptor)
    )
    if recovered["exact_resources_absent"] is not True:
        raise LiveProofError("phase9_live_proof_exact_rollback_recovery_failed")
    pid, descriptor = _fork_worker(WorkerMode.VERIFY_ABSENCE, selected)
    absence = _wait_worker(pid, descriptor)
    if absence.get("exact_resources_absent") is not True:
        raise LiveProofError("phase9_live_proof_exact_rollback_recovery_failed")


def _recover_verified_install_receipt(
    *,
    context: ProofContext,
    execution_id: str,
) -> Mapping[str, object]:
    """Recover a lost worker result without selecting a receipt path."""

    try:
        durable = DurableReceiptStore.production().read(
            ReceiptArtifact.INSTALL, execution_id
        )
        receipt = verify_install_receipt(durable.canonical_receipt)
    except Exception:
        # If the receipt was not yet committed, one bounded invocation of the
        # same public install composition resumes the already claimed journal.
        pid, descriptor = _fork_worker(WorkerMode.RESUME_INSTALL, context)
        receipt = verify_install_receipt(_wait_worker(pid, descriptor))
    if receipt["execution_id"] != execution_id:
        raise LiveProofError("phase9_live_proof_install_recovery_mismatch")
    return MappingProxyType(dict(receipt))


def run_live_proof(inputs: ProofInputs) -> Mapping[str, object]:
    """Run the one exact install/kill/resume/rollback/kill/resume proof."""

    _prepare_fixed_substrate()
    context, unused_permit = _verified_install_context(inputs)
    rollback_context: ProofContext | None = None
    verified_install: Mapping[str, object] | None = None
    # The install execution identifier is a deterministic function of the
    # pre-signed install capability.  Compute it before the first worker so a
    # death immediately after durable authority claim can still resume the
    # exact install rather than falling back to a name-selected cleanup.
    installation_execution_id = _expected_install_execution_id(context)
    try:
        before_install = _execution_directories()
        pid, descriptor = _fork_worker(WorkerMode.INSTALL, context)
        killed_install_execution = _kill_after_new_durable_boundary(
            pid=pid,
            read_fd=descriptor,
            prior_directories=before_install,
            journal_name="journal.jsonl",
            step_id=INSTALL_KILL_STEP,
        )
        if killed_install_execution != installation_execution_id:
            raise LiveProofError("phase9_live_proof_install_execution_mismatch")
        pid, descriptor = _fork_worker(WorkerMode.RESUME_INSTALL, context)
        install_receipt = _wait_worker(pid, descriptor)
        install = verify_install_receipt(install_receipt)
        verified_install = MappingProxyType(dict(install))
        if install["execution_id"] != killed_install_execution:
            raise LiveProofError("phase9_live_proof_install_resume_mismatch")

        rollback_documents = _build_rollback_documents(
            context=context,
            install_receipt=install,
        )
        rollback_context = replace(
            context, rollback_documents=rollback_documents
        )
        # Verification mints the distinct rollback authority before a worker
        # can create its durable claim or perform any rollback effect.
        _verified_rollback_capability(rollback_context)

        before_rollback = _execution_directories()
        pid, descriptor = _fork_worker(
            WorkerMode.ROLLBACK, rollback_context
        )
        killed_rollback_execution = _kill_after_new_durable_boundary(
            pid=pid,
            read_fd=descriptor,
            prior_directories=before_rollback,
            journal_name="rollback.jsonl",
            step_id=ROLLBACK_KILL_STEP,
        )
        pid, descriptor = _fork_worker(
            WorkerMode.RESUME_ROLLBACK, rollback_context
        )
        rollback_receipt = _wait_worker(pid, descriptor)
        rollback = verify_empty_rollback_receipt(rollback_receipt)
        if rollback["execution_id"] != killed_rollback_execution:
            raise LiveProofError("phase9_live_proof_rollback_resume_mismatch")
        pid, descriptor = _fork_worker(
            WorkerMode.VERIFY_ABSENCE, rollback_context
        )
        absence = _wait_worker(pid, descriptor)
        return MappingProxyType(
            _proof_receipt(
                context=rollback_context,
                install_receipt=install,
                rollback_receipt=rollback,
                absence=absence,
            )
        )
    except BaseException as original_error:
        # Never introduce a generic cleanup surface.  As soon as a durable
        # install receipt has verified, even a failure while constructing its
        # first rollback capability must retry that exact construction and
        # finish only through the public ledger-bound rollback.
        if verified_install is None and installation_execution_id is not None:
            try:
                verified_install = _recover_verified_install_receipt(
                    context=context,
                    execution_id=installation_execution_id,
                )
            except BaseException as recovery_error:
                raise LiveProofError(
                    "phase9_live_proof_install_recovery_failed"
                ) from recovery_error
        if verified_install is not None:
            try:
                _complete_exact_rollback_recovery(
                    context=context,
                    install_receipt=verified_install,
                    rollback_context=rollback_context,
                )
            except BaseException as recovery_error:
                raise LiveProofError(
                    "phase9_live_proof_exact_rollback_recovery_failed"
                ) from recovery_error
        raise original_error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_disposable_installation_live_proof.py",
        allow_abbrev=False,
    )
    parser.add_argument(
        "mode",
        choices=(
            "run",
            *(item.value for item in WorkerMode),
        ),
    )
    parser.add_argument("--candidate-git-commit", required=True)
    parser.add_argument("--candidate-git-tree", required=True)
    parser.add_argument("--package-manifest-sha256", required=True)
    parser.add_argument("--controller-runtime-receipt-sha256", required=True)
    return parser


def parse_inputs(argv: Sequence[str]) -> tuple[str, ProofInputs]:
    values = _parser().parse_args(tuple(argv))
    inputs = ProofInputs(
        candidate_git_commit=values.candidate_git_commit,
        candidate_git_tree=values.candidate_git_tree,
        package_manifest_sha256=values.package_manifest_sha256,
        controller_runtime_receipt_sha256=(
            values.controller_runtime_receipt_sha256
        ),
    )
    return values.mode, inputs


def main(argv: Sequence[str] | None = None) -> int:
    mode, inputs = parse_inputs(sys.argv[1:] if argv is None else argv)
    # Worker modes are real, closed in-process modes used only through forked
    # ProofContext objects.  They cannot be invoked externally with loose
    # filesystem or authority inputs.
    if mode != "run":
        raise LiveProofError("phase9_live_proof_worker_context_required")
    receipt = run_live_proof(inputs)
    sys.stdout.buffer.write(_canonical(dict(receipt)) + b"\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LiveProofError as error:
        sys.stderr.write(str(error) + "\n")
        raise SystemExit(1)
