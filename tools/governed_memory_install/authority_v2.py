#!/usr/bin/env python3
from __future__ import annotations

"""Offline verification for a narrowly scoped dormant-install authorization.

The verifier accepts only canonical ASCII JSON supplied by the caller.  It
does not read or write files, invoke commands, contact a network service, use
the wall clock, consume a nonce, or execute an installation.  A successful
result proves only that an externally trusted Ed25519 key signed the exact
Phase 8B scope and that the caller reports its nonce as unused.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import binascii
import hashlib
import json
import re
from typing import Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


SCOPE_SCHEMA_VERSION: Final = "governed-memory-dormant-install-scope-v1"
AUTHORIZATION_SCHEMA_VERSION: Final = (
    "governed-memory-external-authorization-envelope-v1"
)
AUTHORIZATION_PAYLOAD_SCHEMA_VERSION: Final = (
    "governed-memory-dormant-install-authorization-v1"
)
TRUST_BUNDLE_SCHEMA_VERSION: Final = (
    "governed-memory-ed25519-public-key-trust-bundle-v1"
)
AUTHORIZATION_OPERATION: Final = "dormant_install"
AUTHORIZATION_PHASE: Final = "8B"
RESULT_TYPE: Final = "cryptographically_valid_scope_not_execution"
MAX_AUTHORIZATION_LIFETIME_SECONDS: Final = 15 * 60
MAX_DOCUMENT_BYTES: Final = 64 * 1024

ALLOWED_SECRET_NAMES: Final = (
    "GOVERNED_MEMORY_BOOTSTRAP_PASSWORD",
    "GOVERNED_MEMORY_QDRANT_API_KEY",
)
FORBIDDEN_SECRET_CLASSES: Final = (
    "pilot",
    "provider",
    "runtime",
    "service",
    "supabase",
)

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
    "phase",
    "operation",
    "authorization_namespace",
    "thread_id",
    "scope_id",
    "candidate_git_commit",
    "candidate_git_tree",
    "package_manifest_sha256",
    "controller_contract_sha256",
    "execution_plan_sha256",
    "exact_targets_sha256",
    "source_boundary",
    "store_policy",
    "secret_policy",
}
_SOURCE_BOUNDARY = {
    "source_postgres_connection_count": 0,
    "source_postgres_read_count": 0,
    "source_postgres_write_count": 0,
    "source_preparation_phase": "8C_separate_authorization_required",
}
_STORE_POLICY = {
    "postgresql": "fresh_isolated_empty",
    "qdrant": "fresh_isolated_empty",
    "legacy_imports_allowed": False,
    "snapshot_restore_allowed": False,
    "unprocessed_prefill_allowed": False,
    "initial_postgresql_user_row_count": 0,
    "initial_qdrant_point_count": 0,
}
_SECRET_POLICY = {
    "allowed_secret_names": list(ALLOWED_SECRET_NAMES),
    "forbidden_secret_classes": list(FORBIDDEN_SECRET_CLASSES),
    "runtime_secret_count": 0,
    "provider_secret_count": 0,
    "supabase_secret_count": 0,
    "service_secret_count": 0,
    "pilot_secret_count": 0,
    "secret_values_present": False,
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
_TRUST_BUNDLE_KEYS = {
    "schema_version",
    "authorization_namespace",
    "keys",
}
_TRUST_KEY_KEYS = {"key_id", "algorithm", "public_key_base64"}


class AuthorityVerificationError(RuntimeError):
    """Closed, content-free refusal raised by the authority verifier."""


class _DuplicateJsonKey(ValueError):
    pass


class _NonFiniteJsonValue(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CryptographicallyValidScopeNotExecution:
    """Evidence of signature validity; deliberately not an execution token."""

    result_type: str
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
    not_before: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class DormantInstallExpectedBindings:
    """Locally observed immutable bindings required by the signed scope."""

    candidate_git_commit: str
    candidate_git_tree: str
    package_manifest_sha256: str
    controller_contract_sha256: str
    execution_plan_sha256: str
    exact_targets_sha256: str

    def __post_init__(self) -> None:
        if (
            not _is_commit(self.candidate_git_commit)
            or not _is_commit(self.candidate_git_tree)
            or not all(
                _is_hash(value)
                for value in (
                    self.package_manifest_sha256,
                    self.controller_contract_sha256,
                    self.execution_plan_sha256,
                    self.exact_targets_sha256,
                )
            )
        ):
            raise AuthorityVerificationError("authority_expected_bindings_invalid")


_EXECUTION_CAPABILITY_TOKEN = object()


class _VerifiedDormantInstallCapability:
    """Opaque signature-verifier output accepted by execution authority.

    The older public evidence dataclass remains intentionally descriptive and
    structurally constructible.  It is never accepted at the execution
    boundary.  Only this module can mint this private-token-bearing object.
    """

    __slots__ = ("_evidence", "_token")

    def __init__(
        self,
        evidence: CryptographicallyValidScopeNotExecution,
        token: object,
    ) -> None:
        if token is not _EXECUTION_CAPABILITY_TOKEN:
            raise AuthorityVerificationError("execution_capability_invalid")
        self._evidence = evidence
        self._token = token

    def __repr__(self) -> str:
        return "VerifiedDormantInstallCapability(<content-redacted>)"


def _execution_capability_evidence(
    value: object,
) -> CryptographicallyValidScopeNotExecution:
    if (
        type(value) is not _VerifiedDormantInstallCapability
        or value._token is not _EXECUTION_CAPABILITY_TOKEN
        or type(value._evidence) is not CryptographicallyValidScopeNotExecution
    ):
        raise AuthorityVerificationError("execution_capability_invalid")
    return value._evidence


def canonical_json_bytes(value: object) -> bytes:
    """Return the sole accepted ASCII, sorted-key, compact JSON encoding."""

    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as error:
        raise AuthorityVerificationError("authority_json_invalid") from error


def canonical_json_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise _NonFiniteJsonValue(value)


def _parse_canonical_document(
    raw: bytes | str,
    *,
    invalid_code: str,
) -> dict[str, object]:
    if isinstance(raw, str):
        try:
            encoded = raw.encode("ascii")
        except UnicodeError as error:
            raise AuthorityVerificationError(invalid_code) from error
    elif isinstance(raw, bytes):
        encoded = raw
    else:
        raise AuthorityVerificationError(invalid_code)
    if not encoded or len(encoded) > MAX_DOCUMENT_BYTES:
        raise AuthorityVerificationError(invalid_code)
    try:
        text = encoded.decode("ascii")
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
    except _DuplicateJsonKey as error:
        raise AuthorityVerificationError("authority_json_duplicate_key") from error
    except (
        UnicodeError,
        json.JSONDecodeError,
        _NonFiniteJsonValue,
    ) as error:
        raise AuthorityVerificationError(invalid_code) from error
    if not isinstance(value, dict) or canonical_json_bytes(value) != encoded:
        raise AuthorityVerificationError("authority_json_not_canonical")
    return value


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise AuthorityVerificationError(code)


def _strict_structure_equal(actual: object, expected: object) -> bool:
    """Compare JSON values without Python's bool/int numeric coercion."""

    if isinstance(expected, dict):
        return (
            type(actual) is dict
            and set(actual) == set(expected)
            and all(
                _strict_structure_equal(actual[key], value)
                for key, value in expected.items()
            )
        )
    if isinstance(expected, list):
        return (
            type(actual) is list
            and len(actual) == len(expected)
            and all(
                _strict_structure_equal(left, right)
                for left, right in zip(actual, expected, strict=True)
            )
        )
    return type(actual) is type(expected) and actual == expected


