from __future__ import annotations

"""Mint an opaque, claim-backed binding for one Phase 8B execution.

The signed scope is re-bound by its exact SHA-256 before the durable nonce
claim is made.  Callers cannot choose an execution or attempt identifier:
both are derived from the durable claim.  The returned object is deliberately
opaque and is the only input accepted by the Phase 8B durable journal.

This inactive package rehashes caller-supplied package and artifact bytes.  It
does not yet consume an opaque capability minted by the closed package
verifier, so it is not a complete live execution claim boundary.
"""

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Final

from .authority_state import AuthorityState, HASH_RE
from .authority import (
    AuthorityVerificationError,
    canonical_json_bytes,
    _execution_capability_evidence,
)
from .execution_authority import (
    ExecutionAuthorityError,
    TrustedUtcClock,
    claim_execution_authority,
)
from .execution_lock import (
    ExecutionLockError,
    HeldExecutionLockCapability,
    validate_held_execution_lock,
)


RESULT_TYPE: Final = "phase8b_claimed_execution_binding_v1"
_EXECUTION_ID_DOMAIN: Final = b"governed-memory-phase8b-execution-id-v1\x00"
_ATTEMPT_ID_DOMAIN: Final = b"governed-memory-phase8b-attempt-id-v1\x00"
_JOURNAL_BINDING_DOMAIN: Final = (
    b"governed-memory-phase8b-journal-binding-v1\x00"
)
_COMMIT_RE: Final = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_SIGNED_BINDING_KEYS: Final = (
    "candidate_git_commit",
    "candidate_git_tree",
    "package_manifest_sha256",
    "controller_contract_sha256",
    "execution_plan_sha256",
    "exact_targets_sha256",
)
_CONTRACT_PATH: Final = (
    "ops/governed_memory/installation/phase8b/contract.json"
)
_PLAN_PATH: Final = (
    "ops/governed_memory/installation/phase8b/controller_plan.json"
)
_STORE_SPEC_PATH: Final = "ops/governed_memory/installation/store_spec.json"
_RESOURCE_LEDGER_PATH: Final = (
    "tools/governed_memory_install/resource_identity.py"
)
_MAX_PACKAGE_MANIFEST_BYTES: Final = 4 * 1024 * 1024


class ClaimedExecutionBindingError(RuntimeError):
    """Refuse a forged or incomplete execution binding."""


@dataclass(frozen=True, slots=True)
class ClaimedExecutionBindingEvidence:
    result_type: str
    claim_result: str
    operation: str
    claim_sha256: str
    execution_sha256: str
    authorization_sha256: str
    scope_sha256: str
    trust_bundle_sha256: str
    candidate_git_commit: str
    candidate_git_tree: str
    package_manifest_sha256: str
    controller_contract_sha256: str
    execution_plan_sha256: str
    controller_model_sha256: str
    exact_targets_sha256: str
    store_spec_sha256: str
    resource_identity_implementation_sha256: str
    global_lock_path_sha256: str
    authority_state_path_sha256: str
    resource_identity_ledger_path_sha256: str
    execution_journal_path: str
    execution_id: str
    attempt_id: str
    journal_binding_sha256: str


_CLAIMED_EXECUTION_TOKEN = object()


class _ClaimedExecutionBinding:
    __slots__ = ("_evidence", "_token")

    def __init__(
        self,
        evidence: ClaimedExecutionBindingEvidence,
        token: object,
    ) -> None:
        if token is not _CLAIMED_EXECUTION_TOKEN:
            raise ClaimedExecutionBindingError(
                "claimed_execution_binding_invalid"
            )
        self._evidence = evidence
        self._token = token

    def __repr__(self) -> str:
        return "ClaimedExecutionBinding(<content-redacted>)"


def _claimed_execution_binding_evidence(
    value: object,
) -> ClaimedExecutionBindingEvidence:
    if (
        type(value) is not _ClaimedExecutionBinding
        or value._token is not _CLAIMED_EXECUTION_TOKEN
        or type(value._evidence) is not ClaimedExecutionBindingEvidence
    ):
        raise ClaimedExecutionBindingError("claimed_execution_binding_invalid")
    evidence = value._evidence
    if evidence.result_type != RESULT_TYPE:
        raise ClaimedExecutionBindingError("claimed_execution_binding_invalid")
    return evidence


def _require_hash(value: object, code: str) -> str:
    if not isinstance(value, str) or HASH_RE.fullmatch(value) is None:
        raise ClaimedExecutionBindingError(code)
    return value


