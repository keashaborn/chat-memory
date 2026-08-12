from __future__ import annotations

"""Mint an opaque, claim-backed binding for one dormant-store installation execution.

The signed scope is re-bound by its exact SHA-256 before the durable nonce
claim is made.  Callers cannot choose an execution or attempt identifier:
both are derived from the durable claim.  The returned object is deliberately
opaque and is the only input accepted by the dormant-store installation durable journal.

Package bytes are accepted only through the opaque output of the closed
package verifier.  The claim boundary never accepts caller-selected artifact
hashes or loose artifact documents.
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
from .package_capability import (
    PackageCapabilityError,
    _package_capability_parts,
)
from .controller_runtime import (
    ControllerRuntimeCapabilityError,
    _controller_runtime_capability_evidence,
)
from .linux_plan import (
    ExecutionBinding,
    LinuxPlanError,
    bind_store_spec,
)


RESULT_TYPE: Final = "dormant_store_install_claimed_execution_binding_v1"
_EXECUTION_ID_DOMAIN: Final = b"governed-memory-dormant_store_install-execution-id-v1\x00"
_ATTEMPT_ID_DOMAIN: Final = b"governed-memory-dormant_store_install-attempt-id-v1\x00"
_JOURNAL_BINDING_DOMAIN: Final = (
    b"governed-memory-dormant_store_install-journal-binding-v1\x00"
)
_COMMIT_RE: Final = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_SIGNED_BINDING_KEYS: Final = (
    "candidate_git_commit",
    "candidate_git_tree",
    "package_manifest_sha256",
    "controller_contract_sha256",
    "execution_plan_sha256",
    "exact_targets_sha256",
    "controller_runtime_receipt_sha256",
)
_CONTRACT_PATH: Final = (
    "ops/governed_memory/installation/current/contract.json"
)


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
    authorization_id: str
    authorization_nonce_sha256: str
    scope_sha256: str
    trust_bundle_sha256: str
    candidate_git_commit: str
    candidate_git_tree: str
    package_manifest_sha256: str
    controller_contract_sha256: str
    execution_plan_sha256: str
    controller_model_sha256: str
    exact_targets_sha256: str
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
    store_spec_sha256: str
    resolved_store_spec_sha256: str
    resource_identity_implementation_sha256: str
    global_lock_path_sha256: str
    authority_state_path_sha256: str
    resource_identity_ledger_path: str
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


def _require_package_capability(value: object):
    try:
        return _package_capability_parts(value)
    except PackageCapabilityError as error:
        raise ClaimedExecutionBindingError(
            "claimed_execution_package_capability_invalid"
        ) from error


def claim_dormant_store_install_execution_binding(
    verified_scope_capability: object,
    *,
    verified_package_capability: object,
    verified_controller_runtime_capability: object,
    state: AuthorityState,
    clock: TrustedUtcClock,
    held_lock: HeldExecutionLockCapability,
) -> object:
    """Claim authority and mint a stable opaque execution binding.

    The package capability is bound to the same signed scope before
    ``AuthorityState`` is touched.  The durable journal performs its own token
    check before it accesses a journal path.
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
    (
        package_evidence,
        package_scope,
        package_artifacts,
    ) = _require_package_capability(verified_package_capability)
    try:
        runtime_evidence = _controller_runtime_capability_evidence(
            verified_controller_runtime_capability
        )
    except ControllerRuntimeCapabilityError as error:
        raise ClaimedExecutionBindingError(
            "claimed_execution_controller_runtime_invalid"
        ) from error
    if (
        package_evidence.authorization_sha256
        != scope_evidence.authorization_sha256
        or package_evidence.scope_sha256 != scope_evidence.scope_sha256
    ):
        raise ClaimedExecutionBindingError(
            "claimed_execution_package_scope_mismatch"
        )
    scope = dict(package_scope)
    bindings = _signed_bindings(scope)
    if (
        runtime_evidence.controller_runtime_receipt_sha256
        != bindings["controller_runtime_receipt_sha256"]
        or runtime_evidence.package_manifest_sha256
        != package_evidence.package_manifest_sha256
        or runtime_evidence.controller_runtime_contract_sha256
        != package_evidence.controller_runtime_contract_sha256
        or runtime_evidence.controller_requirements_lock_sha256
        != package_evidence.controller_requirements_lock_sha256
        or runtime_evidence.supervisor_launcher_sha256
        != package_evidence.supervisor_launcher_sha256
    ):
        raise ClaimedExecutionBindingError(
            "claimed_execution_controller_runtime_mismatch"
        )
    contract = _parse_json_document(
        package_artifacts[_CONTRACT_PATH],
        "claimed_execution_contract_invalid",
    )
    store_spec_hash = package_evidence.store_spec_sha256
    resource_ledger_hash = (
        package_evidence.resource_identity_implementation_sha256
    )
    controller_model_hash = package_evidence.controller_model_sha256
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
    if (
        execution_journal_template.count("{execution_id}") != 1
        or resource_ledger_path.count("{execution_id}") != 1
    ):
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
    attempt_id = "install-" + attempt_digest[:40]
    execution_journal_path = execution_journal_template.replace(
        "{execution_id}", execution_id
    )
    expanded_resource_ledger_path = resource_ledger_path.replace(
        "{execution_id}", execution_id
    )
    if (
        "{" in execution_journal_path
        or "}" in execution_journal_path
        or "{" in expanded_resource_ledger_path
        or "}" in expanded_resource_ledger_path
    ):
        raise ClaimedExecutionBindingError(
            "claimed_execution_exact_targets_invalid"
        )
    resource_ledger_path_hash = hashlib.sha256(
        expanded_resource_ledger_path.encode("utf-8")
    ).hexdigest()
    journal_material = {
        **bindings,
        **claim_material,
        "authorization_sha256": permit.authorization_sha256,
        "authorization_id": scope_evidence.authorization_id,
        "authorization_nonce_sha256": permit.nonce_sha256,
        "scope_sha256": permit.scope_sha256,
        "trust_bundle_sha256": permit.trust_bundle_sha256,
        "store_spec_sha256": store_spec_hash,
        "resource_identity_implementation_sha256": resource_ledger_hash,
        "controller_model_sha256": controller_model_hash,
        "controller_runtime_receipt_sha256": (
            runtime_evidence.controller_runtime_receipt_sha256
        ),
        "controller_runtime_root": runtime_evidence.runtime_root,
        "controller_runtime_tree_sha256": (
            runtime_evidence.runtime_tree_sha256
        ),
        "controller_release_root": runtime_evidence.release_root,
        "controller_release_tree_sha256": (
            runtime_evidence.release_tree_sha256
        ),
        "controller_release_package_manifest_path": (
            runtime_evidence.package_manifest_path
        ),
        "controller_runtime_interpreter_path": (
            runtime_evidence.interpreter_path
        ),
        "controller_runtime_interpreter_sha256": (
            runtime_evidence.interpreter_sha256
        ),
        "controller_runtime_inventory_path": runtime_evidence.inventory_path,
        "controller_runtime_inventory_sha256": (
            runtime_evidence.installed_distribution_inventory_sha256
        ),
        "controller_requirements_lock_sha256": (
            runtime_evidence.controller_requirements_lock_sha256
        ),
        "supervisor_launcher_path": runtime_evidence.supervisor_launcher_path,
        "supervisor_launcher_sha256": (
            runtime_evidence.supervisor_launcher_sha256
        ),
        "global_lock_path_sha256": global_lock_path_hash,
        "authority_state_path_sha256": authority_state_path_hash,
        "resource_identity_ledger_path_sha256": resource_ledger_path_hash,
        "resource_identity_ledger_path": expanded_resource_ledger_path,
        "execution_id": execution_id,
        "attempt_id": attempt_id,
        "execution_journal_path": execution_journal_path,
    }
    journal_binding = _hash_document(
        _JOURNAL_BINDING_DOMAIN,
        journal_material,
    )
    try:
        static_store_spec = _parse_json_document(
            package_artifacts[
                "ops/governed_memory/installation/store_spec.json"
            ],
            "claimed_execution_store_spec_invalid",
        )
        resolved_store_spec = bind_store_spec(
            static_store_spec,
            ExecutionBinding(
                binding_sha256=journal_binding,
                authorization_id=scope_evidence.authorization_id,
                authorization_nonce_sha256=permit.nonce_sha256,
                execution_id=execution_id,
                package_manifest_sha256=bindings["package_manifest_sha256"],
            ),
        )
        resolved_store_spec_sha256 = hashlib.sha256(
            canonical_json_bytes(resolved_store_spec)
        ).hexdigest()
    except (KeyError, LinuxPlanError) as error:
        raise ClaimedExecutionBindingError(
            "claimed_execution_store_spec_invalid"
        ) from error
    evidence = ClaimedExecutionBindingEvidence(
        result_type=RESULT_TYPE,
        claim_result=permit.result,
        operation="dormant_install",
        claim_sha256=permit.claim_sha256,
        execution_sha256=permit.execution_sha256,
        authorization_sha256=permit.authorization_sha256,
        authorization_id=scope_evidence.authorization_id,
        authorization_nonce_sha256=permit.nonce_sha256,
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
        controller_runtime_interpreter_sha256=(
            runtime_evidence.interpreter_sha256
        ),
        controller_runtime_inventory_path=runtime_evidence.inventory_path,
        controller_runtime_inventory_sha256=(
            runtime_evidence.installed_distribution_inventory_sha256
        ),
        controller_requirements_lock_sha256=(
            runtime_evidence.controller_requirements_lock_sha256
        ),
        supervisor_launcher_path=runtime_evidence.supervisor_launcher_path,
        supervisor_launcher_sha256=runtime_evidence.supervisor_launcher_sha256,
        store_spec_sha256=store_spec_hash,
        resolved_store_spec_sha256=resolved_store_spec_sha256,
        resource_identity_implementation_sha256=resource_ledger_hash,
        global_lock_path_sha256=global_lock_path_hash,
        authority_state_path_sha256=authority_state_path_hash,
        resource_identity_ledger_path=expanded_resource_ledger_path,
        resource_identity_ledger_path_sha256=resource_ledger_path_hash,
        execution_journal_path=execution_journal_path,
        execution_id=execution_id,
        attempt_id=attempt_id,
        journal_binding_sha256=journal_binding,
    )
    return _ClaimedExecutionBinding(evidence, _CLAIMED_EXECUTION_TOKEN)


__all__ = [
    "ClaimedExecutionBindingError",
    "claim_dormant_store_install_execution_binding",
]
