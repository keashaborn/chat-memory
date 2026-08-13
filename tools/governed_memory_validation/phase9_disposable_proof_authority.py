from __future__ import annotations

"""Repository-only signer for one exact Phase 9 empty rollback.

This module is deliberately excluded from the controller release.  It accepts
no path, command, SQL, URL, resource name, or loose signing payload.  Before a
signature is returned it independently re-reads the fixed durable install
receipt, resolved store specification, and resource ledger and reconstructs
the only eligible rollback scope and authorization payload.
"""

import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
from types import MappingProxyType
from typing import Final, Mapping

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tools.governed_memory_install.durable_receipts import (
    DurableReceiptStore,
    ReceiptArtifact,
)
from tools.governed_memory_install.linux_plan import validate_store_spec
from tools.governed_memory_install.receipts import verify_install_receipt
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
    AUTHORIZATION_PAYLOAD_SCHEMA_VERSION,
    AUTHORIZATION_SCHEMA_VERSION,
    ROLLBACK_OPERATION,
    SCOPE_SCHEMA_VERSION,
)
from tools.governed_memory_validation import (
    run_disposable_installation_live_proof as runner,
)


MAX_REQUEST_BYTES: Final = 1024 * 1024
REQUEST_SCHEMA: Final = "governed-memory-phase9-rollback-signing-request-v1"
RESPONSE_SCHEMA: Final = "governed-memory-phase9-rollback-signing-response-v1"
RESOLVED_STORE_SPEC_PATH: Final = Path(
    "/etc/governed-memory-controller/store_spec.json"
)
EXECUTIONS_ROOT: Final = Path(
    "/var/lib/governed-memory-controller/executions"
)
_REQUEST_KEYS: Final = frozenset(
    {
        "schema_version",
        "permit_nonce",
        "candidate_git_commit",
        "candidate_git_tree",
        "package_manifest_sha256",
        "controller_runtime_receipt_sha256",
        "exact_targets_sha256",
        "installation_execution_id",
        "installation_receipt_sha256",
        "resource_ledger_head_sha256",
        "eligibility_receipt_sha256",
        "rollback_scope_base64",
        "rollback_authorization_payload_base64",
        "eligibility_receipt",
    }
)


class Phase9DisposableProofAuthorityError(RuntimeError):
    """Content-free refusal from the external proof signing authority."""


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
        raise Phase9DisposableProofAuthorityError(
            "phase9_disposable_authority_document_invalid"
        ) from error


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise Phase9DisposableProofAuthorityError(
                "phase9_disposable_authority_document_invalid"
            )
        result[key] = value
    return result


def _document(raw: bytes) -> dict[str, object]:
    if type(raw) is not bytes or not 1 <= len(raw) <= MAX_REQUEST_BYTES:
        raise Phase9DisposableProofAuthorityError(
            "phase9_disposable_authority_document_invalid"
        )
    try:
        value = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_unique,
            parse_constant=lambda unused: (_ for _ in ()).throw(
                Phase9DisposableProofAuthorityError(
                    "phase9_disposable_authority_document_invalid"
                )
            ),
        )
    except Phase9DisposableProofAuthorityError:
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise Phase9DisposableProofAuthorityError(
            "phase9_disposable_authority_document_invalid"
        ) from error
    if type(value) is not dict or _canonical(value) != raw:
        raise Phase9DisposableProofAuthorityError(
            "phase9_disposable_authority_document_invalid"
        )
    return value


def _decode_document(value: object) -> tuple[bytes, dict[str, object]]:
    if type(value) is not str:
        raise Phase9DisposableProofAuthorityError(
            "phase9_disposable_authority_request_invalid"
        )
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as error:
        raise Phase9DisposableProofAuthorityError(
            "phase9_disposable_authority_request_invalid"
        ) from error
    return raw, _document(raw)


