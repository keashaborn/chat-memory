from __future__ import annotations

"""Offline, operation-distinct authority for an empty-store rollback.

Signature verification mints an opaque description only.  It cannot execute a
rollback, consume a nonce, read trusted time, or authorize installation or
activation.  A future rollback entrypoint must claim the nonce under the global
execution lock before any effect.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import binascii
import hashlib
import json
import re
from typing import Final, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .package_capability import (
    PackageCapabilityError,
    verified_package_evidence,
)
from .controller_runtime import (
    ControllerRuntimeCapabilityError,
    _controller_runtime_capability_evidence,
)


SCOPE_SCHEMA_VERSION: Final = "governed-memory-empty-store-rollback-scope-v2"
AUTHORIZATION_SCHEMA_VERSION: Final = (
    "governed-memory-external-authorization-envelope-v1"
)
AUTHORIZATION_PAYLOAD_SCHEMA_VERSION: Final = (
    "governed-memory-empty-store-rollback-authorization-v2"
)
TRUST_BUNDLE_SCHEMA_VERSION: Final = (
    "governed-memory-ed25519-public-key-trust-bundle-v1"
)
ROLLBACK_OPERATION: Final = "empty_store_rollback"
MAX_DOCUMENT_BYTES: Final = 64 * 1024
MAX_AUTHORIZATION_LIFETIME_SECONDS: Final = 15 * 60

_HASH_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z", re.ASCII)
_NONCE_RE = re.compile(r"[A-Za-z0-9_-]{32,128}\Z", re.ASCII)
_TIMESTAMP_RE = re.compile(
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z\Z",
    re.ASCII,
)

_SCOPE_KEYS = {
    "schema_version",
    "operation",
    "authorization_namespace",
    "thread_id",
    "scope_id",
    "candidate_git_commit",
    "candidate_git_tree",
    "package_manifest_sha256",
    "controller_runtime_receipt_sha256",
    "installation_receipt_sha256",
    "rollback_plan_sha256",
    "exact_targets_sha256",
    "eligibility_receipt_sha256",
    "resource_ledger_head_sha256",
    "empty_only_policy",
    "retention_policy",
}
_EMPTY_ONLY_POLICY = {
    "pilot_ever_started": False,
    "postgresql_user_rows": 0,
    "projection_queue_rows": 0,
    "qdrant_points": 0,
    "active_clients": 0,
    "legacy_imports": 0,
}
_RETENTION_POLICY = {
    "authorization_nonce_retained": True,
    "execution_journal_retained": True,
    "authority_anchor_retained": True,
    "resource_identity_ledger_retained": True,
    "install_and_rollback_receipts_retained": True,
}
_ENVELOPE_KEYS = {"schema_version", "payload", "signature"}
_PAYLOAD_KEYS = {
    "schema_version",
    "authorization_id",
    "authorization_namespace",
    "thread_id",
    "scope_id",
    "scope_sha256",
    "key_id",
    "approval_phrase",
    "nonce",
    "issued_at",
    "not_before",
    "expires_at",
    "single_use",
}
_SIGNATURE_KEYS = {"algorithm", "key_id", "value_base64"}
_TRUST_BUNDLE_KEYS = {"schema_version", "authorization_namespace", "keys"}
_TRUST_KEY_KEYS = {"key_id", "algorithm", "public_key_base64"}


class RollbackAuthorityError(RuntimeError):
    """Closed, content-free rollback authority refusal."""


@dataclass(frozen=True, slots=True)
class EmptyRollbackExpectedBindings:
    candidate_git_commit: str
    candidate_git_tree: str
    package_manifest_sha256: str
    controller_runtime_receipt_sha256: str
    installation_receipt_sha256: str
    rollback_plan_sha256: str
    exact_targets_sha256: str
    eligibility_receipt_sha256: str
    resource_ledger_head_sha256: str

    def __post_init__(self) -> None:
        if (
            _COMMIT_RE.fullmatch(self.candidate_git_commit) is None
            or _COMMIT_RE.fullmatch(self.candidate_git_tree) is None
            or any(
                _HASH_RE.fullmatch(value) is None
                for value in (
                    self.package_manifest_sha256,
                    self.controller_runtime_receipt_sha256,
                    self.installation_receipt_sha256,
                    self.rollback_plan_sha256,
                    self.exact_targets_sha256,
                    self.eligibility_receipt_sha256,
                    self.resource_ledger_head_sha256,
                )
            )
        ):
            raise RollbackAuthorityError("rollback_expected_bindings_invalid")


@dataclass(frozen=True, slots=True)
class EmptyRollbackAuthorityEvidence:
    operation: str
    authorization_namespace: str
    thread_id: str
    scope_id: str
    authorization_id: str
    key_id: str
    nonce: str
    scope_sha256: str
    authorization_sha256: str
    trust_bundle_sha256: str
    candidate_git_commit: str
    candidate_git_tree: str
    package_manifest_sha256: str
    controller_runtime_receipt_sha256: str
    controller_runtime_root: str
    controller_runtime_tree_sha256: str
    controller_release_root: str
    controller_release_tree_sha256: str
    controller_release_package_manifest_path: str
    controller_runtime_interpreter_path: str
    controller_runtime_interpreter_sha256: str
    controller_runtime_inventory_path: str
    controller_runtime_inventory_sha256: str
    controller_requirements_lock_sha256: str
    supervisor_launcher_path: str
    supervisor_launcher_sha256: str
    installation_receipt_sha256: str
    rollback_plan_sha256: str
    exact_targets_sha256: str
    eligibility_receipt_sha256: str
    resource_ledger_head_sha256: str
    not_before: str
    expires_at: str


_CAPABILITY_TOKEN = object()


class _VerifiedEmptyRollbackCapability:
    __slots__ = ("_evidence", "_token")

    def __init__(self, evidence: EmptyRollbackAuthorityEvidence, token: object) -> None:
        if token is not _CAPABILITY_TOKEN:
            raise RollbackAuthorityError("rollback_capability_invalid")
        self._evidence = evidence
        self._token = token

    def __repr__(self) -> str:
        return "VerifiedEmptyRollbackCapability(<content-redacted>)"


def rollback_capability_evidence(value: object) -> EmptyRollbackAuthorityEvidence:
    if (
        type(value) is not _VerifiedEmptyRollbackCapability
        or value._token is not _CAPABILITY_TOKEN
        or type(value._evidence) is not EmptyRollbackAuthorityEvidence
    ):
        raise RollbackAuthorityError("rollback_capability_invalid")
    return value._evidence


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
        raise RollbackAuthorityError("rollback_json_invalid") from error


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RollbackAuthorityError("rollback_json_duplicate_key")
        result[key] = value
    return result


def _parse_document(value: bytes | str, code: str) -> dict[str, object]:
    raw = value.encode("ascii") if isinstance(value, str) else value
    if type(raw) is not bytes or not 1 <= len(raw) <= MAX_DOCUMENT_BYTES:
        raise RollbackAuthorityError(code)
    try:
        document = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda unused: (_ for _ in ()).throw(
                RollbackAuthorityError(code)
            ),
        )
    except (UnicodeError, json.JSONDecodeError, RollbackAuthorityError) as error:
        raise RollbackAuthorityError(code) from error
    if type(document) is not dict or canonical_json_bytes(document) != raw:
        raise RollbackAuthorityError(code)
    return document


def _decode_base64(value: object, size: int, code: str) -> bytes:
    if not isinstance(value, str):
        raise RollbackAuthorityError(code)
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise RollbackAuthorityError(code) from error
    if len(decoded) != size or base64.b64encode(decoded).decode("ascii") != value:
        raise RollbackAuthorityError(code)
    return decoded


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        raise RollbackAuthorityError("rollback_authorization_time_invalid")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise RollbackAuthorityError(
            "rollback_authorization_time_invalid"
        ) from error


def _is_identifier(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER_RE.fullmatch(value) is not None


def _strict_structure_equal(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return set(left) == set(right) and all(  # type: ignore[arg-type]
            _strict_structure_equal(left[key], right[key])  # type: ignore[index]
            for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(  # type: ignore[arg-type]
            _strict_structure_equal(a, b) for a, b in zip(left, right)
        )
    return left == right


def verify_empty_rollback_execution_capability(
    scope_json: bytes | str,
    authorization_json: bytes | str,
    trust_bundle_json: bytes | str,
    *,
    expected_namespace: str,
    expected_thread_id: str,
    expected_scope_id: str,
    expected_key_id: str,
    expected_trust_bundle_sha256: str,
    expected_bindings: EmptyRollbackExpectedBindings,
    verified_package_capability: object,
    verified_controller_runtime_capability: object,
) -> object:
    try:
        package_evidence = verified_package_evidence(
            verified_package_capability
        )
        runtime_evidence = _controller_runtime_capability_evidence(
            verified_controller_runtime_capability
        )
    except (PackageCapabilityError, ControllerRuntimeCapabilityError) as error:
        raise RollbackAuthorityError(
            "rollback_verified_package_and_runtime_required"
        ) from error
    if (
        not all(
            _is_identifier(value)
            for value in (
                expected_namespace,
                expected_thread_id,
                expected_scope_id,
            )
        )
        or _HASH_RE.fullmatch(expected_key_id) is None
        or _HASH_RE.fullmatch(expected_trust_bundle_sha256) is None
        or type(expected_bindings) is not EmptyRollbackExpectedBindings
        or package_evidence.candidate_git_commit
        != expected_bindings.candidate_git_commit
        or package_evidence.candidate_git_tree
        != expected_bindings.candidate_git_tree
        or package_evidence.package_manifest_sha256
        != expected_bindings.package_manifest_sha256
        or package_evidence.package_manifest_sha256
        != runtime_evidence.package_manifest_sha256
        or package_evidence.controller_runtime_receipt_sha256
        != runtime_evidence.controller_runtime_receipt_sha256
        or runtime_evidence.controller_runtime_receipt_sha256
        != expected_bindings.controller_runtime_receipt_sha256
    ):
        raise RollbackAuthorityError("rollback_expectation_invalid")

    scope = _parse_document(scope_json, "rollback_scope_invalid")
    if (
        set(scope) != _SCOPE_KEYS
        or scope.get("schema_version") != SCOPE_SCHEMA_VERSION
        or scope.get("operation") != ROLLBACK_OPERATION
        or scope.get("authorization_namespace") != expected_namespace
        or scope.get("thread_id") != expected_thread_id
        or scope.get("scope_id") != expected_scope_id
        or not _strict_structure_equal(
            scope.get("empty_only_policy"), _EMPTY_ONLY_POLICY
        )
        or not _strict_structure_equal(
            scope.get("retention_policy"), _RETENTION_POLICY
        )
    ):
        raise RollbackAuthorityError("rollback_scope_invalid")
    for key, expected in (
        ("candidate_git_commit", package_evidence.candidate_git_commit),
        ("candidate_git_tree", package_evidence.candidate_git_tree),
        ("package_manifest_sha256", package_evidence.package_manifest_sha256),
        (
            "controller_runtime_receipt_sha256",
            runtime_evidence.controller_runtime_receipt_sha256,
        ),
        ("installation_receipt_sha256", expected_bindings.installation_receipt_sha256),
        ("rollback_plan_sha256", expected_bindings.rollback_plan_sha256),
        ("exact_targets_sha256", expected_bindings.exact_targets_sha256),
        ("eligibility_receipt_sha256", expected_bindings.eligibility_receipt_sha256),
        ("resource_ledger_head_sha256", expected_bindings.resource_ledger_head_sha256),
    ):
        if scope.get(key) != expected:
            raise RollbackAuthorityError("rollback_local_binding_mismatch")
    scope_sha256 = hashlib.sha256(canonical_json_bytes(scope)).hexdigest()

    trust = _parse_document(trust_bundle_json, "rollback_trust_bundle_invalid")
    trust_sha256 = hashlib.sha256(canonical_json_bytes(trust)).hexdigest()
    keys = trust.get("keys")
    if (
        set(trust) != _TRUST_BUNDLE_KEYS
        or trust.get("schema_version") != TRUST_BUNDLE_SCHEMA_VERSION
        or trust.get("authorization_namespace") != expected_namespace
        or trust_sha256 != expected_trust_bundle_sha256
        or type(keys) is not list
        or not keys
    ):
        raise RollbackAuthorityError("rollback_trust_bundle_invalid")
    public_key: Ed25519PublicKey | None = None
    observed_ids: list[str] = []
    for item in keys:
        if (
            type(item) is not dict
            or set(item) != _TRUST_KEY_KEYS
            or item.get("algorithm") != "Ed25519"
            or type(item.get("key_id")) is not str
            or _HASH_RE.fullmatch(item["key_id"]) is None
        ):
            raise RollbackAuthorityError("rollback_trust_bundle_invalid")
        key_id = str(item["key_id"])
        observed_ids.append(key_id)
        raw_key = _decode_base64(
            item.get("public_key_base64"), 32, "rollback_trust_bundle_invalid"
        )
        if key_id != hashlib.sha256(raw_key).hexdigest():
            raise RollbackAuthorityError("rollback_trust_bundle_key_id_invalid")
        if key_id == expected_key_id:
            public_key = Ed25519PublicKey.from_public_bytes(raw_key)
    if observed_ids != sorted(set(observed_ids)) or public_key is None:
        raise RollbackAuthorityError("rollback_trust_bundle_invalid")

    envelope = _parse_document(
        authorization_json, "rollback_authorization_invalid"
    )
    payload = envelope.get("payload")
    signature = envelope.get("signature")
    if (
        set(envelope) != _ENVELOPE_KEYS
        or envelope.get("schema_version") != AUTHORIZATION_SCHEMA_VERSION
        or type(payload) is not dict
        or type(signature) is not dict
        or set(payload) != _PAYLOAD_KEYS
        or set(signature) != _SIGNATURE_KEYS
    ):
        raise RollbackAuthorityError("rollback_authorization_invalid")
    assert isinstance(payload, dict)
    assert isinstance(signature, dict)
    if (
        payload.get("schema_version") != AUTHORIZATION_PAYLOAD_SCHEMA_VERSION
        or payload.get("authorization_namespace") != expected_namespace
        or payload.get("thread_id") != expected_thread_id
        or payload.get("scope_id") != expected_scope_id
        or payload.get("scope_sha256") != scope_sha256
        or payload.get("key_id") != expected_key_id
        or not _is_identifier(payload.get("authorization_id"))
        or payload.get("approval_phrase")
        != f"APPROVE GOVERNED MEMORY EMPTY STORE ROLLBACK {scope_sha256}"
        or payload.get("single_use") is not True
        or not isinstance(payload.get("nonce"), str)
        or _NONCE_RE.fullmatch(str(payload["nonce"])) is None
        or signature.get("algorithm") != "Ed25519"
        or signature.get("key_id") != expected_key_id
    ):
        raise RollbackAuthorityError("rollback_authorization_binding_invalid")
    issued_at = _timestamp(payload.get("issued_at"))
    not_before = _timestamp(payload.get("not_before"))
    expires_at = _timestamp(payload.get("expires_at"))
    if (
        not issued_at <= not_before < expires_at
        or (expires_at - issued_at).total_seconds()
        > MAX_AUTHORIZATION_LIFETIME_SECONDS
    ):
        raise RollbackAuthorityError("rollback_authorization_time_invalid")
    signature_bytes = _decode_base64(
        signature.get("value_base64"), 64, "rollback_authorization_signature_invalid"
    )
    try:
        public_key.verify(signature_bytes, canonical_json_bytes(payload))
    except InvalidSignature as error:
        raise RollbackAuthorityError(
            "rollback_authorization_signature_invalid"
        ) from error

    evidence = EmptyRollbackAuthorityEvidence(
        operation=ROLLBACK_OPERATION,
        authorization_namespace=expected_namespace,
        thread_id=expected_thread_id,
        scope_id=expected_scope_id,
        authorization_id=str(payload["authorization_id"]),
        key_id=expected_key_id,
        nonce=str(payload["nonce"]),
        scope_sha256=scope_sha256,
        authorization_sha256=hashlib.sha256(canonical_json_bytes(envelope)).hexdigest(),
        trust_bundle_sha256=trust_sha256,
        candidate_git_commit=package_evidence.candidate_git_commit,
        candidate_git_tree=package_evidence.candidate_git_tree,
        package_manifest_sha256=package_evidence.package_manifest_sha256,
        controller_runtime_receipt_sha256=(
            runtime_evidence.controller_runtime_receipt_sha256
        ),
        controller_runtime_root=runtime_evidence.runtime_root,
        controller_runtime_tree_sha256=runtime_evidence.runtime_tree_sha256,
        controller_release_root=runtime_evidence.release_root,
        controller_release_tree_sha256=runtime_evidence.release_tree_sha256,
        controller_release_package_manifest_path=(
            runtime_evidence.package_manifest_path
        ),
        controller_runtime_interpreter_path=runtime_evidence.interpreter_path,
        controller_runtime_interpreter_sha256=runtime_evidence.interpreter_sha256,
        controller_runtime_inventory_path=runtime_evidence.inventory_path,
        controller_runtime_inventory_sha256=(
            runtime_evidence.installed_distribution_inventory_sha256
        ),
        controller_requirements_lock_sha256=(
            runtime_evidence.controller_requirements_lock_sha256
        ),
        supervisor_launcher_path=runtime_evidence.supervisor_launcher_path,
        supervisor_launcher_sha256=runtime_evidence.supervisor_launcher_sha256,
        installation_receipt_sha256=expected_bindings.installation_receipt_sha256,
        rollback_plan_sha256=expected_bindings.rollback_plan_sha256,
        exact_targets_sha256=expected_bindings.exact_targets_sha256,
        eligibility_receipt_sha256=expected_bindings.eligibility_receipt_sha256,
        resource_ledger_head_sha256=expected_bindings.resource_ledger_head_sha256,
        not_before=str(payload["not_before"]),
        expires_at=str(payload["expires_at"]),
    )
    return _VerifiedEmptyRollbackCapability(evidence, _CAPABILITY_TOKEN)


__all__ = [
    "AUTHORIZATION_PAYLOAD_SCHEMA_VERSION",
    "AUTHORIZATION_SCHEMA_VERSION",
    "EmptyRollbackAuthorityEvidence",
    "EmptyRollbackExpectedBindings",
    "ROLLBACK_OPERATION",
    "RollbackAuthorityError",
    "SCOPE_SCHEMA_VERSION",
    "TRUST_BUNDLE_SCHEMA_VERSION",
    "canonical_json_bytes",
    "rollback_capability_evidence",
    "verify_empty_rollback_execution_capability",
]
