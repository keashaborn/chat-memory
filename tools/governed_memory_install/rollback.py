from __future__ import annotations

"""Pure empty-store rollback eligibility and exact-target plan.

This module contains no executor, command, filesystem, secret, network,
database, Qdrant, service, installation, or activation surface.  A caller may
only construct and validate the exact content-free evidence consumed by the
separately authorized rollback entrypoint.
"""

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Final, Iterable, Mapping

from .resource_identity import (
    ResourceIdentityError,
    ResourceIdentityRecord,
    parse_ledger_bytes,
)
from .rollback_authority import rollback_capability_evidence


ELIGIBILITY_SCHEMA: Final = "governed-memory-empty-store-eligibility-receipt-v2"
PLAN_SCHEMA: Final = "governed-memory-empty-store-rollback-plan-v2"
ROLLBACK_OPERATION: Final = "empty_store_rollback"
ZERO_HEAD: Final = "0" * 64

_HASH_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_SAFE_NAME_RE = re.compile(
    r"/?[A-Za-z0-9][A-Za-z0-9._:/@{}+-]{0,511}\Z", re.ASCII
)

_ELIGIBILITY_KEYS = {
    "schema_version",
    "operation",
    "candidate_git_commit",
    "candidate_git_tree",
    "package_manifest_sha256",
    "installation_execution_id",
    "installation_receipt_sha256",
    "exact_targets_sha256",
    "resource_ledger_head_sha256",
    "observation_set_sha256",
    "pilot_ever_started",
    "postgresql_user_rows",
    "projection_queue_rows",
    "qdrant_points",
    "active_clients",
    "legacy_imports",
    "application_services_installed",
    "source_postgres_read_count",
    "production_data_read",
    "provider_calls",
    "receipt_sha256",
}

RESOURCE_KINDS: Final = {
    "systemd_unit",
    "qdrant_alias",
    "qdrant_collection",
    "migration",
    "database",
    "container",
    "volume",
    "network",
    "secret_file",
    "resolved_store_spec",
}

ROLLBACK_RESOURCE_KEYS: Final = (
    "stores_supervisor",
    "qdrant_alias",
    "qdrant_collection",
    "migration_0004",
    "migration_0003",
    "migration_0001",
    "canonical_database_and_roles",
    "qdrant_container",
    "postgres_container",
    "qdrant_volume",
    "postgres_volume",
    "network",
    "qdrant_store_secret",
    "postgres_store_secret",
    "resolved_store_spec",
)

RETAINED_AUDIT_KEYS: Final = (
    "authorization_nonce",
    "installation_journal",
    "rollback_journal",
    "authority_anchor",
    "resource_identity_ledger",
    "terminal_postflight_receipt",
    "installation_receipt",
    "eligibility_receipt",
)


class EmptyRollbackError(RuntimeError):
    """Closed refusal for empty eligibility or exact rollback planning."""


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
        raise EmptyRollbackError("empty_rollback_json_invalid") from error


