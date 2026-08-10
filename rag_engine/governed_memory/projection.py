from __future__ import annotations

"""Pure construction of the single derived Qdrant projection."""

from datetime import datetime
from enum import Enum
import math
import struct
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from uuid import UUID

from .admission import recompute_claim_state_sha256, relational_fact_from_row
from .contracts import (
    ClaimLifecycleState,
    ContractViolation,
    EpistemicStatus,
    ProjectionState,
    Sensitivity,
    canonical_sha256,
    framed_sha256,
    render_relational_fact,
    require_exact_int,
    require_key,
    require_sha256,
    require_utc,
    sha256_hex,
    sha256_text,
    utc_text,
)
from .extraction import (
    CANONICAL_PREDICATE_CATALOG_SHA256,
    policy_for_sensitivity,
)


EMBEDDING_MODEL = "text-embedding-3-large"
DEFAULT_DIMENSIONS = 3_072
NUMERIC_ENCODING = "float32_le"
NORMALIZATION = "l2"
DISTANCE = "dot"
RELATIONAL_RENDERER_SHA256 = (
    "f77b782b3e549b30a46b4beb7e25b248018f6c0f3597b36d0020103570e66442"
)
QDRANT_PAYLOAD_FIELDS = (
    "claim_id",
    "dimensions",
    "domains",
    "embedding_input_sha256",
    "embedding_model",
    "epistemic_state",
    "intents",
    "is_current",
    "lifecycle_state",
    "owner_user_id",
    "predicate",
    "predicate_catalog_sha256",
    "projectable",
    "projection_contract_sha256",
    "projection_manifest_sha256",
    "projection_operation_id",
    "projection_sequence",
    "renderer_sha256",
    "requires_explicit",
    "retrieval_text_sha256",
    "revision_id",
    "revision_number",
    "revision_sha256",
    "selection_binding_sha256",
    "sensitivity",
    "surface",
    "updated_at",
    "valid_from",
    "valid_to",
    "vector_sha256",
)
PROJECTION_CONTRACT_SHA256 = (
    "9a54cf123493a25646b29cadf2be068039ac7062ed6d58e69e8b6e7a58dfe59a"
)
_RENDERER_VECTOR_TEXT = (
    '{"object":{"kind":"literal","literal":"café:\\n猫"},'
    '"predicate":"preference.personal","subject":{"display_name":null,'
    '"entity_key":"self","entity_type":"self"}}'
)
PROJECTION_TEST_VECTORS = MappingProxyType(
    {
        "renderer": MappingProxyType(
            {
                "contract_domain": "governed_memory.relational_renderer_contract.v1",
                "contract_ordered_fields": (
                    ("name", "canonical_relational_fact"),
                    ("encoding", "utf-8"),
                    ("normalization", "NFC"),
                    ("format", "canonical_json_sort_keys_compact_utf8"),
                    ("fields", "[object,predicate,subject]"),
                ),
                "subject": MappingProxyType(
                    {
                        "display_name": None,
                        "entity_key": "self",
                        "entity_type": "self",
                    }
                ),
                "predicate": "preference.personal",
                "object": MappingProxyType(
                    {"kind": "literal", "literal": "café:\n猫"}
                ),
                "rendered_utf8": _RENDERER_VECTOR_TEXT.encode("utf-8"),
                "rendered_sha256": (
                    "404e70a184945366051bd89614ea852e8530f46141f552ae2ff9a897e654004e"
                ),
                "renderer_sha256": RELATIONAL_RENDERER_SHA256,
            }
        ),
        "projection_contract": MappingProxyType(
            {
                "domain": "governed_memory.projection_contract.v1",
                "ordered_fields": (
                    ("embedding_model", EMBEDDING_MODEL),
                    ("dimensions", "3072"),
                    ("numeric_encoding", NUMERIC_ENCODING),
                    ("normalization", NORMALIZATION),
                    ("distance", DISTANCE),
                    ("renderer_sha256", RELATIONAL_RENDERER_SHA256),
                    (
                        "qdrant_payload_fields",
                        "[" + ",".join(QDRANT_PAYLOAD_FIELDS) + "]",
                    ),
                ),
                "sha256": PROJECTION_CONTRACT_SHA256,
            }
        ),
        "projection_operation": MappingProxyType(
            {
                "domain": "governed_memory.projection_operation.v1",
                "ordered_fields": (
                    ("projection_contract_sha256", PROJECTION_CONTRACT_SHA256),
                    ("owner_user_id", "00000000-0000-0000-0000-000000000001"),
                    ("claim_id", "00000000-0000-0000-0000-000000000006"),
                    ("revision_id", "00000000-0000-0000-0000-000000000007"),
                    ("operation_id", "00000000-0000-0000-0000-000000000008"),
                    ("operation", "upsert"),
                    ("sequence_number", "3"),
                    ("revision_sha256", "5" * 64),
                    ("selection_binding_sha256", "6" * 64),
                    (
                        "retrieval_text_sha256",
                        "404e70a184945366051bd89614ea852e8530f46141f552ae2ff9a897e654004e",
                    ),
                    (
                        "embedding_input_sha256",
                        "404e70a184945366051bd89614ea852e8530f46141f552ae2ff9a897e654004e",
                    ),
                ),
                "sha256": (
                    "bd26e95f74ddaf4dc6fc3c06da17851e65f50a86629531db56ecd70b5ef885be"
                ),
            }
        ),
    }
)


