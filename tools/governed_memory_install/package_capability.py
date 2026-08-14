from __future__ import annotations

"""Closed, in-memory verification of one dormant-store install package.

This module has no filesystem, command, network, secret, or clock access.  It
rehashes every artifact named by the exact package manifest and mints an
opaque capability bound to the already-verified signed authorization scope.
"""

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType
from typing import Final

from .authority import (
    AuthorityVerificationError,
    canonical_json_bytes,
    _execution_capability_evidence,
)


RESULT_TYPE: Final = "verified_dormant_install_package_v1"
MAX_PACKAGE_BYTES: Final = 32 * 1024 * 1024
MAX_ARTIFACT_BYTES: Final = 8 * 1024 * 1024
_HASH_RE: Final = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_COMMIT_RE: Final = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_CONTRACT_PATH: Final = "ops/governed_memory/installation/current/contract.json"
_PLAN_PATH: Final = (
    "ops/governed_memory/installation/current/controller_plan.json"
)
_STORE_SPEC_PATH: Final = "ops/governed_memory/installation/store_spec-v3.json"
_RESOURCE_IDENTITY_PATH: Final = (
    "tools/governed_memory_install/resource_identity.py"
)
_CONTROLLER_RUNTIME_CONTRACT_PATH: Final = (
    "ops/governed_memory/installation/current/controller_runtime_contract.json"
)
_CONTROLLER_REQUIREMENTS_LOCK_PATH: Final = (
    "ops/governed_memory/controller-requirements.lock"
)
_SUPERVISOR_LAUNCHER_PATH: Final = (
    "tools/governed_memory_install/store_supervisor_launcher.py"
)
_REQUIRED_PATHS: Final = frozenset(
    {
        _CONTRACT_PATH,
        _PLAN_PATH,
        _STORE_SPEC_PATH,
        _RESOURCE_IDENTITY_PATH,
        _CONTROLLER_RUNTIME_CONTRACT_PATH,
        _CONTROLLER_REQUIREMENTS_LOCK_PATH,
        _SUPERVISOR_LAUNCHER_PATH,
    }
)
_SCOPE_BINDING_KEYS: Final = frozenset(
    {
        "candidate_git_commit",
        "candidate_git_tree",
        "package_manifest_sha256",
        "controller_contract_sha256",
        "execution_plan_sha256",
        "exact_targets_sha256",
        "controller_runtime_receipt_sha256",
    }
)


class PackageCapabilityError(RuntimeError):
    """Content-free refusal for an incomplete or changed package."""


@dataclass(frozen=True, slots=True)
class VerifiedPackageEvidence:
    result_type: str
    authorization_sha256: str
    scope_sha256: str
    candidate_git_commit: str
    candidate_git_tree: str
    package_manifest_sha256: str
    controller_contract_sha256: str
    execution_plan_sha256: str
    exact_targets_sha256: str
    controller_runtime_receipt_sha256: str
    store_spec_sha256: str
    resource_identity_implementation_sha256: str
    controller_runtime_contract_sha256: str
    controller_requirements_lock_sha256: str
    supervisor_launcher_sha256: str
    controller_model_sha256: str
    artifact_count: int
    aggregate_artifact_sha256: str


_PACKAGE_TOKEN = object()


class _VerifiedPackageCapability:
    __slots__ = ("_artifacts", "_evidence", "_scope", "_token")

    def __init__(
        self,
        evidence: VerifiedPackageEvidence,
        scope: dict[str, object],
        artifacts: dict[str, bytes],
        token: object,
    ) -> None:
        if token is not _PACKAGE_TOKEN:
            raise PackageCapabilityError("package_capability_invalid")
        self._evidence = evidence
        self._scope = MappingProxyType(dict(scope))
        self._artifacts = MappingProxyType(dict(artifacts))
        self._token = token

    def __repr__(self) -> str:
        return "VerifiedPackageCapability(<content-redacted>)"