def _sha(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _is_hash(value: object) -> bool:
    return type(value) is str and _HASH_RE.fullmatch(value) is not None


def _is_commit(value: object) -> bool:
    return type(value) is str and _COMMIT_RE.fullmatch(value) is not None


def _unsigned(value: Mapping[str, object]) -> dict[str, object]:
    return {key: item for key, item in value.items() if key != "receipt_sha256"}


def eligibility_receipt_sha256(value: Mapping[str, object]) -> str:
    return _sha(_unsigned(value))


def build_empty_rollback_eligibility_receipt(
    *,
    candidate_git_commit: str,
    candidate_git_tree: str,
    package_manifest_sha256: str,
    installation_execution_id: str,
    installation_receipt_sha256: str,
    exact_targets_sha256: str,
    resource_ledger_head_sha256: str,
    observation_set_sha256: str,
    pilot_ever_started: bool,
    postgresql_user_rows: int,
    projection_queue_rows: int,
    qdrant_points: int,
    active_clients: int,
    legacy_imports: int,
    application_services_installed: bool,
    source_postgres_read_count: int,
    production_data_read: bool,
    provider_calls: int,
) -> dict[str, object]:
    receipt: dict[str, object] = {
        "schema_version": ELIGIBILITY_SCHEMA,
        "operation": ROLLBACK_OPERATION,
        "candidate_git_commit": candidate_git_commit,
        "candidate_git_tree": candidate_git_tree,
        "package_manifest_sha256": package_manifest_sha256,
        "installation_execution_id": installation_execution_id,
        "installation_receipt_sha256": installation_receipt_sha256,
        "exact_targets_sha256": exact_targets_sha256,
        "resource_ledger_head_sha256": resource_ledger_head_sha256,
        "observation_set_sha256": observation_set_sha256,
        "pilot_ever_started": pilot_ever_started,
        "postgresql_user_rows": postgresql_user_rows,
        "projection_queue_rows": projection_queue_rows,
        "qdrant_points": qdrant_points,
        "active_clients": active_clients,
        "legacy_imports": legacy_imports,
        "application_services_installed": application_services_installed,
        "source_postgres_read_count": source_postgres_read_count,
        "production_data_read": production_data_read,
        "provider_calls": provider_calls,
    }
    receipt["receipt_sha256"] = eligibility_receipt_sha256(receipt)
    return verify_empty_rollback_eligibility_receipt(receipt)


def verify_empty_rollback_eligibility_receipt(
    value: Mapping[str, object],
) -> dict[str, object]:
    receipt = dict(value)
    if (
        set(receipt) != _ELIGIBILITY_KEYS
        or receipt.get("schema_version") != ELIGIBILITY_SCHEMA
        or receipt.get("operation") != ROLLBACK_OPERATION
        or not _is_commit(receipt.get("candidate_git_commit"))
        or not _is_commit(receipt.get("candidate_git_tree"))
        or not _is_hash(receipt.get("installation_execution_id"))
    ):
        raise EmptyRollbackError("empty_rollback_eligibility_shape_invalid")
    for key in (
        "package_manifest_sha256",
        "installation_receipt_sha256",
        "exact_targets_sha256",
        "resource_ledger_head_sha256",
        "observation_set_sha256",
        "receipt_sha256",
    ):
        if not _is_hash(receipt.get(key)):
            raise EmptyRollbackError("empty_rollback_eligibility_hash_invalid")
    if (
        receipt.get("pilot_ever_started") is not False
        or receipt.get("application_services_installed") is not False
        or receipt.get("production_data_read") is not False
    ):
        raise EmptyRollbackError("empty_rollback_not_eligible")
    for key in (
        "postgresql_user_rows",
        "projection_queue_rows",
        "qdrant_points",
        "active_clients",
        "legacy_imports",
        "source_postgres_read_count",
        "provider_calls",
    ):
        if type(receipt.get(key)) is not int or receipt[key] != 0:
            raise EmptyRollbackError("empty_rollback_not_eligible")
    if receipt["receipt_sha256"] != eligibility_receipt_sha256(receipt):
        raise EmptyRollbackError("empty_rollback_eligibility_digest_invalid")
    return receipt


@dataclass(frozen=True, slots=True)
class ExactRollbackResource:
    resource_key: str
    resource_kind: str
    resource_name: str
    resource_identity_sha256: str
    ownership_sha256: str

    def __post_init__(self) -> None:
        if (
            self.resource_key not in ROLLBACK_RESOURCE_KEYS
            or self.resource_kind not in RESOURCE_KINDS
            or _SAFE_NAME_RE.fullmatch(self.resource_name) is None
            or not _is_hash(self.resource_identity_sha256)
            or not _is_hash(self.ownership_sha256)
            or "//" in self.resource_name
            or any(
                part in {".", ".."}
                for part in self.resource_name.split("/")
            )
        ):
            raise EmptyRollbackError("empty_rollback_resource_invalid")

    def as_dict(self) -> dict[str, str]:
        return {
            "ownership_sha256": self.ownership_sha256,
            "resource_identity_sha256": self.resource_identity_sha256,
            "resource_key": self.resource_key,
            "resource_kind": self.resource_kind,
            "resource_name": self.resource_name,
        }


def expected_rollback_resource_names(
    package_manifest_sha256: str,
) -> dict[str, tuple[str, str]]:
    if not _is_hash(package_manifest_sha256):
        raise EmptyRollbackError("empty_rollback_package_hash_invalid")
    return {
        "stores_supervisor": (
            "systemd_unit",
            "/etc/systemd/system/governed-memory-stores-v5.service",
        ),
        "qdrant_alias": ("qdrant_alias", "governed_memory_active"),
        "qdrant_collection": (
            "qdrant_collection",
            "governed_memory_9a54cf123493_000005",
        ),
        "migration_0004": ("migration", "0004_pilot_marker"),
        "migration_0003": ("migration", "0003_owner_claim_detail"),
        "migration_0001": ("migration", "0001_foundation"),
        "canonical_database_and_roles": ("database", "governed_memory"),
        "qdrant_container": (
            "container",
            "governed-memory-qdrant-9a54cf123493-000005",
        ),
        "postgres_container": (
            "container",
            "governed-memory-postgres-9a54cf123493-000005",
        ),
        "qdrant_volume": (
            "volume",
            "governed-memory-qdrant-data-9a54cf123493-000005",
        ),
        "postgres_volume": (
            "volume",
            "governed-memory-postgres-data-9a54cf123493-000005",
        ),
        "network": ("network", "governed-memory-net-9a54cf123493-000005"),
        "qdrant_store_secret": (
            "secret_file",
            "/etc/governed-memory-stores/9a54cf123493-000005/qdrant.env",
        ),
        "postgres_store_secret": (
            "secret_file",
            "/etc/governed-memory-stores/9a54cf123493-000005/postgres.env",
        ),
        "resolved_store_spec": (
            "resolved_store_spec",
            "/etc/governed-memory-controller/store_spec-v5.json",
        ),
    }


@dataclass(frozen=True, slots=True)
class VerifiedRollbackResourceEvidence:
    package_manifest_sha256: str
    resource_ledger_binding_sha256: str
    resource_ledger_head_sha256: str
    resource_ledger_sequence: int
    exact_targets_sha256: str


_VERIFIED_RESOURCE_TOKEN = object()


class _VerifiedExactRollbackResources:
    __slots__ = ("_evidence", "_resources", "_token")

    def __init__(
        self,
        evidence: VerifiedRollbackResourceEvidence,
        resources: tuple[ExactRollbackResource, ...],
        token: object,
    ) -> None:
        if token is not _VERIFIED_RESOURCE_TOKEN:
            raise EmptyRollbackError("empty_rollback_resource_provenance_invalid")
        self._evidence = evidence
        self._resources = resources
        self._token = token

    def __repr__(self) -> str:
        return "VerifiedExactRollbackResources(<content-redacted>)"


def verified_rollback_resource_parts(
    value: object,
) -> tuple[VerifiedRollbackResourceEvidence, tuple[ExactRollbackResource, ...]]:
    if (
        type(value) is not _VerifiedExactRollbackResources
        or value._token is not _VERIFIED_RESOURCE_TOKEN
        or type(value._evidence) is not VerifiedRollbackResourceEvidence
        or type(value._resources) is not tuple
    ):
        raise EmptyRollbackError("empty_rollback_resource_provenance_invalid")
    return value._evidence, value._resources


def _resource_identity_sha256(record: ResourceIdentityRecord) -> str:
    return _sha(
        {
            "schema_version": "governed-memory-rollback-resource-identity-v2",
            "binding_sha256": record.binding_sha256,
            "resource_kind": record.resource_kind,
            "resource_name": record.resource_name,
            "resource_id": record.resource_id,
            "image_id": record.image_id,
            "image_repo_digest": record.image_repo_digest,
            "ownership_sha256": record.ownership_sha256,
            "resource_labels_sha256": record.resource_labels_sha256,
        }
    )


def derive_exact_rollback_resources_from_ledger(
    records: Iterable[ResourceIdentityRecord],
    *,
    package_manifest_sha256: str,
    expected_ledger_head_sha256: str,
) -> object:
    """Mint exact rollback targets only from one validated install ledger chain."""

    if (
        isinstance(records, (str, bytes, Mapping))
        or not _is_hash(package_manifest_sha256)
        or not _is_hash(expected_ledger_head_sha256)
    ):
        raise EmptyRollbackError("empty_rollback_resource_ledger_invalid")
    snapshot = tuple(records)
    if not snapshot or any(type(record) is not ResourceIdentityRecord for record in snapshot):
        raise EmptyRollbackError("empty_rollback_resource_ledger_invalid")
    try:
        raw = b"".join(
            canonical_json_bytes(record.as_dict()) + b"\n" for record in snapshot
        )
        parsed = parse_ledger_bytes(
            raw,
            expected_binding_sha256=snapshot[0].binding_sha256,
        )
    except ResourceIdentityError as error:
        raise EmptyRollbackError("empty_rollback_resource_ledger_invalid") from error
    if (
        parsed != snapshot
        or parsed[-1].entry_sha256 != expected_ledger_head_sha256
        or any(record.binding_sha256 != parsed[0].binding_sha256 for record in parsed)
    ):
        raise EmptyRollbackError("empty_rollback_resource_ledger_mismatch")

    expected = expected_rollback_resource_names(package_manifest_sha256)
    expected_identities = set(expected.values())
    latest: dict[tuple[str, str], ResourceIdentityRecord] = {}
    for record in parsed:
        identity = (record.resource_kind, record.resource_name)
        if identity not in expected_identities and record.resource_kind != "image":
            raise EmptyRollbackError("empty_rollback_unexpected_install_resource")
        latest[identity] = record

    exact: list[ExactRollbackResource] = []
    for key in ROLLBACK_RESOURCE_KEYS:
        identity = expected[key]
        record = latest.get(identity)
        if record is None or record.event == "removed":
            raise EmptyRollbackError("empty_rollback_resource_ledger_incomplete")
        exact.append(
            ExactRollbackResource(
                resource_key=key,
                resource_kind=record.resource_kind,
                resource_name=record.resource_name,
                resource_identity_sha256=_resource_identity_sha256(record),
                ownership_sha256=record.ownership_sha256,
            )
        )
    normalized = _validate_exact_resources(
        exact, package_manifest_sha256=package_manifest_sha256
    )
    exact_sha = _sha([resource.as_dict() for resource in normalized])
    evidence = VerifiedRollbackResourceEvidence(
        package_manifest_sha256=package_manifest_sha256,
        resource_ledger_binding_sha256=parsed[0].binding_sha256,
        resource_ledger_head_sha256=expected_ledger_head_sha256,
        resource_ledger_sequence=len(parsed),
        exact_targets_sha256=exact_sha,
    )
    return _VerifiedExactRollbackResources(
        evidence, normalized, _VERIFIED_RESOURCE_TOKEN
    )


def exact_rollback_targets_sha256(
    resources: object,
    *,
    package_manifest_sha256: str,
) -> str:
    evidence, normalized = verified_rollback_resource_parts(resources)
    if evidence.package_manifest_sha256 != package_manifest_sha256:
        raise EmptyRollbackError("empty_rollback_resource_package_mismatch")
    calculated = _sha([resource.as_dict() for resource in normalized])
    if calculated != evidence.exact_targets_sha256:
        raise EmptyRollbackError("empty_rollback_resource_provenance_mismatch")
    return calculated


def _validate_exact_resources(
    resources: Iterable[ExactRollbackResource],
    *,
    package_manifest_sha256: str,
) -> tuple[ExactRollbackResource, ...]:
    if isinstance(resources, (str, bytes, Mapping)):
        raise EmptyRollbackError("empty_rollback_resources_invalid")
    normalized = tuple(resources)
    if len(normalized) != len(ROLLBACK_RESOURCE_KEYS) or any(
        type(resource) is not ExactRollbackResource for resource in normalized
    ):
        raise EmptyRollbackError("empty_rollback_resources_incomplete")
    if tuple(resource.resource_key for resource in normalized) != ROLLBACK_RESOURCE_KEYS:
        raise EmptyRollbackError("empty_rollback_resource_order_invalid")
    expected = expected_rollback_resource_names(package_manifest_sha256)
    for resource in normalized:
        if (resource.resource_kind, resource.resource_name) != expected[
            resource.resource_key
        ]:
            raise EmptyRollbackError("empty_rollback_resource_target_mismatch")
    return normalized


@dataclass(frozen=True, slots=True)
class EmptyRollbackPlanStep:
    step_id: str
    operation: str
    resource_key: str | None
    invariant_only: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "resource_key": self.resource_key,
            "step_id": self.step_id,
            "invariant_only": self.invariant_only,
        }