def _read_resolved_store_spec() -> Mapping[str, object]:
    descriptor = -1
    try:
        descriptor = os.open(
            RESOLVED_STORE_SPEC_PATH,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        named = RESOLVED_STORE_SPEC_PATH.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != 0
            or opened.st_gid != 0
            or opened.st_nlink != 1
            or not 1 <= opened.st_size <= 16 * 1024 * 1024
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise Phase9DisposableProofAuthorityError(
                "phase9_disposable_authority_store_spec_invalid"
            )
        raw = os.read(descriptor, opened.st_size + 1)
        if len(raw) != opened.st_size or os.fstat(descriptor) != opened:
            raise Phase9DisposableProofAuthorityError(
                "phase9_disposable_authority_store_spec_invalid"
            )
    except Phase9DisposableProofAuthorityError:
        raise
    except OSError as error:
        raise Phase9DisposableProofAuthorityError(
            "phase9_disposable_authority_store_spec_invalid"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not raw.endswith(b"\n"):
        raise Phase9DisposableProofAuthorityError(
            "phase9_disposable_authority_store_spec_invalid"
        )
    return MappingProxyType(validate_store_spec(_document(raw[:-1]), allow_placeholders=False))


def _expected_eligibility(
    install: Mapping[str, object], resources: object
) -> Mapping[str, object]:
    evidence, unused = verified_rollback_resource_parts(resources)
    observation_set_sha256 = hashlib.sha256(
        _canonical(
            {
                "schema_version": "governed-memory-phase9-zero-state-expectation-v1",
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
    ).hexdigest()
    return MappingProxyType(
        build_empty_rollback_eligibility_receipt(
            candidate_git_commit=str(install["candidate_git_commit"]),
            candidate_git_tree=str(install["candidate_git_tree"]),
            package_manifest_sha256=str(install["package_manifest_sha256"]),
            installation_execution_id=str(install["execution_id"]),
            installation_receipt_sha256=str(install["receipt_sha256"]),
            exact_targets_sha256=evidence.exact_targets_sha256,
            resource_ledger_head_sha256=evidence.resource_ledger_head_sha256,
            observation_set_sha256=observation_set_sha256,
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


def _independent_install_and_resources(
    execution_id: str,
) -> tuple[Mapping[str, object], object]:
    try:
        durable = DurableReceiptStore.production().read(
            ReceiptArtifact.INSTALL, execution_id
        )
        install = verify_install_receipt(durable.canonical_receipt)
        resolved = _read_resolved_store_spec()
        binding = resolved.get("execution_binding")
        if type(binding) is not dict or binding.get("execution_id") != execution_id:
            raise Phase9DisposableProofAuthorityError(
                "phase9_disposable_authority_install_binding_invalid"
            )
        journal_binding = binding.get("binding_sha256")
        if type(journal_binding) is not str:
            raise Phase9DisposableProofAuthorityError(
                "phase9_disposable_authority_install_binding_invalid"
            )
        records = load_ledger(
            EXECUTIONS_ROOT / execution_id / "resources.jsonl",
            expected_binding_sha256=resource_ledger_binding_sha256(
                journal_binding
            ),
            expected_uid=0,
        )
        if (
            not records
            or len(records) != install["resource_ledger_sequence"]
            or records[-1].entry_sha256
            != install["resource_ledger_head_sha256"]
        ):
            raise Phase9DisposableProofAuthorityError(
                "phase9_disposable_authority_ledger_invalid"
            )
        resources = derive_exact_rollback_resources_from_ledger(
            records,
            package_manifest_sha256=str(install["package_manifest_sha256"]),
            expected_ledger_head_sha256=str(
                install["resource_ledger_head_sha256"]
            ),
        )
        return MappingProxyType(install), resources
    except Phase9DisposableProofAuthorityError:
        raise
    except Exception as error:
        raise Phase9DisposableProofAuthorityError(
            "phase9_disposable_authority_install_evidence_invalid"
        ) from error


def _expected_scope(
    permit: Mapping[str, object],
    install: Mapping[str, object],
    eligibility: Mapping[str, object],
    resources: object,
) -> dict[str, object]:
    evidence, unused = verified_rollback_resource_parts(resources)
    return {
        "schema_version": SCOPE_SCHEMA_VERSION,
        "operation": ROLLBACK_OPERATION,
        "authorization_namespace": runner.AUTHORIZATION_NAMESPACE,
        "thread_id": runner.THREAD_ID,
        "scope_id": runner.ROLLBACK_SCOPE_ID,
        "candidate_git_commit": permit["candidate_git_commit"],
        "candidate_git_tree": permit["candidate_git_tree"],
        "package_manifest_sha256": permit["package_manifest_sha256"],
        "controller_runtime_receipt_sha256": permit[
            "controller_runtime_receipt_sha256"
        ],
        "installation_execution_id": install["execution_id"],
        "installation_receipt_sha256": install["receipt_sha256"],
        "rollback_plan_sha256": empty_rollback_plan_sha256(
            eligibility, resources
        ),
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


def _expected_payload(
    permit: Mapping[str, object], scope_raw: bytes
) -> dict[str, object]:
    scope_sha256 = hashlib.sha256(scope_raw).hexdigest()
    return {
        "schema_version": AUTHORIZATION_PAYLOAD_SCHEMA_VERSION,
        "authorization_id": "phase9-disposable-live-rollback-auth-000001",
        "authorization_namespace": runner.AUTHORIZATION_NAMESPACE,
        "thread_id": runner.THREAD_ID,
        "scope_id": runner.ROLLBACK_SCOPE_ID,
        "scope_sha256": scope_sha256,
        "key_id": permit["key_id"],
        "approval_phrase": (
            "APPROVE GOVERNED MEMORY EMPTY STORE ROLLBACK " + scope_sha256
        ),
        "nonce": hashlib.sha256(
            runner.ROLLBACK_NONCE_DOMAIN
            + str(permit["permit_nonce"]).encode("ascii")
        ).hexdigest(),
        "issued_at": permit["issued_at"],
        "not_before": permit["not_before"],
        "expires_at": permit["expires_at"],
        "single_use": True,
    }


@dataclass(slots=True)
class ExactRollbackSigningAuthority:
    permit: Mapping[str, object]
    private_key: Ed25519PrivateKey
    _signed_request_sha256: str | None = None
    _response: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.private_key, Ed25519PrivateKey):
            raise Phase9DisposableProofAuthorityError(
                "phase9_disposable_authority_key_invalid"
            )
        public = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        encoded = base64.b64encode(public).decode("ascii")
        if (
            self.permit.get("public_key_base64") != encoded
            or self.permit.get("key_id") != hashlib.sha256(public).hexdigest()
        ):
            raise Phase9DisposableProofAuthorityError(
                "phase9_disposable_authority_key_invalid"
            )
        runner._verify_rollback_delegation(
            self.permit,
            self.private_key.public_key(),
            str(self.permit["key_id"]),
        )

    def authorize(self, request_raw: bytes) -> Mapping[str, object]:
        request = _document(request_raw)
        request_sha256 = hashlib.sha256(request_raw).hexdigest()
        if (
            self._signed_request_sha256 is not None
            and request_sha256 != self._signed_request_sha256
        ):
            raise Phase9DisposableProofAuthorityError(
                "phase9_disposable_authority_second_request_refused"
            )
        if self._response is not None:
            return self._response
        if set(request) != _REQUEST_KEYS or request.get("schema_version") != REQUEST_SCHEMA:
            raise Phase9DisposableProofAuthorityError(
                "phase9_disposable_authority_request_invalid"
            )
        install, resources = _independent_install_and_resources(
            str(request.get("installation_execution_id"))
        )
        eligibility = _expected_eligibility(install, resources)
        scope_raw, scope = _decode_document(request.get("rollback_scope_base64"))
        payload_raw, payload = _decode_document(
            request.get("rollback_authorization_payload_base64")
        )
        expected_scope = _expected_scope(
            self.permit, install, eligibility, resources
        )
        expected_payload = _expected_payload(self.permit, scope_raw)
        evidence, unused = verified_rollback_resource_parts(resources)
        summary = {
            "permit_nonce": self.permit["permit_nonce"],
            "candidate_git_commit": self.permit["candidate_git_commit"],
            "candidate_git_tree": self.permit["candidate_git_tree"],
            "package_manifest_sha256": self.permit["package_manifest_sha256"],
            "controller_runtime_receipt_sha256": self.permit[
                "controller_runtime_receipt_sha256"
            ],
            "exact_targets_sha256": evidence.exact_targets_sha256,
            "installation_execution_id": install["execution_id"],
            "installation_receipt_sha256": install["receipt_sha256"],
            "resource_ledger_head_sha256": evidence.resource_ledger_head_sha256,
            "eligibility_receipt_sha256": eligibility["receipt_sha256"],
        }
        if (
            any(request.get(key) != value for key, value in summary.items())
            or request.get("eligibility_receipt") != dict(eligibility)
            or scope != expected_scope
            or payload != expected_payload
            or install["candidate_git_commit"]
            != self.permit["candidate_git_commit"]
            or install["candidate_git_tree"] != self.permit["candidate_git_tree"]
            or install["package_manifest_sha256"]
            != self.permit["package_manifest_sha256"]
            or install["controller_runtime_receipt_sha256"]
            != self.permit["controller_runtime_receipt_sha256"]
            or evidence.exact_targets_sha256
            != self.permit["exact_targets_sha256"]
        ):
            raise Phase9DisposableProofAuthorityError(
                "phase9_disposable_authority_request_binding_invalid"
            )
        signature = base64.b64encode(
            self.private_key.sign(payload_raw)
        ).decode("ascii")
        response = MappingProxyType(
            {
                "schema_version": RESPONSE_SCHEMA,
                "result": "exact_delegated_rollback_signed",
                "request_sha256": request_sha256,
                "signature": {
                    "algorithm": "Ed25519",
                    "key_id": self.permit["key_id"],
                    "value_base64": signature,
                },
            }
        )
        self._signed_request_sha256 = request_sha256
        self._response = response
        return response


__all__ = [
    "ExactRollbackSigningAuthority",
    "Phase9DisposableProofAuthorityError",
    "REQUEST_SCHEMA",
    "RESPONSE_SCHEMA",
]