def _is_identifier(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER_RE.fullmatch(value) is not None


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and _HASH_RE.fullmatch(value) is not None


def _is_commit(value: object) -> bool:
    return isinstance(value, str) and _COMMIT_RE.fullmatch(value) is not None


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        raise AuthorityVerificationError("authorization_validity_invalid")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise AuthorityVerificationError("authorization_validity_invalid") from error


def _decode_canonical_base64(
    value: object,
    *,
    expected_bytes: int,
    invalid_code: str,
) -> bytes:
    if not isinstance(value, str) or not value:
        raise AuthorityVerificationError(invalid_code)
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeError, binascii.Error, ValueError) as error:
        raise AuthorityVerificationError(invalid_code) from error
    if (
        len(decoded) != expected_bytes
        or base64.b64encode(decoded).decode("ascii") != value
    ):
        raise AuthorityVerificationError(invalid_code)
    return decoded


def _verify_scope(
    scope: Mapping[str, object],
    *,
    expected_namespace: str,
    expected_thread_id: str,
    expected_scope_id: str,
) -> None:
    _require(set(scope) == _SCOPE_KEYS, "dormant_install_scope_invalid")
    _require(
        scope.get("schema_version") == SCOPE_SCHEMA_VERSION
        and scope.get("phase") == AUTHORIZATION_PHASE
        and scope.get("operation") == AUTHORIZATION_OPERATION,
        "dormant_install_scope_invalid",
    )
    _require(
        scope.get("authorization_namespace") == expected_namespace
        and scope.get("thread_id") == expected_thread_id
        and scope.get("scope_id") == expected_scope_id,
        "dormant_install_scope_identity_mismatch",
    )
    _require(
        all(
            _is_identifier(value)
            for value in (
                expected_namespace,
                expected_thread_id,
                expected_scope_id,
            )
        ),
        "dormant_install_scope_identity_mismatch",
    )
    _require(
        _is_commit(scope.get("candidate_git_commit"))
        and _is_commit(scope.get("candidate_git_tree"))
        and all(
            _is_hash(scope.get(key))
            for key in (
                "package_manifest_sha256",
                "controller_contract_sha256",
                "execution_plan_sha256",
                "exact_targets_sha256",
            )
        ),
        "dormant_install_scope_binding_invalid",
    )
    _require(
        _strict_structure_equal(
            scope.get("source_boundary"), _SOURCE_BOUNDARY
        ),
        "phase8b_source_boundary_invalid",
    )
    _require(
        _strict_structure_equal(scope.get("store_policy"), _STORE_POLICY),
        "phase8b_fresh_store_policy_invalid",
    )
    _require(
        _strict_structure_equal(scope.get("secret_policy"), _SECRET_POLICY),
        "phase8b_secret_policy_invalid",
    )