EMPTY_ROLLBACK_STEPS: Final = (
    EmptyRollbackPlanStep("R01_REVERIFY_GLOBAL_LOCK", "reverify_global_lock", None),
    EmptyRollbackPlanStep("R02_VERIFY_CLAIMED_ROLLBACK_AUTHORITY", "verify_claimed_rollback_authority", None),
    EmptyRollbackPlanStep("R03_VERIFY_INSTALL_RECEIPT_AND_LEDGER", "verify_install_receipt_and_ledger", None),
    EmptyRollbackPlanStep("R04_ACQUIRE_ROLLBACK_CONTROLLER_AUTHORITY_MARKER", "acquire_rollback_controller_authority_marker", None),
    EmptyRollbackPlanStep("R05_DISABLE_AND_REMOVE_STORES_SUPERVISOR", "disable_and_remove_stores_supervisor", "stores_supervisor"),
    EmptyRollbackPlanStep("R06_ESTABLISH_ADMINISTRATIVE_WRITER_FENCE_AND_RECHECK_SEMANTIC_EMPTY", "establish_administrative_writer_fence_and_recheck_semantic_empty", None),
    EmptyRollbackPlanStep("R07_STOP_EXACT_STORES", "stop_exact_stores", None),
    EmptyRollbackPlanStep("R08_REMOVE_EXACT_QDRANT_CONTAINER", "remove_exact_qdrant_container", "qdrant_container"),
    EmptyRollbackPlanStep("R09_REMOVE_EXACT_POSTGRES_CONTAINER", "remove_exact_postgres_container", "postgres_container"),
    EmptyRollbackPlanStep("R10_REMOVE_EXACT_EMPTY_QDRANT_VOLUME", "remove_exact_empty_qdrant_volume", "qdrant_volume"),
    EmptyRollbackPlanStep("R11_REMOVE_EXACT_EMPTY_POSTGRES_VOLUME", "remove_exact_empty_postgres_volume", "postgres_volume"),
    EmptyRollbackPlanStep("R12_REMOVE_EXACT_UNUSED_NETWORK", "remove_exact_unused_network", "network"),
    EmptyRollbackPlanStep("R13_REMOVE_FRESH_QDRANT_STORE_SECRET", "remove_fresh_qdrant_store_secret", "qdrant_store_secret"),
    EmptyRollbackPlanStep("R14_REMOVE_FRESH_POSTGRES_STORE_SECRET", "remove_fresh_postgres_store_secret", "postgres_store_secret"),
    EmptyRollbackPlanStep("R15_REMOVE_RESOLVED_STORE_SPEC", "remove_resolved_store_spec", "resolved_store_spec"),
    EmptyRollbackPlanStep("R16_VERIFY_QDRANT_ALIAS_PHYSICALLY_ABSENT", "verify_qdrant_alias_physically_absent", "qdrant_alias", True),
    EmptyRollbackPlanStep("R17_VERIFY_QDRANT_COLLECTION_PHYSICALLY_ABSENT", "verify_qdrant_collection_physically_absent", "qdrant_collection", True),
    EmptyRollbackPlanStep("R18_VERIFY_PILOT_MARKER_0004_PHYSICALLY_ABSENT", "verify_pilot_marker_0004_physically_absent", "migration_0004", True),
    EmptyRollbackPlanStep("R19_VERIFY_OWNER_CLAIM_DETAIL_0003_PHYSICALLY_ABSENT", "verify_owner_claim_detail_0003_physically_absent", "migration_0003", True),
    EmptyRollbackPlanStep("R20_VERIFY_FOUNDATION_0001_PHYSICALLY_ABSENT", "verify_foundation_0001_physically_absent", "migration_0001", True),
    EmptyRollbackPlanStep("R21_VERIFY_CANONICAL_DATABASE_AND_ROLES_PHYSICALLY_ABSENT", "verify_canonical_database_and_roles_physically_absent", "canonical_database_and_roles", True),
    EmptyRollbackPlanStep("R22_VERIFY_EXACT_ABSENCE_AND_RETAIN_AUDIT", "verify_exact_absence_and_retain_audit", None),
)