class ProjectionOperation(str, Enum):
    UPSERT = "upsert"
    DELETE = "delete"


def _uuid(value: object, code: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ContractViolation(code) from exc


def _utc_text_value(value: object, code: str) -> str:
    if isinstance(value, datetime):
        return utc_text(value)
    if not isinstance(value, str):
        raise ContractViolation(code)
    try:
        return utc_text(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as exc:
        raise ContractViolation(code) from exc


def _sorted_keys(value: object, code: str) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ContractViolation(code)
    result = [require_key(item, code) for item in value]
    if result != sorted(set(result)):
        raise ContractViolation(code)
    return result


def _float32(value: float) -> float:
    try:
        return struct.unpack("<f", struct.pack("<f", value))[0]
    except (OverflowError, struct.error) as exc:
        raise ContractViolation("embedding_float32_overflow") from exc


def normalize_embedding(
    values: Sequence[float | int], dimensions: int = DEFAULT_DIMENSIONS
) -> tuple[float, ...]:
    expected = require_exact_int(
        dimensions,
        code="invalid_embedding_dimensions",
        minimum=2,
        maximum=65_536,
    )
    if expected != DEFAULT_DIMENSIONS:
        raise ContractViolation("projection_dimensions_contract_mismatch")
    if isinstance(values, (str, bytes, bytearray)):
        raise ContractViolation("invalid_embedding_sequence")
    try:
        raw = tuple(values)
    except TypeError as exc:
        raise ContractViolation("invalid_embedding_sequence") from exc
    if len(raw) != expected:
        raise ContractViolation("embedding_dimension_mismatch")
    converted: list[float] = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ContractViolation("invalid_embedding_value")
        try:
            numeric = float(value)
        except (OverflowError, ValueError) as exc:
            raise ContractViolation("invalid_embedding_value") from exc
        if not math.isfinite(numeric):
            raise ContractViolation("nonfinite_embedding_value")
        converted.append(_float32(numeric))
    magnitude = math.sqrt(math.fsum(value * value for value in converted))
    if not math.isfinite(magnitude) or magnitude == 0.0:
        raise ContractViolation("zero_embedding")
    normalized = tuple(_float32(value / magnitude) for value in converted)
    normalized_magnitude = math.sqrt(
        math.fsum(value * value for value in normalized)
    )
    if abs(normalized_magnitude - 1.0) > 1e-7:
        normalized = tuple(
            _float32(value / normalized_magnitude) for value in normalized
        )
    if abs(math.sqrt(math.fsum(v * v for v in normalized)) - 1.0) > 1e-7:
        raise ContractViolation("embedding_normalization_unstable")
    return normalized


def vector_sha256(vector: Sequence[float]) -> str:
    try:
        material = b"".join(struct.pack("<f", value) for value in vector)
    except (OverflowError, struct.error, TypeError) as exc:
        raise ContractViolation("invalid_vector_hash_input") from exc
    return sha256_hex(material)


def recompute_projection_contract_sha256() -> str:
    return framed_sha256(
        "governed_memory.projection_contract.v1",
        PROJECTION_TEST_VECTORS["projection_contract"]["ordered_fields"],
    )


def recompute_relational_renderer_sha256() -> str:
    return framed_sha256(
        PROJECTION_TEST_VECTORS["renderer"]["contract_domain"],
        PROJECTION_TEST_VECTORS["renderer"]["contract_ordered_fields"],
    )


def render_projection_surface(claim: Mapping[str, Any]) -> str:
    if not isinstance(claim, Mapping):
        raise ContractViolation("invalid_projection_claim")
    subject, object_value = relational_fact_from_row(claim)
    rendered = render_relational_fact(
        subject=subject,
        predicate=require_key(claim.get("predicate"), "invalid_projection_predicate"),
        object_value=object_value,
    )
    supplied_text = claim.get("retrieval_text")
    if supplied_text != rendered:
        raise ContractViolation("projection_retrieval_text_mismatch")
    digest = sha256_text(rendered)
    if claim.get("retrieval_text_sha256") != digest:
        raise ContractViolation("projection_retrieval_text_sha256_mismatch")
    return rendered


def projection_manifest_sha256(
    *,
    owner_user_id: UUID,
    claim_id: UUID,
    revision_id: UUID,
    operation_id: UUID,
    operation: ProjectionOperation | str,
    sequence_number: int,
    revision_sha256: str,
    selection_binding_sha256: str,
    retrieval_text_sha256: str,
    embedding_input_sha256: str,
) -> str:
    for value, code in (
        (owner_user_id, "invalid_projection_owner"),
        (claim_id, "invalid_projection_claim_id"),
        (revision_id, "invalid_projection_revision_id"),
        (operation_id, "invalid_projection_operation_id"),
    ):
        if not isinstance(value, UUID):
            raise ContractViolation(code)
    try:
        checked_operation = ProjectionOperation(operation)
    except (TypeError, ValueError) as exc:
        raise ContractViolation("invalid_projection_operation") from exc
    sequence = require_exact_int(
        sequence_number,
        code="invalid_projection_sequence",
        minimum=1,
    )
    revision_hash = require_sha256(
        revision_sha256, "invalid_projection_revision_sha256"
    )
    selection_hash = require_sha256(
        selection_binding_sha256, "invalid_projection_selection_binding_sha256"
    )
    retrieval_hash = require_sha256(
        retrieval_text_sha256, "invalid_projection_retrieval_text_sha256"
    )
    embedding_hash = require_sha256(
        embedding_input_sha256, "invalid_projection_embedding_input_sha256"
    )
    if checked_operation is ProjectionOperation.UPSERT and embedding_hash != retrieval_hash:
        raise ContractViolation("projection_embedding_input_binding_mismatch")
    return framed_sha256(
        "governed_memory.projection_operation.v1",
        (
            ("projection_contract_sha256", PROJECTION_CONTRACT_SHA256),
            ("owner_user_id", str(owner_user_id)),
            ("claim_id", str(claim_id)),
            ("revision_id", str(revision_id)),
            ("operation_id", str(operation_id)),
            ("operation", checked_operation.value),
            ("sequence_number", str(sequence)),
            ("revision_sha256", revision_hash),
            ("selection_binding_sha256", selection_hash),
            ("retrieval_text_sha256", retrieval_hash),
            ("embedding_input_sha256", embedding_hash),
        ),
    )


PROJECTION_OUTBOX_FIELDS = (
    "owner_user_id",
    "claim_id",
    "revision_id",
    "operation_id",
    "operation",
    "sequence_number",
    "revision_sha256",
    "selection_binding_sha256",
    "retrieval_text_sha256",
    "embedding_input_sha256",
    "projection_contract_sha256",
    "projection_manifest_sha256",
    "state",
)
_OUTBOX_FIELDS = frozenset(PROJECTION_OUTBOX_FIELDS)


def _build_projection_point(
    claim: Mapping[str, Any],
    outbox_record: Mapping[str, Any],
    vector: Sequence[float | int],
    *,
    required_outbox_state: str,
) -> dict[str, object]:
    if not isinstance(claim, Mapping) or not isinstance(outbox_record, Mapping):
        raise ContractViolation("invalid_projection_input")
    if (
        set(outbox_record) != _OUTBOX_FIELDS
        or outbox_record["state"] != required_outbox_state
    ):
        raise ContractViolation("invalid_projection_outbox_record")
    if claim.get("state_sha256") != recompute_claim_state_sha256(claim):
        raise ContractViolation("projection_claim_state_sha256_mismatch")
    try:
        lifecycle = ClaimLifecycleState(claim["lifecycle_state"])
        epistemic = EpistemicStatus(claim["epistemic_state"])
        sensitivity = Sensitivity(claim["sensitivity"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractViolation("invalid_projection_claim_authority") from exc
    if (
        lifecycle is not ClaimLifecycleState.ACTIVE
        or claim.get("is_current") is not True
        or claim.get("projectable") is not True
    ):
        raise ContractViolation("projection_claim_not_projectable")
    policy = policy_for_sensitivity(sensitivity.value)
    if (
        claim.get("surface") != policy["surface"]
        or claim.get("requires_explicit") is not policy["requires_explicit"]
        or claim.get("domains") != []
        or claim.get("intents") != []
    ):
        raise ContractViolation("projection_policy_mismatch")
    if claim.get("predicate_catalog_sha256") != CANONICAL_PREDICATE_CATALOG_SHA256:
        raise ContractViolation("projection_catalog_mismatch")
    rendered = render_projection_surface(claim)
    embedding_input_sha256 = sha256_text(rendered)
    if recompute_relational_renderer_sha256() != RELATIONAL_RENDERER_SHA256:
        raise ContractViolation("renderer_contract_constant_mismatch")
    if recompute_projection_contract_sha256() != PROJECTION_CONTRACT_SHA256:
        raise ContractViolation("projection_contract_constant_mismatch")
    owner = _uuid(claim.get("owner_user_id"), "invalid_projection_owner")
    claim_id = _uuid(claim.get("claim_id"), "invalid_projection_claim_id")
    revision_id = _uuid(claim.get("revision_id"), "invalid_projection_revision_id")
    operation_id = _uuid(
        outbox_record["operation_id"], "invalid_projection_operation_id"
    )
    sequence_number = require_exact_int(
        outbox_record["sequence_number"],
        code="invalid_projection_sequence",
        minimum=1,
    )
    claim_sequence = require_exact_int(
        claim.get("projection_sequence"),
        code="invalid_claim_projection_sequence",
        minimum=1,
    )
    if sequence_number != claim_sequence:
        raise ContractViolation("projection_outbox_sequence_mismatch")
    for field, expected in (
        ("owner_user_id", str(owner)),
        ("claim_id", str(claim_id)),
        ("revision_id", str(revision_id)),
        ("revision_sha256", claim.get("revision_sha256")),
        ("selection_binding_sha256", claim.get("selection_binding_sha256")),
        ("retrieval_text_sha256", claim.get("retrieval_text_sha256")),
        ("embedding_input_sha256", embedding_input_sha256),
        ("projection_contract_sha256", PROJECTION_CONTRACT_SHA256),
    ):
        if outbox_record[field] != expected:
            raise ContractViolation("projection_outbox_claim_binding_mismatch")
    if outbox_record["operation"] != ProjectionOperation.UPSERT.value:
        raise ContractViolation("projection_point_requires_upsert")
    expected_manifest_sha256 = projection_manifest_sha256(
        owner_user_id=owner,
        claim_id=claim_id,
        revision_id=revision_id,
        operation_id=operation_id,
        operation=ProjectionOperation.UPSERT,
        sequence_number=sequence_number,
        revision_sha256=str(claim["revision_sha256"]),
        selection_binding_sha256=str(claim["selection_binding_sha256"]),
        retrieval_text_sha256=str(claim["retrieval_text_sha256"]),
        embedding_input_sha256=embedding_input_sha256,
    )
    if outbox_record["projection_manifest_sha256"] != expected_manifest_sha256:
        raise ContractViolation("projection_manifest_sha256_mismatch")
    normalized = normalize_embedding(vector)
    vector_digest = vector_sha256(normalized)
    updated_at = _utc_text_value(
        claim.get("updated_at"), "invalid_projection_updated_at"
    )
    revision_number = require_exact_int(
        claim.get("revision_number"),
        code="invalid_projection_revision_number",
        minimum=1,
    )
    payload = {
        "claim_id": str(claim_id),
        "dimensions": DEFAULT_DIMENSIONS,
        "domains": _sorted_keys(claim.get("domains"), "invalid_projection_domains"),
        "embedding_input_sha256": embedding_input_sha256,
        "embedding_model": EMBEDDING_MODEL,
        "epistemic_state": epistemic.value,
        "intents": _sorted_keys(claim.get("intents"), "invalid_projection_intents"),
        "is_current": True,
        "lifecycle_state": lifecycle.value,
        "owner_user_id": str(owner),
        "predicate": require_key(claim.get("predicate"), "invalid_projection_predicate"),
        "predicate_catalog_sha256": CANONICAL_PREDICATE_CATALOG_SHA256,
        "projectable": True,
        "projection_contract_sha256": PROJECTION_CONTRACT_SHA256,
        "projection_manifest_sha256": expected_manifest_sha256,
        "projection_operation_id": str(operation_id),
        "projection_sequence": sequence_number,
        "renderer_sha256": RELATIONAL_RENDERER_SHA256,
        "requires_explicit": policy["requires_explicit"],
        "retrieval_text_sha256": embedding_input_sha256,
        "revision_id": str(revision_id),
        "revision_number": revision_number,
        "revision_sha256": require_sha256(
            claim.get("revision_sha256"), "invalid_projection_revision_sha256"
        ),
        "selection_binding_sha256": require_sha256(
            claim.get("selection_binding_sha256"),
            "invalid_projection_selection_binding_sha256",
        ),
        "sensitivity": sensitivity.value,
        "surface": str(policy["surface"]),
        "updated_at": updated_at,
        "valid_from": claim.get("valid_from"),
        "valid_to": claim.get("valid_to"),
        "vector_sha256": vector_digest,
    }
    if tuple(sorted(payload)) != QDRANT_PAYLOAD_FIELDS:
        raise ContractViolation("projection_payload_contract_mismatch")
    return {
        "point_id": str(claim_id),
        "vector": normalized,
        "payload": payload,
        "payload_sha256": canonical_sha256(
            "governed_memory.projection_payload", payload
        ),
    }


def build_projection_point(
    claim: Mapping[str, Any],
    outbox_record: Mapping[str, Any],
    vector: Sequence[float | int],
) -> dict[str, object]:
    return _build_projection_point(
        claim, outbox_record, vector, required_outbox_state="claimed"
    )


def build_rebuild_projection_point(
    claim: Mapping[str, Any],
    applied_outbox_record: Mapping[str, Any],
    vector: Sequence[float | int],
) -> dict[str, object]:
    return _build_projection_point(
        claim,
        applied_outbox_record,
        vector,
        required_outbox_state="applied",
    )


PROJECTION_DELETE_COMMAND_FIELDS = (
    "claim_id",
    "collection_alias",
    "embedding_input_sha256",
    "operation",
    "owner_user_id",
    "physical_collection",
    "point_id",
    "projection_contract_sha256",
    "projection_manifest_sha256",
    "projection_operation_id",
    "projection_sequence",
    "retrieval_text_sha256",
    "revision_id",
    "revision_sha256",
    "selection_binding_sha256",
)
PROJECTION_DELETE_RECEIPT_FIELDS = tuple(
    sorted(
        set(PROJECTION_DELETE_COMMAND_FIELDS)
        | {
            "absence_verification_sha256",
            "absence_verified_at",
            "applied_at",
            "receipt_sha256",
            "state",
        }
    )
)

QDRANT_ABSENCE_VERIFICATION_DOMAIN = (
    "governed_memory.qdrant_absence_verification.v1"
)


def qdrant_absence_verification_sha256(
    *,
    collection_alias: str,
    physical_collection: str,
    point_id: UUID,
    projection_sequence: int,
    alias_target_verified: bool,
    alias_absent: bool,
    physical_absent: bool,
) -> str:
    """Bind a delete receipt to exact alias and physical-point absence."""

    if (
        type(alias_target_verified) is not bool
        or type(alias_absent) is not bool
        or type(physical_absent) is not bool
        or not alias_target_verified
        or not alias_absent
        or not physical_absent
    ):
        raise ContractViolation("qdrant_absence_not_verified")
    point = _uuid(point_id, "invalid_absence_point_id")
    sequence = require_exact_int(
        projection_sequence,
        code="invalid_absence_projection_sequence",
        minimum=1,
    )
    return framed_sha256(
        QDRANT_ABSENCE_VERIFICATION_DOMAIN,
        (
            (
                "collection_alias",
                require_key(collection_alias, "invalid_absence_collection_alias"),
            ),
            (
                "physical_collection",
                require_key(
                    physical_collection,
                    "invalid_absence_physical_collection",
                ),
            ),
            ("point_id", str(point)),
            ("projection_sequence", str(sequence)),
            ("alias_target_verified", "true"),
            ("alias_absent", "true"),
            ("physical_absent", "true"),
        ),
    )


def build_projection_delete(
    outbox_record: Mapping[str, Any],
    *,
    collection_alias: str,
    physical_collection: str,
) -> dict[str, object]:
    if not isinstance(outbox_record, Mapping) or set(outbox_record) != _OUTBOX_FIELDS:
        raise ContractViolation("invalid_projection_delete_outbox")
    if (
        outbox_record["state"] != "claimed"
        or outbox_record["operation"] != ProjectionOperation.DELETE.value
    ):
        raise ContractViolation("projection_delete_outbox_not_claimed")
    owner = _uuid(outbox_record["owner_user_id"], "invalid_projection_delete_owner")
    claim_id = _uuid(outbox_record["claim_id"], "invalid_projection_delete_claim")
    revision_id = _uuid(
        outbox_record["revision_id"], "invalid_projection_delete_revision"
    )
    operation_id = _uuid(
        outbox_record["operation_id"], "invalid_projection_delete_operation"
    )
    sequence = require_exact_int(
        outbox_record["sequence_number"],
        code="invalid_projection_delete_sequence",
        minimum=1,
    )
    revision_sha256 = require_sha256(
        outbox_record["revision_sha256"],
        "invalid_projection_delete_revision_sha256",
    )
    selection_sha256 = require_sha256(
        outbox_record["selection_binding_sha256"],
        "invalid_projection_delete_selection_sha256",
    )
    retrieval_sha256 = require_sha256(
        outbox_record["retrieval_text_sha256"],
        "invalid_projection_delete_retrieval_sha256",
    )
    embedding_sha256 = require_sha256(
        outbox_record["embedding_input_sha256"],
        "invalid_projection_delete_embedding_sha256",
    )
    if embedding_sha256 != retrieval_sha256:
        raise ContractViolation("projection_delete_embedding_binding_mismatch")
    if outbox_record["projection_contract_sha256"] != PROJECTION_CONTRACT_SHA256:
        raise ContractViolation("projection_delete_contract_mismatch")
    expected_manifest = projection_manifest_sha256(
        owner_user_id=owner,
        claim_id=claim_id,
        revision_id=revision_id,
        operation_id=operation_id,
        operation=ProjectionOperation.DELETE,
        sequence_number=sequence,
        revision_sha256=revision_sha256,
        selection_binding_sha256=selection_sha256,
        retrieval_text_sha256=retrieval_sha256,
        embedding_input_sha256=embedding_sha256,
    )
    if outbox_record["projection_manifest_sha256"] != expected_manifest:
        raise ContractViolation("projection_delete_manifest_mismatch")
    command = {
        "claim_id": str(claim_id),
        "collection_alias": require_key(
            collection_alias, "invalid_projection_collection_alias"
        ),
        "embedding_input_sha256": embedding_sha256,
        "operation": ProjectionOperation.DELETE.value,
        "owner_user_id": str(owner),
        "physical_collection": require_key(
            physical_collection, "invalid_projection_physical_collection"
        ),
        "point_id": str(claim_id),
        "projection_contract_sha256": PROJECTION_CONTRACT_SHA256,
        "projection_manifest_sha256": expected_manifest,
        "projection_operation_id": str(operation_id),
        "projection_sequence": sequence,
        "retrieval_text_sha256": retrieval_sha256,
        "revision_id": str(revision_id),
        "revision_sha256": revision_sha256,
        "selection_binding_sha256": selection_sha256,
    }
    if tuple(sorted(command)) != PROJECTION_DELETE_COMMAND_FIELDS:
        raise ContractViolation("projection_delete_command_contract_mismatch")
    return command


def build_projection_delete_receipt(
    delete_command: Mapping[str, Any],
    *,
    applied_at: datetime,
    absence_verified_at: datetime,
    absence_verification_sha256: str,
) -> dict[str, object]:
    if (
        not isinstance(delete_command, Mapping)
        or tuple(sorted(delete_command)) != PROJECTION_DELETE_COMMAND_FIELDS
    ):
        raise ContractViolation("invalid_projection_delete_command")
    applied = require_utc(applied_at, "invalid_projection_delete_applied_at")
    verified = require_utc(
        absence_verified_at, "invalid_projection_delete_absence_verified_at"
    )
    if verified < applied:
        raise ContractViolation("projection_delete_absence_before_apply")
    material = {
        **dict(delete_command),
        "absence_verification_sha256": require_sha256(
            absence_verification_sha256,
            "invalid_projection_delete_absence_verification_sha256",
        ),
        "absence_verified_at": verified,
        "applied_at": applied,
        "state": "applied",
    }
    receipt = {
        **material,
        "receipt_sha256": canonical_sha256(
            "governed_memory.projection_delete_receipt", material
        ),
    }
    if tuple(sorted(receipt)) != PROJECTION_DELETE_RECEIPT_FIELDS:
        raise ContractViolation("projection_delete_receipt_contract_mismatch")
    return receipt


def supersede_stale_projection(
    outbox_record: Mapping[str, Any],
    *,
    authoritative_projection_sequence: int,
) -> dict[str, object]:
    """Terminalize lower-sequence work without treating it as applied or retryable."""

    if not isinstance(outbox_record, Mapping) or set(outbox_record) != _OUTBOX_FIELDS:
        raise ContractViolation("invalid_stale_projection_outbox")
    try:
        state = ProjectionState(outbox_record["state"])
        operation = ProjectionOperation(outbox_record["operation"])
    except (TypeError, ValueError) as exc:
        raise ContractViolation("invalid_stale_projection_state") from exc
    if state not in {
        ProjectionState.PENDING,
        ProjectionState.CLAIMED,
        ProjectionState.RETRYABLE,
    }:
        raise ContractViolation("projection_state_not_supersedable")
    outbox_sequence = require_exact_int(
        outbox_record["sequence_number"],
        code="invalid_stale_projection_sequence",
        minimum=1,
    )
    authoritative_sequence = require_exact_int(
        authoritative_projection_sequence,
        code="invalid_authoritative_projection_sequence",
        minimum=1,
    )
    if outbox_sequence >= authoritative_sequence:
        raise ContractViolation("projection_not_stale")
    owner = _uuid(outbox_record["owner_user_id"], "invalid_stale_projection_owner")
    claim_id = _uuid(outbox_record["claim_id"], "invalid_stale_projection_claim")
    revision_id = _uuid(
        outbox_record["revision_id"], "invalid_stale_projection_revision"
    )
    operation_id = _uuid(
        outbox_record["operation_id"], "invalid_stale_projection_operation"
    )
    for field in (
        "revision_sha256",
        "selection_binding_sha256",
        "retrieval_text_sha256",
        "embedding_input_sha256",
        "projection_manifest_sha256",
    ):
        require_sha256(outbox_record[field], f"invalid_stale_projection_{field}")
    if outbox_record["projection_contract_sha256"] != PROJECTION_CONTRACT_SHA256:
        raise ContractViolation("stale_projection_contract_mismatch")
    expected_manifest = projection_manifest_sha256(
        owner_user_id=owner,
        claim_id=claim_id,
        revision_id=revision_id,
        operation_id=operation_id,
        operation=operation,
        sequence_number=outbox_sequence,
        revision_sha256=str(outbox_record["revision_sha256"]),
        selection_binding_sha256=str(outbox_record["selection_binding_sha256"]),
        retrieval_text_sha256=str(outbox_record["retrieval_text_sha256"]),
        embedding_input_sha256=str(outbox_record["embedding_input_sha256"]),
    )
    if outbox_record["projection_manifest_sha256"] != expected_manifest:
        raise ContractViolation("stale_projection_manifest_mismatch")
    material = {
        "owner_user_id": str(owner),
        "claim_id": str(claim_id),
        "projection_operation_id": str(operation_id),
        "operation": operation.value,
        "from_state": state.value,
        "to_state": ProjectionState.SUPERSEDED.value,
        "outbox_sequence": outbox_sequence,
        "authoritative_projection_sequence": authoritative_sequence,
        "projection_manifest_sha256": expected_manifest,
        "reason_code": "stale_projection_sequence",
        "applied": False,
        "retryable": False,
    }
    return {
        **material,
        "receipt_sha256": canonical_sha256(
            "governed_memory.projection_supersession_receipt", material
        ),
    }


__all__ = [
    "DEFAULT_DIMENSIONS",
    "DISTANCE",
    "EMBEDDING_MODEL",
    "NORMALIZATION",
    "NUMERIC_ENCODING",
    "PROJECTION_CONTRACT_SHA256",
    "PROJECTION_DELETE_COMMAND_FIELDS",
    "PROJECTION_DELETE_RECEIPT_FIELDS",
    "QDRANT_ABSENCE_VERIFICATION_DOMAIN",
    "PROJECTION_OUTBOX_FIELDS",
    "PROJECTION_TEST_VECTORS",
    "ProjectionOperation",
    "QDRANT_PAYLOAD_FIELDS",
    "RELATIONAL_RENDERER_SHA256",
    "build_projection_delete",
    "build_projection_delete_receipt",
    "build_projection_point",
    "build_rebuild_projection_point",
    "normalize_embedding",
    "projection_manifest_sha256",
    "qdrant_absence_verification_sha256",
    "recompute_projection_contract_sha256",
    "recompute_relational_renderer_sha256",
    "render_projection_surface",
    "supersede_stale_projection",
    "vector_sha256",
]