def _canonical_scope(
    signed_scope_json: bytes | str,
    *,
    expected_scope_sha256: str,
) -> dict[str, object]:
    if isinstance(signed_scope_json, str):
        try:
            raw = signed_scope_json.encode("ascii")
        except UnicodeError as error:
            raise ClaimedExecutionBindingError(
                "claimed_execution_scope_invalid"
            ) from error
    elif isinstance(signed_scope_json, bytes):
        raw = signed_scope_json
    else:
        raise ClaimedExecutionBindingError("claimed_execution_scope_invalid")
    if hashlib.sha256(raw).hexdigest() != expected_scope_sha256:
        raise ClaimedExecutionBindingError("claimed_execution_scope_mismatch")
    try:
        value = json.loads(raw.decode("ascii"))
        canonical = canonical_json_bytes(value)
    except (UnicodeError, json.JSONDecodeError, AuthorityVerificationError) as error:
        raise ClaimedExecutionBindingError(
            "claimed_execution_scope_invalid"
        ) from error
    if type(value) is not dict or canonical != raw:
        raise ClaimedExecutionBindingError("claimed_execution_scope_invalid")
    return value


def _raw_content(value: bytes | str, code: str) -> bytes:
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        try:
            raw = value.encode("utf-8")
        except UnicodeError as error:
            raise ClaimedExecutionBindingError(code) from error
    else:
        raise ClaimedExecutionBindingError(code)
    if not raw or len(raw) > _MAX_PACKAGE_MANIFEST_BYTES:
        raise ClaimedExecutionBindingError(code)
    return raw


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ClaimedExecutionBindingError(
                "claimed_execution_package_manifest_invalid"
            )
        result[key] = value
    return result


def _parse_json_document(raw: bytes, code: str) -> dict[str, object]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda unused: (_ for _ in ()).throw(
                ClaimedExecutionBindingError(code)
            ),
        )
    except ClaimedExecutionBindingError:
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ClaimedExecutionBindingError(code) from error
    if type(value) is not dict:
        raise ClaimedExecutionBindingError(code)
    return value


def _package_artifact_bindings(
    package_manifest_json: bytes | str,
    *,
    bindings: dict[str, str],
    controller_contract_json: bytes | str,
    execution_plan_json: bytes | str,
    store_spec_json: bytes | str,
    resource_identity_ledger_source: bytes | str,
) -> tuple[dict[str, object], str, str, str]:
    raw = _raw_content(
        package_manifest_json,
        "claimed_execution_package_manifest_invalid",
    )
    if hashlib.sha256(raw).hexdigest() != bindings["package_manifest_sha256"]:
        raise ClaimedExecutionBindingError(
            "claimed_execution_package_manifest_mismatch"
        )
    manifest = _parse_json_document(
        raw,
        "claimed_execution_package_manifest_invalid",
    )
    if type(manifest.get("artifacts")) is not dict:
        raise ClaimedExecutionBindingError(
            "claimed_execution_package_manifest_invalid"
        )
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, dict)
    contract_raw = _raw_content(
        controller_contract_json,
        "claimed_execution_contract_invalid",
    )
    plan_raw = _raw_content(
        execution_plan_json,
        "claimed_execution_plan_invalid",
    )
    store_spec_raw = _raw_content(
        store_spec_json,
        "claimed_execution_store_spec_invalid",
    )
    resource_ledger_raw = _raw_content(
        resource_identity_ledger_source,
        "claimed_execution_resource_ledger_invalid",
    )
    contract_hash = hashlib.sha256(contract_raw).hexdigest()
    plan_hash = hashlib.sha256(plan_raw).hexdigest()
    store_spec_hash = hashlib.sha256(store_spec_raw).hexdigest()
    resource_ledger_hash = hashlib.sha256(resource_ledger_raw).hexdigest()
    expected = {
        _CONTRACT_PATH: contract_hash,
        _PLAN_PATH: plan_hash,
        _STORE_SPEC_PATH: store_spec_hash,
        _RESOURCE_LEDGER_PATH: resource_ledger_hash,
    }
    if (
        contract_hash != bindings["controller_contract_sha256"]
        or plan_hash != bindings["execution_plan_sha256"]
        or any(artifacts.get(path) != digest for path, digest in expected.items())
    ):
        raise ClaimedExecutionBindingError(
            "claimed_execution_package_artifact_mismatch"
        )
    contract = _parse_json_document(
        contract_raw,
        "claimed_execution_contract_invalid",
    )
    plan = _parse_json_document(plan_raw, "claimed_execution_plan_invalid")
    _parse_json_document(store_spec_raw, "claimed_execution_store_spec_invalid")
    install_steps = plan.get("install_steps")
    if type(install_steps) is not list:
        raise ClaimedExecutionBindingError("claimed_execution_plan_invalid")
    controller_model_hash = hashlib.sha256(
        canonical_json_bytes(install_steps)
    ).hexdigest()
    return contract, store_spec_hash, resource_ledger_hash, controller_model_hash