@dataclass(frozen=True, slots=True)
class EmptyRollbackPlan:
    candidate_git_commit: str
    candidate_git_tree: str
    package_manifest_sha256: str
    installation_execution_id: str
    installation_receipt_sha256: str
    eligibility_receipt_sha256: str
    resource_ledger_head_sha256: str
    exact_targets_sha256: str
    authorization_sha256: str
    resources: tuple[ExactRollbackResource, ...]
    steps: tuple[EmptyRollbackPlanStep, ...]
    retained_audit_keys: tuple[str, ...]
    plan_sha256: str

    def hash_input_dict(self) -> dict[str, object]:
        return {
            "schema_version": PLAN_SCHEMA,
            "operation": ROLLBACK_OPERATION,
            "candidate_git_commit": self.candidate_git_commit,
            "candidate_git_tree": self.candidate_git_tree,
            "package_manifest_sha256": self.package_manifest_sha256,
            "installation_execution_id": self.installation_execution_id,
            "installation_receipt_sha256": self.installation_receipt_sha256,
            "eligibility_receipt_sha256": self.eligibility_receipt_sha256,
            "resource_ledger_head_sha256": self.resource_ledger_head_sha256,
            "exact_targets_sha256": self.exact_targets_sha256,
            "steps": [step.as_dict() for step in self.steps],
            "retained_audit_keys": list(self.retained_audit_keys),
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self.hash_input_dict(),
            "authorization_sha256": self.authorization_sha256,
            "plan_sha256": self.plan_sha256,
        }