def _public_key_from_trust_bundle(
    bundle: Mapping[str, object],
    *,
    expected_namespace: str,
    expected_key_id: str,
) -> Ed25519PublicKey:
    _require(set(bundle) == _TRUST_BUNDLE_KEYS, "trust_bundle_invalid")
    keys = bundle.get("keys")
    _require(
        bundle.get("schema_version") == TRUST_BUNDLE_SCHEMA_VERSION
        and bundle.get("authorization_namespace") == expected_namespace
        and isinstance(keys, list)
        and bool(keys),
        "trust_bundle_invalid",
    )
    observed_ids: list[str] = []
    selected: bytes | None = None
    for entry in keys:
        _require(
            isinstance(entry, dict) and set(entry) == _TRUST_KEY_KEYS,
            "trust_bundle_invalid",
        )
        key_id = entry.get("key_id")
        _require(
            _is_hash(key_id) and entry.get("algorithm") == "Ed25519",
            "trust_bundle_invalid",
        )
        assert isinstance(key_id, str)
        public_bytes = _decode_canonical_base64(
            entry.get("public_key_base64"),
            expected_bytes=32,
            invalid_code="trust_bundle_invalid",
        )
        _require(
            key_id == hashlib.sha256(public_bytes).hexdigest(),
            "trust_bundle_key_id_invalid",
        )
        observed_ids.append(key_id)
        if key_id == expected_key_id:
            selected = public_bytes
    _require(
        observed_ids == sorted(set(observed_ids)),
        "trust_bundle_invalid",
    )
    _require(selected is not None, "authorization_key_id_mismatch")
    assert selected is not None
    return Ed25519PublicKey.from_public_bytes(selected)