def _signed_bindings(scope: dict[str, object]) -> dict[str, str]:
    values: dict[str, str] = {}
    for key in _SIGNED_BINDING_KEYS:
        value = scope.get(key)
        if key in {"candidate_git_commit", "candidate_git_tree"}:
            if not isinstance(value, str) or _COMMIT_RE.fullmatch(value) is None:
                raise ClaimedExecutionBindingError(
                    "claimed_execution_signed_binding_invalid"
                )
        elif not isinstance(value, str) or HASH_RE.fullmatch(value) is None:
            raise ClaimedExecutionBindingError(
                "claimed_execution_signed_binding_invalid"
            )
        values[key] = value
    return values


def _hash_document(domain: bytes, document: dict[str, str]) -> str:
    return hashlib.sha256(domain + canonical_json_bytes(document)).hexdigest()


def claim_phase8b_execution_binding(
    verified_scope_capability: object,
    *,
    signed_scope_json: bytes | str,
    package_manifest_json: bytes | str,
    controller_contract_json: bytes | str,
    execution_plan_json: bytes | str,
    store_spec_json: bytes | str,
    resource_identity_ledger_source: bytes | str,
    state: AuthorityState,
    clock: TrustedUtcClock,
    held_lock: HeldExecutionLockCapability,
) -> object:
    """Claim authority and mint a stable opaque execution binding.

    All caller-controlled hashes and the exact signed scope are validated
    before ``AuthorityState`` is touched.  The durable journal performs its
    own token check before it accesses a journal path.
    """

    try:
        scope_evidence = _execution_capability_evidence(
            verified_scope_capability
        )
    except AuthorityVerificationError as error:
        raise ClaimedExecutionBindingError(
            "claimed_execution_capability_invalid"
        ) from error
    if scope_evidence.operation != "dormant_install":
        raise ClaimedExecutionBindingError(
            "claimed_execution_operation_invalid"
        )
    if type(state) is not AuthorityState:
        raise ClaimedExecutionBindingError("claimed_execution_state_invalid")
    try:
        validate_held_execution_lock(held_lock)
    except ExecutionLockError as error:
        raise ClaimedExecutionBindingError(
            "claimed_execution_lock_not_held"
        ) from error
    scope = _canonical_scope(
        signed_scope_json,
        expected_scope_sha256=scope_evidence.scope_sha256,
    )
    bindings = _signed_bindings(scope)
    (
        contract,
        store_spec_hash,
        resource_ledger_hash,
        controller_model_hash,
    ) = _package_artifact_bindings(
        package_manifest_json,
        bindings=bindings,
        controller_contract_json=controller_contract_json,
        execution_plan_json=execution_plan_json,
        store_spec_json=store_spec_json,
        resource_identity_ledger_source=resource_identity_ledger_source,
    )
    exact_targets = contract.get("exact_targets")
    if type(exact_targets) is not dict or hashlib.sha256(
        canonical_json_bytes(exact_targets)
    ).hexdigest() != bindings["exact_targets_sha256"]:
        raise ClaimedExecutionBindingError(
            "claimed_execution_exact_targets_mismatch"
        )
    assert isinstance(exact_targets, dict)
    global_lock_path = exact_targets.get("global_lock")
    authority_state_path = exact_targets.get("nonce_state")
    resource_ledger_path = exact_targets.get("resource_identity_ledger")
    execution_journal_template = exact_targets.get("execution_journal")
    if not all(
        isinstance(value, str) and value.startswith("/")
        for value in (
            global_lock_path,
            authority_state_path,
            resource_ledger_path,
            execution_journal_template,
        )
    ):
        raise ClaimedExecutionBindingError(
            "claimed_execution_exact_targets_invalid"
        )
    assert isinstance(global_lock_path, str)
    assert isinstance(authority_state_path, str)
    assert isinstance(resource_ledger_path, str)
    assert isinstance(execution_journal_template, str)
    if execution_journal_template.count("{execution_id}") != 1:
        raise ClaimedExecutionBindingError(
            "claimed_execution_exact_targets_invalid"
        )
    if str(held_lock._owner.path) != global_lock_path:
        raise ClaimedExecutionBindingError(
            "claimed_execution_global_lock_mismatch"
        )
    if str(state.path) != authority_state_path:
        raise ClaimedExecutionBindingError(
            "claimed_execution_authority_state_path_mismatch"
        )
    global_lock_path_hash = hashlib.sha256(
        global_lock_path.encode("utf-8")
    ).hexdigest()
    authority_state_path_hash = hashlib.sha256(
        authority_state_path.encode("utf-8")
    ).hexdigest()
    resource_ledger_path_hash = hashlib.sha256(
        resource_ledger_path.encode("utf-8")
    ).hexdigest()

    try:
        permit = claim_execution_authority(
            verified_scope_capability,
            state=state,
            clock=clock,
            held_lock=held_lock,
            expected_operation="dormant_install",
        )
    except ExecutionAuthorityError as error:
        raise ClaimedExecutionBindingError(
            "claimed_execution_authority_refused"
        ) from error

    claim_material = {
        "claim_sha256": permit.claim_sha256,
        "execution_sha256": permit.execution_sha256,
        "operation_sha256": permit.operation_sha256,
    }
    execution_id = _hash_document(_EXECUTION_ID_DOMAIN, claim_material)
    attempt_digest = _hash_document(_ATTEMPT_ID_DOMAIN, claim_material)
    attempt_id = "phase8b-" + attempt_digest[:40]
    execution_journal_path = execution_journal_template.replace(
        "{execution_id}", execution_id
    )
    if "{" in execution_journal_path or "}" in execution_journal_path:
        raise ClaimedExecutionBindingError(
            "claimed_execution_exact_targets_invalid"
        )
    journal_material = {
        **bindings,
        **claim_material,
        "authorization_sha256": permit.authorization_sha256,
        "scope_sha256": permit.scope_sha256,
        "trust_bundle_sha256": permit.trust_bundle_sha256,
        "store_spec_sha256": store_spec_hash,
        "resource_identity_implementation_sha256": resource_ledger_hash,
        "controller_model_sha256": controller_model_hash,
        "global_lock_path_sha256": global_lock_path_hash,
        "authority_state_path_sha256": authority_state_path_hash,
        "resource_identity_ledger_path_sha256": resource_ledger_path_hash,
        "execution_id": execution_id,
        "attempt_id": attempt_id,
        "execution_journal_path": execution_journal_path,
    }
    journal_binding = _hash_document(
        _JOURNAL_BINDING_DOMAIN,
        journal_material,
    )
    evidence = ClaimedExecutionBindingEvidence(
        result_type=RESULT_TYPE,
        claim_result=permit.result,
        operation="dormant_install",
        claim_sha256=permit.claim_sha256,
        execution_sha256=permit.execution_sha256,
        authorization_sha256=permit.authorization_sha256,
        scope_sha256=permit.scope_sha256,
        trust_bundle_sha256=permit.trust_bundle_sha256,
        candidate_git_commit=bindings["candidate_git_commit"],
        candidate_git_tree=bindings["candidate_git_tree"],
        package_manifest_sha256=bindings["package_manifest_sha256"],
        controller_contract_sha256=bindings[
            "controller_contract_sha256"
        ],
        execution_plan_sha256=bindings["execution_plan_sha256"],
        controller_model_sha256=controller_model_hash,
        exact_targets_sha256=bindings["exact_targets_sha256"],
        store_spec_sha256=store_spec_hash,
        resource_identity_implementation_sha256=resource_ledger_hash,
        global_lock_path_sha256=global_lock_path_hash,
        authority_state_path_sha256=authority_state_path_hash,
        resource_identity_ledger_path_sha256=resource_ledger_path_hash,
        execution_journal_path=execution_journal_path,
        execution_id=execution_id,
        attempt_id=attempt_id,
        journal_binding_sha256=journal_binding,
    )
    return _ClaimedExecutionBinding(evidence, _CLAIMED_EXECUTION_TOKEN)


__all__ = [
    "ClaimedExecutionBindingError",
    "claim_phase8b_execution_binding",
]