def _plan_hash_input(
    eligibility: Mapping[str, object], exact_targets_sha256: str
) -> dict[str, object]:
    return {
        "schema_version": PLAN_SCHEMA,
        "operation": ROLLBACK_OPERATION,
        "candidate_git_commit": eligibility["candidate_git_commit"],
        "candidate_git_tree": eligibility["candidate_git_tree"],
        "package_manifest_sha256": eligibility["package_manifest_sha256"],
        "installation_execution_id": eligibility["installation_execution_id"],
        "installation_receipt_sha256": eligibility[
            "installation_receipt_sha256"
        ],
        "eligibility_receipt_sha256": eligibility["receipt_sha256"],
        "resource_ledger_head_sha256": eligibility[
            "resource_ledger_head_sha256"
        ],
        "exact_targets_sha256": exact_targets_sha256,
        "steps": [step.as_dict() for step in EMPTY_ROLLBACK_STEPS],
        "retained_audit_keys": list(RETAINED_AUDIT_KEYS),
    }


def empty_rollback_plan_sha256(
    eligibility_receipt: Mapping[str, object],
    resources: object,
) -> str:
    eligibility = verify_empty_rollback_eligibility_receipt(eligibility_receipt)
    package_sha = eligibility["package_manifest_sha256"]
    assert isinstance(package_sha, str)
    targets_sha = exact_rollback_targets_sha256(
        resources, package_manifest_sha256=package_sha
    )
    return _sha(_plan_hash_input(eligibility, targets_sha))