def _verify_dormant_install_signature_evidence(
    scope_json: bytes | str,
    authorization_json: bytes | str,
    trust_bundle_json: bytes | str,
    *,
    expected_namespace: str,
    expected_thread_id: str,
    expected_scope_id: str,
    expected_key_id: str,
    expected_trust_bundle_sha256: str,
    expected_bindings: DormantInstallExpectedBindings | None = None,
) -> CryptographicallyValidScopeNotExecution:
    """Verify exact signed bytes without deciding current nonce authority."""

    _require(
        _is_hash(expected_key_id)
        and _is_hash(expected_trust_bundle_sha256),
        "authority_expectation_invalid",
    )
    scope = _parse_canonical_document(
        scope_json, invalid_code="dormant_install_scope_invalid"
    )
    _verify_scope(
        scope,
        expected_namespace=expected_namespace,
        expected_thread_id=expected_thread_id,
        expected_scope_id=expected_scope_id,
    )
    if expected_bindings is not None:
        _verify_expected_scope_bindings(scope, expected_bindings)
    scope_bytes = canonical_json_bytes(scope)
    scope_sha256 = hashlib.sha256(scope_bytes).hexdigest()

    trust_bundle = _parse_canonical_document(
        trust_bundle_json, invalid_code="trust_bundle_invalid"
    )
    trust_bundle_sha256 = canonical_json_sha256(trust_bundle)
    _require(
        trust_bundle_sha256 == expected_trust_bundle_sha256,
        "trust_bundle_hash_mismatch",
    )
    public_key = _public_key_from_trust_bundle(
        trust_bundle,
        expected_namespace=expected_namespace,
        expected_key_id=expected_key_id,
    )

    envelope = _parse_canonical_document(
        authorization_json, invalid_code="authorization_envelope_invalid"
    )
    payload = envelope.get("payload")
    signature = envelope.get("signature")
    _require(
        set(envelope) == _ENVELOPE_KEYS
        and envelope.get("schema_version") == AUTHORIZATION_SCHEMA_VERSION
        and isinstance(payload, dict)
        and isinstance(signature, dict)
        and set(payload) == _PAYLOAD_KEYS
        and set(signature) == _SIGNATURE_KEYS,
        "authorization_envelope_invalid",
    )
    assert isinstance(payload, dict)
    assert isinstance(signature, dict)
    _require(
        payload.get("schema_version") == AUTHORIZATION_PAYLOAD_SCHEMA_VERSION
        and _is_identifier(payload.get("authorization_id"))
        and payload.get("authorization_namespace") == expected_namespace
        and payload.get("thread_id") == expected_thread_id
        and payload.get("scope_id") == expected_scope_id
        and payload.get("scope_sha256") == scope_sha256
        and payload.get("key_id") == expected_key_id,
        "authorization_identity_or_binding_mismatch",
    )
    _require(
        signature.get("algorithm") == "Ed25519"
        and signature.get("key_id") == expected_key_id
        and signature.get("key_id") == payload.get("key_id"),
        "authorization_key_id_mismatch",
    )
    expected_phrase = f"APPROVE PHASE 8B DORMANT INSTALL {scope_sha256}"
    _require(
        payload.get("approval_phrase") == expected_phrase,
        "authorization_approval_phrase_invalid",
    )
    _require(
        payload.get("single_use") is True,
        "authorization_single_use_required",
    )
    nonce = payload.get("nonce")
    _require(
        isinstance(nonce, str) and _NONCE_RE.fullmatch(nonce) is not None,
        "authorization_nonce_invalid",
    )

    issued_at = _parse_timestamp(payload.get("issued_at"))
    not_before = _parse_timestamp(payload.get("not_before"))
    expires_at = _parse_timestamp(payload.get("expires_at"))
    _require(
        issued_at <= not_before < expires_at
        and (expires_at - issued_at).total_seconds()
        <= MAX_AUTHORIZATION_LIFETIME_SECONDS,
        "authorization_validity_invalid",
    )
    signature_bytes = _decode_canonical_base64(
        signature.get("value_base64"),
        expected_bytes=64,
        invalid_code="authorization_signature_invalid",
    )
    try:
        public_key.verify(signature_bytes, canonical_json_bytes(payload))
    except InvalidSignature as error:
        raise AuthorityVerificationError("authorization_signature_invalid") from error

    authorization_sha256 = canonical_json_sha256(envelope)
    return CryptographicallyValidScopeNotExecution(
        result_type=RESULT_TYPE,
        operation=AUTHORIZATION_OPERATION,
        authorization_namespace=expected_namespace,
        thread_id=expected_thread_id,
        scope_id=expected_scope_id,
        authorization_id=str(payload["authorization_id"]),
        key_id=expected_key_id,
        nonce=nonce,
        scope_sha256=scope_sha256,
        authorization_sha256=authorization_sha256,
        trust_bundle_sha256=trust_bundle_sha256,
        not_before=str(payload["not_before"]),
        expires_at=str(payload["expires_at"]),
    )