def _package_capability_parts(
    value: object,
) -> tuple[VerifiedPackageEvidence, Mapping[str, object], Mapping[str, bytes]]:
    if (
        type(value) is not _VerifiedPackageCapability
        or value._token is not _PACKAGE_TOKEN
        or type(value._evidence) is not VerifiedPackageEvidence
    ):
        raise PackageCapabilityError("package_capability_invalid")
    return value._evidence, value._scope, value._artifacts


def verified_package_evidence(value: object) -> VerifiedPackageEvidence:
    """Return immutable, content-free evidence from an opaque package capability."""

    evidence, _scope, _artifacts = _package_capability_parts(value)
    return evidence


def _raw(value: bytes | str, code: str) -> bytes:
    if isinstance(value, bytes):
        result = bytes(value)
    elif isinstance(value, str):
        try:
            result = value.encode("utf-8")
        except UnicodeError as error:
            raise PackageCapabilityError(code) from error
    else:
        raise PackageCapabilityError(code)
    if not result or len(result) > MAX_ARTIFACT_BYTES:
        raise PackageCapabilityError(code)
    return result


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PackageCapabilityError("package_json_duplicate_key")
        result[key] = value
    return result


def _document(raw: bytes, code: str) -> dict[str, object]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique,
            parse_constant=lambda unused: (_ for _ in ()).throw(
                PackageCapabilityError(code)
            ),
        )
    except PackageCapabilityError:
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PackageCapabilityError(code) from error
    if type(value) is not dict:
        raise PackageCapabilityError(code)
    return value


def _scope_document(
    signed_scope_json: bytes | str, expected_sha256: str
) -> tuple[bytes, dict[str, object]]:
    raw = _raw(signed_scope_json, "package_scope_invalid")
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise PackageCapabilityError("package_scope_mismatch")
    scope = _document(raw, "package_scope_invalid")
    if canonical_json_bytes(scope) != raw:
        raise PackageCapabilityError("package_scope_not_canonical")
    if not _SCOPE_BINDING_KEYS.issubset(scope):
        raise PackageCapabilityError("package_scope_bindings_missing")
    for key in ("candidate_git_commit", "candidate_git_tree"):
        if not isinstance(scope[key], str) or _COMMIT_RE.fullmatch(scope[key]) is None:
            raise PackageCapabilityError("package_scope_binding_invalid")
    for key in _SCOPE_BINDING_KEYS - {"candidate_git_commit", "candidate_git_tree"}:
        if not isinstance(scope[key], str) or _HASH_RE.fullmatch(scope[key]) is None:
            raise PackageCapabilityError("package_scope_binding_invalid")
    return raw, scope


