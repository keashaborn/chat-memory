from __future__ import annotations

"""Closed conversions from typed PostgreSQL rows into pure Memory contracts."""

from datetime import datetime
from typing import Any, Mapping
from uuid import UUID

from .contracts import (
    ContractViolation,
    require_bounded_text,
    require_exact_int,
    require_sha256,
    require_utc,
    require_uuid,
    selection_binding_sha256,
    sha256_text,
)
from .projection import PROJECTION_OUTBOX_FIELDS
from .retrieval import AUTHORITATIVE_RETRIEVAL_ROW_FIELDS


EXTRACTION_LEASE_FIELDS = (
    "owner_user_id",
    "job_id",
    "evidence_id",
    "source_kind",
    "source_message_id",
    "source_thread_id",
    "source_window_id",
    "source_window_sha256",
    "source_sha256",
    "selected_sha256",
    "selection_binding_sha256",
    "selected_start_utf8",
    "selected_end_utf8",
    "context_message_id",
    "context_sha256",
    "review_excerpt",
    "predicate_catalog_sha256",
    "attempt_number",
    "lease_token",
    "lease_expires_at",
    "provider_call_id",
    "provider_operation_id",
)

PROJECTION_REBUILD_ROW_FIELDS = (
    "owner_user_id",
    "claim_id",
    "revision_id",
    "revision_number",
    "revision_sha256",
    "projection_sequence",
    "lifecycle_state",
    "state_sha256",
    "semantic_key_sha256",
    "claim_identity_sha256",
    "source_sha256",
    "is_current",
    "projectable",
    "predicate_catalog_sha256",
    "selection_binding_sha256",
    "subject_entity_key",
    "subject_entity_type",
    "subject_display_name",
    "retrieval_text",
    "retrieval_text_sha256",
    "predicate",
    "object_kind",
    "object_entity_key",
    "object_entity_type",
    "object_display_name",
    "object_literal",
    "epistemic_state",
    "sensitivity",
    "domains",
    "intents",
    "surface",
    "requires_explicit",
    "valid_from",
    "valid_to",
    "updated_at",
    "outbox_id",
    "projection_operation_id",
    "projection_operation",
    "outbox_sequence_number",
    "point_id",
    "collection_alias",
    "projection_contract_sha256",
    "dimensions",
    "embedding_model",
    "renderer_sha256",
    "projection_manifest_sha256",
    "embedding_input_sha256",
    "applied_vector_sha256",
    "applied_physical_collection",
    "applied_at",
)


