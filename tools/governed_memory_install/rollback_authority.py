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
from types import MappingProxyType
from typing import Final, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .authority_state import nonce_sha256
from .execution_authority import execution_binding_sha256
from .execution_capability import (
    ClaimedExecutionBindingError,
    verified_dormant_install_authority_identity,
)
from .package_capability import (
    PackageCapabilityError,
    verified_package_evidence,
)
from .controller_runtime import (
    ControllerRuntimeCapabilityError,
    _controller_runtime_capability_evidence,
)


SCOPE_SCHEMA_VERSION: Final = "governed-memory-empty-store-rollback-scope-v3"
AUTHORIZATION_SCHEMA_VERSION: Final = (
    "governed-memory-external-authorization-envelope-v1"
)
AUTHORIZATION_PAYLOAD_SCHEMA_VERSION: Final = (
    "governed-memory-empty-store-rollback-authorization-v3"
)
TRUST_BUNDLE_SCHEMA_VERSION: Final = (
    "governed-memory-ed25519-public-key-trust-bundle-v1"
)
ROLLBACK_OPERATION: Final = "empty_store_rollback"
RECOVERY_RESERVATION_OPERATION: Final = (
    "empty_store_rollback_recovery_reservation"
)
RECOVERY_DELEGATION_SCHEMA_VERSION: Final = (
    "governed-memory-empty-store-recovery-delegation-envelope-v1"
)
RECOVERY_DELEGATION_PAYLOAD_SCHEMA_VERSION: Final = (
    "governed-memory-empty-store-recovery-delegation-v1"
)
RECOVERY_RESERVATION_SCOPE_SCHEMA_VERSION: Final = (
    "governed-memory-empty-store-recovery-reservation-scope-v1"
)
MAX_DOCUMENT_BYTES: Final = 64 * 1024
MAX_AUTHORIZATION_LIFETIME_SECONDS: Final = 15 * 60