def verify_install_package_capability(
    verified_scope_capability: object,
    *,
    signed_scope_json: bytes | str,
    package_manifest_json: bytes | str,
    artifact_bytes: Mapping[str, bytes | str],
) -> object:
    """Verify all exact package bytes and mint an opaque package capability."""

    try:
        scope_evidence = _execution_capability_evidence(
            verified_scope_capability
        )
    except AuthorityVerificationError as error:
        raise PackageCapabilityError("package_scope_capability_invalid") from error
    _, scope = _scope_document(signed_scope_json, scope_evidence.scope_sha256)
    manifest_raw = _raw(package_manifest_json, "package_manifest_invalid")
    if len(manifest_raw) > MAX_PACKAGE_BYTES:
        raise PackageCapabilityError("package_manifest_too_large")
    if hashlib.sha256(manifest_raw).hexdigest() != scope["package_manifest_sha256"]:
        raise PackageCapabilityError("package_manifest_scope_mismatch")
    manifest = _document(manifest_raw, "package_manifest_invalid")
    if set(manifest) != {"schema_version", "state", "artifacts"}:
        raise PackageCapabilityError("package_manifest_shape_invalid")
    expected = manifest["artifacts"]
    if type(expected) is not dict or not expected:
        raise PackageCapabilityError("package_artifact_index_invalid")
    if set(expected) != set(artifact_bytes) or not _REQUIRED_PATHS.issubset(expected):
        raise PackageCapabilityError("package_artifact_set_mismatch")
    closed: dict[str, bytes] = {}
    for path in sorted(expected):
        digest = expected[path]
        if (
            type(path) is not str
            or not path
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or type(digest) is not str
            or _HASH_RE.fullmatch(digest) is None
        ):
            raise PackageCapabilityError("package_artifact_index_invalid")
        raw = _raw(artifact_bytes[path], "package_artifact_invalid")
        if hashlib.sha256(raw).hexdigest() != digest:
            raise PackageCapabilityError("package_artifact_hash_mismatch")
        closed[path] = raw
    contract_raw = closed[_CONTRACT_PATH]
    plan_raw = closed[_PLAN_PATH]
    store_raw = closed[_STORE_SPEC_PATH]
    identity_raw = closed[_RESOURCE_IDENTITY_PATH]
    runtime_contract_raw = closed[_CONTROLLER_RUNTIME_CONTRACT_PATH]
    requirements_lock_raw = closed[_CONTROLLER_REQUIREMENTS_LOCK_PATH]
    supervisor_launcher_raw = closed[_SUPERVISOR_LAUNCHER_PATH]
    if hashlib.sha256(contract_raw).hexdigest() != scope["controller_contract_sha256"]:
        raise PackageCapabilityError("package_contract_scope_mismatch")
    if hashlib.sha256(plan_raw).hexdigest() != scope["execution_plan_sha256"]:
        raise PackageCapabilityError("package_plan_scope_mismatch")
    contract = _document(contract_raw, "package_contract_invalid")
    plan = _document(plan_raw, "package_plan_invalid")
    _document(store_raw, "package_store_spec_invalid")
    targets = contract.get("exact_targets")
    steps = plan.get("install_steps")
    if type(targets) is not dict or hashlib.sha256(
        canonical_json_bytes(targets)
    ).hexdigest() != scope["exact_targets_sha256"]:
        raise PackageCapabilityError("package_exact_targets_mismatch")
    if type(steps) is not list or len(steps) != 19:
        raise PackageCapabilityError("package_plan_steps_invalid")
    controller_model_sha256 = hashlib.sha256(
        canonical_json_bytes(steps)
    ).hexdigest()
    aggregate = hashlib.sha256()
    for path in sorted(closed):
        aggregate.update(path.encode("utf-8") + b"\x00")
        aggregate.update(hashlib.sha256(closed[path]).digest())
    evidence = VerifiedPackageEvidence(
        result_type=RESULT_TYPE,
        authorization_sha256=scope_evidence.authorization_sha256,
        scope_sha256=scope_evidence.scope_sha256,
        candidate_git_commit=str(scope["candidate_git_commit"]),
        candidate_git_tree=str(scope["candidate_git_tree"]),
        package_manifest_sha256=str(scope["package_manifest_sha256"]),
        controller_contract_sha256=str(scope["controller_contract_sha256"]),
        execution_plan_sha256=str(scope["execution_plan_sha256"]),
        exact_targets_sha256=str(scope["exact_targets_sha256"]),
        controller_runtime_receipt_sha256=str(
            scope["controller_runtime_receipt_sha256"]
        ),
        store_spec_sha256=hashlib.sha256(store_raw).hexdigest(),
        resource_identity_implementation_sha256=hashlib.sha256(
            identity_raw
        ).hexdigest(),
        controller_runtime_contract_sha256=hashlib.sha256(
            runtime_contract_raw
        ).hexdigest(),
        controller_requirements_lock_sha256=hashlib.sha256(
            requirements_lock_raw
        ).hexdigest(),
        supervisor_launcher_sha256=hashlib.sha256(
            supervisor_launcher_raw
        ).hexdigest(),
        controller_model_sha256=controller_model_sha256,
        artifact_count=len(closed),
        aggregate_artifact_sha256=aggregate.hexdigest(),
    )
    return _VerifiedPackageCapability(evidence, scope, closed, _PACKAGE_TOKEN)


__all__ = [
    "PackageCapabilityError",
    "VerifiedPackageEvidence",
    "verified_package_evidence",
    "verify_install_package_capability",
]