def _closed_row(
    value: Mapping[str, Any], fields: tuple[str, ...], code: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or tuple(value.keys()) != fields:
        raise ContractViolation(code)
    return dict(value)


def _uuid_text(value: object, code: str) -> str:
    return str(require_uuid(value, code))


def normalize_postgres_record(value: Any) -> Any:
    """Convert wire UUIDs to canonical text without weakening typed timestamps."""

    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {key: normalize_postgres_record(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_postgres_record(item) for item in value]
    return value


def extraction_lease_to_provider_inputs(
    value: Mapping[str, Any],
) -> dict[str, object]:
    row = _closed_row(value, EXTRACTION_LEASE_FIELDS, "invalid_extraction_lease_row")
    owner = require_uuid(row["owner_user_id"], "invalid_lease_owner")
    job_id = require_uuid(row["job_id"], "invalid_lease_job")
    evidence_id = require_uuid(row["evidence_id"], "invalid_lease_evidence")
    source_message_id = require_uuid(
        row["source_message_id"], "invalid_lease_source_message"
    )
    source_thread_id = require_uuid(
        row["source_thread_id"], "invalid_lease_source_thread"
    )
    source_window_id = require_uuid(
        row["source_window_id"], "invalid_lease_source_window"
    )
    provider_call_id = require_uuid(
        row["provider_call_id"], "invalid_lease_provider_call"
    )
    provider_operation_id = require_uuid(
        row["provider_operation_id"], "invalid_lease_provider_operation"
    )
    lease_token = require_uuid(row["lease_token"], "invalid_lease_token")
    lease_expires_at = require_utc(
        row["lease_expires_at"], "invalid_lease_expiration"
    )
    if row["source_kind"] != "conversation_message":
        raise ContractViolation("invalid_lease_source_kind")
    window_sha256 = require_sha256(
        row["source_window_sha256"], "invalid_lease_window_sha256"
    )
    source_sha256 = require_sha256(
        row["source_sha256"], "invalid_lease_source_sha256"
    )
    selected_sha256 = require_sha256(
        row["selected_sha256"], "invalid_lease_selected_sha256"
    )
    supplied_selection = require_sha256(
        row["selection_binding_sha256"], "invalid_lease_selection_binding"
    )
    catalog_sha256 = require_sha256(
        row["predicate_catalog_sha256"], "invalid_lease_catalog_sha256"
    )
    start = require_exact_int(
        row["selected_start_utf8"],
        code="invalid_lease_selected_start",
        maximum=16_000_000,
    )
    end = require_exact_int(
        row["selected_end_utf8"],
        code="invalid_lease_selected_end",
        minimum=1,
        maximum=16_000_000,
    )
    if end <= start:
        raise ContractViolation("invalid_lease_selected_range")
    selected_text = require_bounded_text(
        row["review_excerpt"],
        code="invalid_lease_selected_text",
        maximum_bytes=16_000,
    )
    if len(selected_text.encode("utf-8")) != end - start:
        raise ContractViolation("lease_selected_range_mismatch")
    if sha256_text(selected_text) != selected_sha256:
        raise ContractViolation("lease_selected_sha256_mismatch")
    context_message = row["context_message_id"]
    context_sha = row["context_sha256"]
    if (context_message is None) != (context_sha is None):
        raise ContractViolation("incomplete_lease_context_binding")
    context_message_id = (
        require_uuid(context_message, "invalid_lease_context_message")
        if context_message is not None
        else None
    )
    context_sha256 = (
        require_sha256(context_sha, "invalid_lease_context_sha256")
        if context_sha is not None
        else None
    )
    expected_selection = selection_binding_sha256(
        owner_user_id=owner,
        source_kind="conversation_message",
        source_message_id=source_message_id,
        source_thread_id=source_thread_id,
        source_window_id=source_window_id,
        window_sha256=window_sha256,
        source_sha256=source_sha256,
        selected_sha256=selected_sha256,
        start_utf8=start,
        end_utf8=end,
        context_message_id=context_message_id,
        context_sha256=context_sha256,
    )
    if supplied_selection != expected_selection:
        raise ContractViolation("lease_selection_binding_mismatch")
    attempt_number = require_exact_int(
        row["attempt_number"],
        code="invalid_lease_attempt_number",
        minimum=1,
        maximum=5,
    )
    context_lookup = None
    if context_message_id is not None:
        context_lookup = {
            "owner_user_id": str(owner),
            "message_id": str(context_message_id),
            "role": "assistant",
            "content_sha256": context_sha256,
        }
    return {
        "job": {
            "owner_user_id": str(owner),
            "job_id": str(job_id),
            "evidence_id": str(evidence_id),
            "provider_call_id": str(provider_call_id),
            "source_sha256": source_sha256,
            "state": "claimed",
            "attempt_number": attempt_number,
            "operation_id": str(provider_operation_id),
        },
        "evidence": {
            "owner_user_id": str(owner),
            "evidence_id": str(evidence_id),
            "source_kind": "conversation_message",
            "source_message_id": str(source_message_id),
            "source_thread_id": str(source_thread_id),
            "source_window_id": str(source_window_id),
            "start_utf8": start,
            "end_utf8": end,
            "selected_text": selected_text,
            "selected_sha256": selected_sha256,
            "source_sha256": source_sha256,
            "window_sha256": window_sha256,
            "context_message_id": (
                str(context_message_id) if context_message_id is not None else None
            ),
            "context_sha256": context_sha256,
            "selection_binding_sha256": supplied_selection,
        },
        "source_lookup": {
            "owner_user_id": str(owner),
            "message_id": str(source_message_id),
            "thread_id": str(source_thread_id),
            "content_sha256": source_sha256,
        },
        "context_lookup": context_lookup,
        "lease": {
            "lease_token": str(lease_token),
            "lease_expires_at": lease_expires_at,
            "predicate_catalog_sha256": catalog_sha256,
        },
    }


def projection_rebuild_row_to_inputs(
    value: Mapping[str, Any],
) -> dict[str, object]:
    row = _closed_row(
        value, PROJECTION_REBUILD_ROW_FIELDS, "invalid_projection_rebuild_row"
    )
    claim = normalize_postgres_record(
        {field: row[field] for field in AUTHORITATIVE_RETRIEVAL_ROW_FIELDS}
    )
    outbox = {
        "owner_user_id": _uuid_text(row["owner_user_id"], "invalid_rebuild_owner"),
        "claim_id": _uuid_text(row["claim_id"], "invalid_rebuild_claim"),
        "revision_id": _uuid_text(row["revision_id"], "invalid_rebuild_revision"),
        "operation_id": _uuid_text(
            row["projection_operation_id"], "invalid_rebuild_operation"
        ),
        "operation": row["projection_operation"],
        "sequence_number": row["outbox_sequence_number"],
        "revision_sha256": row["revision_sha256"],
        "selection_binding_sha256": row["selection_binding_sha256"],
        "retrieval_text_sha256": row["retrieval_text_sha256"],
        "embedding_input_sha256": row["embedding_input_sha256"],
        "projection_contract_sha256": row["projection_contract_sha256"],
        "projection_manifest_sha256": row["projection_manifest_sha256"],
        "state": "applied",
    }
    if tuple(outbox) != PROJECTION_OUTBOX_FIELDS:
        raise ContractViolation("invalid_rebuild_outbox_mapping")
    if row["projection_operation"] != "upsert":
        raise ContractViolation("invalid_rebuild_projection_operation")
    if row["point_id"] != row["claim_id"]:
        raise ContractViolation("invalid_rebuild_point_id")
    if row["outbox_sequence_number"] != row["projection_sequence"]:
        raise ContractViolation("invalid_rebuild_projection_sequence")
    if row["dimensions"] != 3072:
        raise ContractViolation("invalid_rebuild_dimensions")
    if row["embedding_model"] != "text-embedding-3-large":
        raise ContractViolation("invalid_rebuild_embedding_model")
    expected_vector_sha256 = require_sha256(
        row["applied_vector_sha256"], "invalid_rebuild_vector_sha256"
    )
    applied_at = require_utc(row["applied_at"], "invalid_rebuild_applied_at")
    physical = require_bounded_text(
        row["applied_physical_collection"],
        code="invalid_rebuild_physical_collection",
        maximum_bytes=128,
    )
    return {
        "claim": claim,
        "outbox": outbox,
        "expected_vector_sha256": expected_vector_sha256,
        "applied_physical_collection": physical,
        "applied_at": applied_at,
    }


__all__ = [
    "EXTRACTION_LEASE_FIELDS",
    "PROJECTION_REBUILD_ROW_FIELDS",
    "extraction_lease_to_provider_inputs",
    "normalize_postgres_record",
    "projection_rebuild_row_to_inputs",
]