def build_empty_rollback_plan(
    capability: object,
    eligibility_receipt: Mapping[str, object],
    resources: object,
) -> EmptyRollbackPlan:
    evidence = rollback_capability_evidence(capability)
    eligibility = verify_empty_rollback_eligibility_receipt(eligibility_receipt)
    package_sha = eligibility["package_manifest_sha256"]
    assert isinstance(package_sha, str)
    resource_evidence, normalized_resources = verified_rollback_resource_parts(
        resources
    )
    if (
        resource_evidence.package_manifest_sha256 != package_sha
        or resource_evidence.resource_ledger_head_sha256
        != eligibility["resource_ledger_head_sha256"]
        or resource_evidence.exact_targets_sha256
        != eligibility["exact_targets_sha256"]
    ):
        raise EmptyRollbackError("empty_rollback_resource_provenance_mismatch")
    exact_targets_sha = resource_evidence.exact_targets_sha256
    eligibility_sha = eligibility["receipt_sha256"]
    assert isinstance(eligibility_sha, str)
    bindings = {
        "candidate_git_commit": eligibility["candidate_git_commit"],
        "candidate_git_tree": eligibility["candidate_git_tree"],
        "package_manifest_sha256": package_sha,
        "installation_execution_id": eligibility["installation_execution_id"],
        "installation_receipt_sha256": eligibility[
            "installation_receipt_sha256"
        ],
        "eligibility_receipt_sha256": eligibility_sha,
        "resource_ledger_head_sha256": eligibility[
            "resource_ledger_head_sha256"
        ],
        "exact_targets_sha256": exact_targets_sha,
    }
    if (
        evidence.operation != ROLLBACK_OPERATION
        or any(getattr(evidence, key) != value for key, value in bindings.items())
    ):
        raise EmptyRollbackError("empty_rollback_authority_binding_mismatch")
    document = _plan_hash_input(eligibility, exact_targets_sha)
    plan_sha = _sha(document)
    if evidence.rollback_plan_sha256 != plan_sha:
        raise EmptyRollbackError("empty_rollback_plan_binding_mismatch")
    return EmptyRollbackPlan(
        candidate_git_commit=str(document["candidate_git_commit"]),
        candidate_git_tree=str(document["candidate_git_tree"]),
        package_manifest_sha256=package_sha,
        installation_execution_id=str(document["installation_execution_id"]),
        installation_receipt_sha256=str(
            document["installation_receipt_sha256"]
        ),
        eligibility_receipt_sha256=eligibility_sha,
        resource_ledger_head_sha256=str(
            document["resource_ledger_head_sha256"]
        ),
        exact_targets_sha256=exact_targets_sha,
        authorization_sha256=evidence.authorization_sha256,
        resources=normalized_resources,
        steps=EMPTY_ROLLBACK_STEPS,
        retained_audit_keys=RETAINED_AUDIT_KEYS,
        plan_sha256=plan_sha,
    )