RECOVERY_DERIVATION_POLICY: Final = MappingProxyType(
    {
        "installation_evidence": (
            "verified_durable_install_receipt_and_anchored_resource_ledger"
        ),
        "rollback_scope": "deterministic_exact_empty_only_v1",
        "post_expiry_new_claim": (
            "preclaimed_exact_recovery_reservation_only"
        ),
        "private_key_required_after_reservation": False,
    }
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
    "operation",
    "authorization_namespace",
    "thread_id",
    "scope_id",
    "candidate_git_commit",
    "candidate_git_tree",
    "package_manifest_sha256",
    "controller_runtime_receipt_sha256",
    "installation_execution_id",
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
_RECOVERY_DELEGATION_PAYLOAD_KEYS = {
    "schema_version",
    "authorization_namespace",
    "thread_id",
    "install_scope_id",
    "rollback_scope_id",
    "authorization_text_sha256",
    "candidate_git_commit",
    "candidate_git_tree",
    "package_manifest_sha256",
    "controller_runtime_receipt_sha256",
    "controller_contract_sha256",
    "execution_plan_sha256",
    "exact_target_contract_sha256",
    "installation_execution_id",
    "rollback_nonce",
    "recovery_reservation_nonce",
    "key_id",
    "issued_at",
    "not_before",
    "expires_at",
    "single_use",
    "empty_only",
    "derivation_policy",
    "source_postgres_read_count",
    "production_data_read",
    "provider_calls",
    "activation_allowed",
}
_RECOVERY_RESERVATION_SCOPE_DOMAIN: Final = (
    b"governed-memory-empty-store-recovery-reservation-scope-v1\x00"
)


class RollbackAuthorityError(RuntimeError):
    """Closed, content-free rollback authority refusal."""


@dataclass(frozen=True, slots=True)
class EmptyRollbackExpectedBindings:
    candidate_git_commit: str
    candidate_git_tree: str
    package_manifest_sha256: str
    controller_runtime_receipt_sha256: str
    installation_execution_id: str
    installation_nonce_sha256: str
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
                    self.installation_execution_id,
                    self.installation_nonce_sha256,
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
class EmptyRollbackRecoveryReservationBinding:
    nonce: str
    operation: str
    execution_sha256: str
    authorization_sha256: str
    scope_sha256: str
    trust_bundle_sha256: str
    installation_execution_id: str
    not_before: str
    expires_at: str

    def __post_init__(self) -> None:
        if (
            self.operation != RECOVERY_RESERVATION_OPERATION
            or _NONCE_RE.fullmatch(self.nonce) is None
            or any(
                _HASH_RE.fullmatch(value) is None
                for value in (
                    self.execution_sha256,
                    self.authorization_sha256,
                    self.scope_sha256,
                    self.trust_bundle_sha256,
                    self.installation_execution_id,
                )
            )
        ):
            raise RollbackAuthorityError(
                "rollback_recovery_reservation_binding_invalid"
            )
        not_before = _timestamp(self.not_before)
        expires_at = _timestamp(self.expires_at)
        if not not_before < expires_at:
            raise RollbackAuthorityError(
                "rollback_recovery_reservation_binding_invalid"
            )


@dataclass(frozen=True, slots=True)
class EmptyRollbackRecoveryDelegationEvidence:
    authorization_namespace: str
    thread_id: str
    install_scope_id: str
    rollback_scope_id: str
    authorization_text_sha256: str
    key_id: str
    rollback_nonce: str
    delegation_sha256: str
    trust_bundle_sha256: str
    candidate_git_commit: str
    candidate_git_tree: str
    package_manifest_sha256: str
    controller_contract_sha256: str
    execution_plan_sha256: str
    exact_target_contract_sha256: str
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
    installation_execution_id: str
    installation_nonce_sha256: str
    issued_at: str
    not_before: str
    expires_at: str
    recovery_reservation: EmptyRollbackRecoveryReservationBinding


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
    installation_execution_id: str
    installation_nonce_sha256: str
    installation_receipt_sha256: str
    rollback_plan_sha256: str
    exact_targets_sha256: str
    eligibility_receipt_sha256: str
    resource_ledger_head_sha256: str
    not_before: str
    expires_at: str
    recovery_reservation: EmptyRollbackRecoveryReservationBinding | None = None


_CAPABILITY_TOKEN = object()
_RECOVERY_DELEGATION_TOKEN = object()


class _VerifiedEmptyRollbackCapability:
    __slots__ = ("_evidence", "_token")

    def __init__(self, evidence: EmptyRollbackAuthorityEvidence, token: object) -> None:
        if token is not _CAPABILITY_TOKEN:
            raise RollbackAuthorityError("rollback_capability_invalid")
        self._evidence = evidence
        self._token = token

    def __repr__(self) -> str:
        return "VerifiedEmptyRollbackCapability(<content-redacted>)"


class _VerifiedEmptyRollbackRecoveryDelegation:
    __slots__ = ("_evidence", "_token")

    def __init__(
        self,
        evidence: EmptyRollbackRecoveryDelegationEvidence,
        token: object,
    ) -> None:
        if token is not _RECOVERY_DELEGATION_TOKEN:
            raise RollbackAuthorityError(
                "rollback_recovery_delegation_capability_invalid"
            )
        self._evidence = evidence
        self._token = token

    def __repr__(self) -> str:
        return "VerifiedEmptyRollbackRecoveryDelegation(<content-redacted>)"


def rollback_capability_evidence(value: object) -> EmptyRollbackAuthorityEvidence:
    if (
        type(value) is not _VerifiedEmptyRollbackCapability
        or value._token is not _CAPABILITY_TOKEN
        or type(value._evidence) is not EmptyRollbackAuthorityEvidence
    ):
        raise RollbackAuthorityError("rollback_capability_invalid")
    return value._evidence


def recovery_delegation_evidence(
    value: object,
) -> EmptyRollbackRecoveryDelegationEvidence:
    if (
        type(value) is not _VerifiedEmptyRollbackRecoveryDelegation
        or value._token is not _RECOVERY_DELEGATION_TOKEN
        or type(value._evidence)
        is not EmptyRollbackRecoveryDelegationEvidence
    ):
        raise RollbackAuthorityError(
            "rollback_recovery_delegation_capability_invalid"
        )
    return value._evidence


def recovery_reservation_binding(
    value: object,
) -> EmptyRollbackRecoveryReservationBinding:
    return recovery_delegation_evidence(value).recovery_reservation


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


def _verified_trust_public_key(
    trust_bundle_json: bytes | str,
    *,
    expected_namespace: str,
    expected_key_id: str,
    expected_trust_bundle_sha256: str,
) -> tuple[Ed25519PublicKey, str]:
    trust = _parse_document(
        trust_bundle_json, "rollback_recovery_trust_bundle_invalid"
    )
    trust_raw = canonical_json_bytes(trust)
    trust_sha256 = hashlib.sha256(trust_raw).hexdigest()
    keys = trust.get("keys")
    if (
        set(trust) != _TRUST_BUNDLE_KEYS
        or trust.get("schema_version") != TRUST_BUNDLE_SCHEMA_VERSION
        or trust.get("authorization_namespace") != expected_namespace
        or trust_sha256 != expected_trust_bundle_sha256
        or type(keys) is not list
        or not keys
    ):
        raise RollbackAuthorityError(
            "rollback_recovery_trust_bundle_invalid"
        )
    selected: Ed25519PublicKey | None = None
    observed_ids: list[str] = []
    for item in keys:
        if (
            type(item) is not dict
            or set(item) != _TRUST_KEY_KEYS
            or item.get("algorithm") != "Ed25519"
            or type(item.get("key_id")) is not str
            or _HASH_RE.fullmatch(str(item["key_id"])) is None
        ):
            raise RollbackAuthorityError(
                "rollback_recovery_trust_bundle_invalid"
            )
        key_id = str(item["key_id"])
        observed_ids.append(key_id)
        raw_key = _decode_base64(
            item.get("public_key_base64"),
            32,
            "rollback_recovery_trust_bundle_invalid",
        )
        if key_id != hashlib.sha256(raw_key).hexdigest():
            raise RollbackAuthorityError(
                "rollback_recovery_trust_bundle_key_id_invalid"
            )
        if key_id == expected_key_id:
            selected = Ed25519PublicKey.from_public_bytes(raw_key)
    if observed_ids != sorted(set(observed_ids)) or selected is None:
        raise RollbackAuthorityError(
            "rollback_recovery_trust_bundle_invalid"
        )
    return selected, trust_sha256


def _reservation_scope_sha256(
    payload: Mapping[str, object], delegation_sha256: str
) -> str:
    material = {
        "schema_version": RECOVERY_RESERVATION_SCOPE_SCHEMA_VERSION,
        "operation": RECOVERY_RESERVATION_OPERATION,
        "authorization_namespace": payload["authorization_namespace"],
        "thread_id": payload["thread_id"],
        "install_scope_id": payload["install_scope_id"],
        "rollback_scope_id": payload["rollback_scope_id"],
        "candidate_git_commit": payload["candidate_git_commit"],
        "candidate_git_tree": payload["candidate_git_tree"],
        "package_manifest_sha256": payload["package_manifest_sha256"],
        "controller_runtime_receipt_sha256": payload[
            "controller_runtime_receipt_sha256"
        ],
        "controller_contract_sha256": payload[
            "controller_contract_sha256"
        ],
        "execution_plan_sha256": payload["execution_plan_sha256"],
        "exact_target_contract_sha256": payload[
            "exact_target_contract_sha256"
        ],
        "installation_execution_id": payload["installation_execution_id"],
        "delegation_sha256": delegation_sha256,
        "derivation_policy": dict(RECOVERY_DERIVATION_POLICY),
    }
    return hashlib.sha256(
        _RECOVERY_RESERVATION_SCOPE_DOMAIN + canonical_json_bytes(material)
    ).hexdigest()


def _reservation_execution_sha256(
    *,
    scope_sha256: str,
    delegation_sha256: str,
    trust_bundle_sha256: str,
) -> str:
    return execution_binding_sha256(
        operation=RECOVERY_RESERVATION_OPERATION,
        scope_sha256=scope_sha256,
        authorization_sha256=delegation_sha256,
        trust_bundle_sha256=trust_bundle_sha256,
    )


def verify_empty_rollback_recovery_delegation(
    delegation_json: bytes | str,
    trust_bundle_json: bytes | str,
    *,
    expected_namespace: str,
    expected_thread_id: str,
    expected_install_scope_id: str,
    expected_rollback_scope_id: str,
    expected_authorization_text_sha256: str,
    expected_key_id: str,
    expected_trust_bundle_sha256: str,
    expected_installation_execution_id: str,
    verified_install_scope_capability: object,
    verified_package_capability: object,
    verified_controller_runtime_capability: object,
) -> object:
    """Verify a signed public delegation before any installation effect.

    Time is checked only for a structurally bounded signed window.  The caller
    must use trusted time when durably claiming the returned reservation.
    """

    try:
        package = verified_package_evidence(verified_package_capability)
        runtime = _controller_runtime_capability_evidence(
            verified_controller_runtime_capability
        )
        install_identity = verified_dormant_install_authority_identity(
            verified_install_scope_capability
        )
    except (
        PackageCapabilityError,
        ControllerRuntimeCapabilityError,
        ClaimedExecutionBindingError,
    ) as error:
        raise RollbackAuthorityError(
            "rollback_recovery_verified_package_and_runtime_required"
        ) from error
    expected_identifiers = (
        expected_namespace,
        expected_thread_id,
        expected_install_scope_id,
        expected_rollback_scope_id,
    )
    if (
        not all(_is_identifier(value) for value in expected_identifiers)
        or any(
            _HASH_RE.fullmatch(value) is None
            for value in (
                expected_authorization_text_sha256,
                expected_key_id,
                expected_trust_bundle_sha256,
                expected_installation_execution_id,
            )
        )
        or install_identity.execution_id
        != expected_installation_execution_id
        or install_identity.thread_id != expected_thread_id
        or install_identity.scope_id != expected_install_scope_id
        or install_identity.authorization_sha256
        != package.authorization_sha256
        or install_identity.scope_sha256 != package.scope_sha256
        or package.package_manifest_sha256
        != runtime.package_manifest_sha256
        or package.controller_runtime_receipt_sha256
        != runtime.controller_runtime_receipt_sha256
    ):
        raise RollbackAuthorityError(
            "rollback_recovery_delegation_expectation_invalid"
        )
    public_key, trust_sha256 = _verified_trust_public_key(
        trust_bundle_json,
        expected_namespace=expected_namespace,
        expected_key_id=expected_key_id,
        expected_trust_bundle_sha256=expected_trust_bundle_sha256,
    )
    envelope = _parse_document(
        delegation_json, "rollback_recovery_delegation_invalid"
    )
    payload = envelope.get("payload")
    signature = envelope.get("signature")
    if (
        set(envelope) != _ENVELOPE_KEYS
        or envelope.get("schema_version")
        != RECOVERY_DELEGATION_SCHEMA_VERSION
        or type(payload) is not dict
        or type(signature) is not dict
        or set(payload) != _RECOVERY_DELEGATION_PAYLOAD_KEYS
        or set(signature) != _SIGNATURE_KEYS
    ):
        raise RollbackAuthorityError(
            "rollback_recovery_delegation_invalid"
        )
    assert isinstance(payload, dict)
    assert isinstance(signature, dict)
    expected_payload_bindings = {
        "authorization_namespace": expected_namespace,
        "thread_id": expected_thread_id,
        "install_scope_id": expected_install_scope_id,
        "rollback_scope_id": expected_rollback_scope_id,
        "authorization_text_sha256": expected_authorization_text_sha256,
        "candidate_git_commit": package.candidate_git_commit,
        "candidate_git_tree": package.candidate_git_tree,
        "package_manifest_sha256": package.package_manifest_sha256,
        "controller_runtime_receipt_sha256": (
            runtime.controller_runtime_receipt_sha256
        ),
        "controller_contract_sha256": package.controller_contract_sha256,
        "execution_plan_sha256": package.execution_plan_sha256,
        "exact_target_contract_sha256": package.exact_targets_sha256,
        "installation_execution_id": expected_installation_execution_id,
        "key_id": expected_key_id,
    }
    if (
        payload.get("schema_version")
        != RECOVERY_DELEGATION_PAYLOAD_SCHEMA_VERSION
        or any(
            payload.get(name) != expected
            for name, expected in expected_payload_bindings.items()
        )
        or payload.get("single_use") is not True
        or payload.get("empty_only") is not True
        or not _strict_structure_equal(
            payload.get("derivation_policy"),
            dict(RECOVERY_DERIVATION_POLICY),
        )
        or payload.get("source_postgres_read_count") != 0
        or payload.get("production_data_read") is not False
        or payload.get("provider_calls") != 0
        or payload.get("activation_allowed") is not False
        or type(payload.get("rollback_nonce")) is not str
        or _NONCE_RE.fullmatch(str(payload["rollback_nonce"])) is None
        or type(payload.get("recovery_reservation_nonce")) is not str
        or _NONCE_RE.fullmatch(
            str(payload["recovery_reservation_nonce"])
        )
        is None
        or len(
            {
                install_identity.authorization_nonce_sha256,
                nonce_sha256(str(payload["rollback_nonce"])),
                nonce_sha256(str(payload["recovery_reservation_nonce"])),
            }
        )
        != 3
        or signature.get("algorithm") != "Ed25519"
        or signature.get("key_id") != expected_key_id
    ):
        raise RollbackAuthorityError(
            "rollback_recovery_delegation_binding_invalid"
        )
    issued_at = _timestamp(payload.get("issued_at"))
    not_before = _timestamp(payload.get("not_before"))
    expires_at = _timestamp(payload.get("expires_at"))
    if (
        not issued_at <= not_before < expires_at
        or (expires_at - issued_at).total_seconds()
        > MAX_AUTHORIZATION_LIFETIME_SECONDS
    ):
        raise RollbackAuthorityError(
            "rollback_recovery_delegation_time_invalid"
        )
    signature_bytes = _decode_base64(
        signature.get("value_base64"),
        64,
        "rollback_recovery_delegation_signature_invalid",
    )
    try:
        public_key.verify(signature_bytes, canonical_json_bytes(payload))
    except InvalidSignature as error:
        raise RollbackAuthorityError(
            "rollback_recovery_delegation_signature_invalid"
        ) from error
    delegation_sha256 = hashlib.sha256(
        canonical_json_bytes(envelope)
    ).hexdigest()
    reservation_scope_sha256 = _reservation_scope_sha256(
        payload, delegation_sha256
    )
    reservation = EmptyRollbackRecoveryReservationBinding(
        operation=RECOVERY_RESERVATION_OPERATION,
        nonce=str(payload["recovery_reservation_nonce"]),
        execution_sha256=_reservation_execution_sha256(
            scope_sha256=reservation_scope_sha256,
            delegation_sha256=delegation_sha256,
            trust_bundle_sha256=trust_sha256,
        ),
        authorization_sha256=delegation_sha256,
        scope_sha256=reservation_scope_sha256,
        trust_bundle_sha256=trust_sha256,
        installation_execution_id=expected_installation_execution_id,
        not_before=str(payload["not_before"]),
        expires_at=str(payload["expires_at"]),
    )
    evidence = EmptyRollbackRecoveryDelegationEvidence(
        authorization_namespace=expected_namespace,
        thread_id=expected_thread_id,
        install_scope_id=expected_install_scope_id,
        rollback_scope_id=expected_rollback_scope_id,
        authorization_text_sha256=expected_authorization_text_sha256,
        key_id=expected_key_id,
        rollback_nonce=str(payload["rollback_nonce"]),
        delegation_sha256=delegation_sha256,
        trust_bundle_sha256=trust_sha256,
        candidate_git_commit=package.candidate_git_commit,
        candidate_git_tree=package.candidate_git_tree,
        package_manifest_sha256=package.package_manifest_sha256,
        controller_contract_sha256=package.controller_contract_sha256,
        execution_plan_sha256=package.execution_plan_sha256,
        exact_target_contract_sha256=package.exact_targets_sha256,
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
        installation_execution_id=expected_installation_execution_id,
        installation_nonce_sha256=(
            install_identity.authorization_nonce_sha256
        ),
        issued_at=str(payload["issued_at"]),
        not_before=str(payload["not_before"]),
        expires_at=str(payload["expires_at"]),
        recovery_reservation=reservation,
    )
    return _VerifiedEmptyRollbackRecoveryDelegation(
        evidence, _RECOVERY_DELEGATION_TOKEN
    )


def derive_empty_rollback_execution_capability_from_recovery_delegation(
    recovery_delegation_capability: object,
    scope_json: bytes | str,
    *,
    expected_bindings: EmptyRollbackExpectedBindings,
) -> object:
    """Bind verified final install evidence to a pre-effect delegation."""

    delegation = recovery_delegation_evidence(
        recovery_delegation_capability
    )
    if (
        type(expected_bindings) is not EmptyRollbackExpectedBindings
        or expected_bindings.candidate_git_commit
        != delegation.candidate_git_commit
        or expected_bindings.candidate_git_tree
        != delegation.candidate_git_tree
        or expected_bindings.package_manifest_sha256
        != delegation.package_manifest_sha256
        or expected_bindings.controller_runtime_receipt_sha256
        != delegation.controller_runtime_receipt_sha256
        or expected_bindings.installation_execution_id
        != delegation.installation_execution_id
        or expected_bindings.installation_nonce_sha256
        != delegation.installation_nonce_sha256
    ):
        raise RollbackAuthorityError(
            "rollback_recovery_local_binding_mismatch"
        )
    scope = _parse_document(
        scope_json, "rollback_recovery_derived_scope_invalid"
    )
    if (
        set(scope) != _SCOPE_KEYS
        or scope.get("schema_version") != SCOPE_SCHEMA_VERSION
        or scope.get("operation") != ROLLBACK_OPERATION
        or scope.get("authorization_namespace")
        != delegation.authorization_namespace
        or scope.get("thread_id") != delegation.thread_id
        or scope.get("scope_id") != delegation.rollback_scope_id
        or not _strict_structure_equal(
            scope.get("empty_only_policy"), _EMPTY_ONLY_POLICY
        )
        or not _strict_structure_equal(
            scope.get("retention_policy"), _RETENTION_POLICY
        )
    ):
        raise RollbackAuthorityError(
            "rollback_recovery_derived_scope_invalid"
        )
    expected_scope_bindings = {
        "candidate_git_commit": expected_bindings.candidate_git_commit,
        "candidate_git_tree": expected_bindings.candidate_git_tree,
        "package_manifest_sha256": expected_bindings.package_manifest_sha256,
        "controller_runtime_receipt_sha256": (
            expected_bindings.controller_runtime_receipt_sha256
        ),
        "installation_execution_id": (
            expected_bindings.installation_execution_id
        ),
        "installation_receipt_sha256": (
            expected_bindings.installation_receipt_sha256
        ),
        "rollback_plan_sha256": expected_bindings.rollback_plan_sha256,
        "exact_targets_sha256": expected_bindings.exact_targets_sha256,
        "eligibility_receipt_sha256": (
            expected_bindings.eligibility_receipt_sha256
        ),
        "resource_ledger_head_sha256": (
            expected_bindings.resource_ledger_head_sha256
        ),
    }
    if any(
        scope.get(name) != expected
        for name, expected in expected_scope_bindings.items()
    ):
        raise RollbackAuthorityError(
            "rollback_recovery_local_binding_mismatch"
        )
    scope_sha256 = hashlib.sha256(canonical_json_bytes(scope)).hexdigest()
    evidence = EmptyRollbackAuthorityEvidence(
        operation=ROLLBACK_OPERATION,
        authorization_namespace=delegation.authorization_namespace,
        thread_id=delegation.thread_id,
        scope_id=delegation.rollback_scope_id,
        authorization_id=(
            "recovery-delegation-" + delegation.delegation_sha256[:32]
        ),
        key_id=delegation.key_id,
        nonce=delegation.rollback_nonce,
        scope_sha256=scope_sha256,
        authorization_sha256=delegation.delegation_sha256,
        trust_bundle_sha256=delegation.trust_bundle_sha256,
        candidate_git_commit=delegation.candidate_git_commit,
        candidate_git_tree=delegation.candidate_git_tree,
        package_manifest_sha256=delegation.package_manifest_sha256,
        controller_runtime_receipt_sha256=(
            delegation.controller_runtime_receipt_sha256
        ),
        controller_runtime_root=delegation.controller_runtime_root,
        controller_runtime_tree_sha256=(
            delegation.controller_runtime_tree_sha256
        ),
        controller_release_root=delegation.controller_release_root,
        controller_release_tree_sha256=(
            delegation.controller_release_tree_sha256
        ),
        controller_release_package_manifest_path=(
            delegation.controller_release_package_manifest_path
        ),
        controller_runtime_interpreter_path=(
            delegation.controller_runtime_interpreter_path
        ),
        controller_runtime_interpreter_sha256=(
            delegation.controller_runtime_interpreter_sha256
        ),
        controller_runtime_inventory_path=(
            delegation.controller_runtime_inventory_path
        ),
        controller_runtime_inventory_sha256=(
            delegation.controller_runtime_inventory_sha256
        ),
        controller_requirements_lock_sha256=(
            delegation.controller_requirements_lock_sha256
        ),
        supervisor_launcher_path=delegation.supervisor_launcher_path,
        supervisor_launcher_sha256=delegation.supervisor_launcher_sha256,
        installation_execution_id=expected_bindings.installation_execution_id,
        installation_nonce_sha256=(
            expected_bindings.installation_nonce_sha256
        ),
        installation_receipt_sha256=(
            expected_bindings.installation_receipt_sha256
        ),
        rollback_plan_sha256=expected_bindings.rollback_plan_sha256,
        exact_targets_sha256=expected_bindings.exact_targets_sha256,
        eligibility_receipt_sha256=(
            expected_bindings.eligibility_receipt_sha256
        ),
        resource_ledger_head_sha256=(
            expected_bindings.resource_ledger_head_sha256
        ),
        not_before=delegation.not_before,
        expires_at=delegation.expires_at,
        recovery_reservation=delegation.recovery_reservation,
    )
    return _VerifiedEmptyRollbackCapability(evidence, _CAPABILITY_TOKEN)


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
    verified_install_scope_capability: object,
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
        install_identity = verified_dormant_install_authority_identity(
            verified_install_scope_capability
        )
    except (
        PackageCapabilityError,
        ControllerRuntimeCapabilityError,
        ClaimedExecutionBindingError,
    ) as error:
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
        or install_identity.thread_id != expected_thread_id
        or install_identity.authorization_sha256
        != package_evidence.authorization_sha256
        or install_identity.scope_sha256 != package_evidence.scope_sha256
        or install_identity.authorization_nonce_sha256
        != expected_bindings.installation_nonce_sha256
        or install_identity.execution_id
        != expected_bindings.installation_execution_id
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
        ("installation_execution_id", expected_bindings.installation_execution_id),
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
        or nonce_sha256(str(payload["nonce"]))
        == install_identity.authorization_nonce_sha256
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
        installation_execution_id=expected_bindings.installation_execution_id,
        installation_nonce_sha256=(
            install_identity.authorization_nonce_sha256
        ),
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
    "EmptyRollbackRecoveryDelegationEvidence",
    "EmptyRollbackRecoveryReservationBinding",
    "RECOVERY_DELEGATION_PAYLOAD_SCHEMA_VERSION",
    "RECOVERY_DELEGATION_SCHEMA_VERSION",
    "RECOVERY_DERIVATION_POLICY",
    "RECOVERY_RESERVATION_OPERATION",
    "RECOVERY_RESERVATION_SCOPE_SCHEMA_VERSION",
    "ROLLBACK_OPERATION",
    "RollbackAuthorityError",
    "SCOPE_SCHEMA_VERSION",
    "TRUST_BUNDLE_SCHEMA_VERSION",
    "canonical_json_bytes",
    "derive_empty_rollback_execution_capability_from_recovery_delegation",
    "recovery_delegation_evidence",
    "recovery_reservation_binding",
    "rollback_capability_evidence",
    "verify_empty_rollback_execution_capability",
    "verify_empty_rollback_recovery_delegation",
]