def verify_dormant_install_authority(
    scope_json: bytes | str,
    authorization_json: bytes | str,
    trust_bundle_json: bytes | str,
    *,
    expected_namespace: str,
    expected_thread_id: str,
    expected_scope_id: str,
    expected_key_id: str,
    expected_trust_bundle_sha256: str,
    now: datetime,
    nonce_used: Callable[[str], bool],
) -> CryptographicallyValidScopeNotExecution:
    """Return descriptive current/unused signature evidence, never a permit."""

    _require(
        isinstance(now, datetime)
        and now.tzinfo is not None
        and now.utcoffset() == timezone.utc.utcoffset(now),
        "authority_clock_invalid",
    )
    _require(callable(nonce_used), "authority_nonce_callback_invalid")
    evidence = _verify_dormant_install_signature_evidence(
        scope_json,
        authorization_json,
        trust_bundle_json,
        expected_namespace=expected_namespace,
        expected_thread_id=expected_thread_id,
        expected_scope_id=expected_scope_id,
        expected_key_id=expected_key_id,
        expected_trust_bundle_sha256=expected_trust_bundle_sha256,
    )
    not_before = _parse_timestamp(evidence.not_before)
    expires_at = _parse_timestamp(evidence.expires_at)
    _require(not_before <= now < expires_at, "authorization_not_current")
    try:
        already_used = nonce_used(evidence.nonce)
    except Exception as error:
        raise AuthorityVerificationError(
            "authorization_nonce_state_unavailable"
        ) from error
    _require(isinstance(already_used, bool), "authorization_nonce_state_invalid")
    _require(not already_used, "authorization_nonce_replayed")
    return evidence


def _verify_expected_scope_bindings(
    scope: Mapping[str, object],
    expected: DormantInstallExpectedBindings,
) -> None:
    if type(expected) is not DormantInstallExpectedBindings:
        raise AuthorityVerificationError("authority_expected_bindings_invalid")
    exact = {
        "candidate_git_commit": expected.candidate_git_commit,
        "candidate_git_tree": expected.candidate_git_tree,
        "package_manifest_sha256": expected.package_manifest_sha256,
        "controller_contract_sha256": expected.controller_contract_sha256,
        "execution_plan_sha256": expected.execution_plan_sha256,
        "exact_targets_sha256": expected.exact_targets_sha256,
    }
    if any(scope.get(key) != value for key, value in exact.items()):
        raise AuthorityVerificationError("authority_local_binding_mismatch")


def verify_dormant_install_execution_capability(
    scope_json: bytes | str,
    authorization_json: bytes | str,
    trust_bundle_json: bytes | str,
    *,
    expected_namespace: str,
    expected_thread_id: str,
    expected_scope_id: str,
    expected_key_id: str,
    expected_trust_bundle_sha256: str,
    expected_bindings: DormantInstallExpectedBindings,
) -> object:
    """Verify exact signed bytes and local bindings without using live time.

    This is the only verifier that mints the opaque input accepted by
    ``claim_execution_authority``.  It intentionally does not decide whether a
    nonce is new, used, current, or resumable.  Those decisions require the
    held global lock, one trusted clock reading, and the atomic durable nonce
    ledger, and therefore belong exclusively to the execution-authority layer.

    Re-verifying the same signed bytes after expiry is safe: a new claim will
    still fail, while an exact already-claimed execution can recover.
    """

    evidence = _verify_dormant_install_signature_evidence(
        scope_json,
        authorization_json,
        trust_bundle_json,
        expected_namespace=expected_namespace,
        expected_thread_id=expected_thread_id,
        expected_scope_id=expected_scope_id,
        expected_key_id=expected_key_id,
        expected_trust_bundle_sha256=expected_trust_bundle_sha256,
        expected_bindings=expected_bindings,
    )
    return _VerifiedDormantInstallCapability(
        evidence,
        _EXECUTION_CAPABILITY_TOKEN,
    )


__all__ = [
    "ALLOWED_SECRET_NAMES",
    "AUTHORIZATION_OPERATION",
    "AUTHORIZATION_PHASE",
    "AuthorityVerificationError",
    "CryptographicallyValidScopeNotExecution",
    "DormantInstallExpectedBindings",
    "FORBIDDEN_SECRET_CLASSES",
    "MAX_AUTHORIZATION_LIFETIME_SECONDS",
    "RESULT_TYPE",
    "canonical_json_bytes",
    "canonical_json_sha256",
    "verify_dormant_install_authority",
    "verify_dormant_install_execution_capability",
]