def verify_built_empty_rollback_plan(plan: object) -> EmptyRollbackPlan:
    """Revalidate one already-built immutable plan without reminting authority."""

    if type(plan) is not EmptyRollbackPlan:
        raise EmptyRollbackError("empty_rollback_plan_invalid")
    normalized = _validate_exact_resources(
        plan.resources,
        package_manifest_sha256=plan.package_manifest_sha256,
    )
    targets_sha = _sha([resource.as_dict() for resource in normalized])
    if (
        normalized != plan.resources
        or targets_sha != plan.exact_targets_sha256
        or _sha(plan.hash_input_dict()) != plan.plan_sha256
        or plan.steps != EMPTY_ROLLBACK_STEPS
        or plan.retained_audit_keys != RETAINED_AUDIT_KEYS
    ):
        raise EmptyRollbackError("empty_rollback_plan_integrity_invalid")
    return plan


__all__ = [
    "ELIGIBILITY_SCHEMA",
    "EMPTY_ROLLBACK_STEPS",
    "EmptyRollbackError",
    "EmptyRollbackPlan",
    "EmptyRollbackPlanStep",
    "ExactRollbackResource",
    "PLAN_SCHEMA",
    "RETAINED_AUDIT_KEYS",
    "ROLLBACK_OPERATION",
    "ROLLBACK_RESOURCE_KEYS",
    "build_empty_rollback_eligibility_receipt",
    "build_empty_rollback_plan",
    "canonical_json_bytes",
    "derive_exact_rollback_resources_from_ledger",
    "empty_rollback_plan_sha256",
    "eligibility_receipt_sha256",
    "exact_rollback_targets_sha256",
    "expected_rollback_resource_names",
    "verified_rollback_resource_parts",
    "verify_built_empty_rollback_plan",
    "verify_empty_rollback_eligibility_receipt",
    "VerifiedRollbackResourceEvidence",
]
