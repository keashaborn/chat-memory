from __future__ import annotations

"""Closed conversion for one lease-bound chat_log reader result."""

from typing import Any, Mapping
import unicodedata
from uuid import UUID

from .contracts import (
    ContractViolation,
    canonical_sha256,
    require_sha256,
    sha256_text,
)
from .worker import BRIDGE_LEASE_FIELDS


LEASED_CHAT_LOG_FIELDS = BRIDGE_LEASE_FIELDS + ("role", "content")


def _uuid(value: object, code: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ContractViolation(code) from exc


def split_leased_chat_log_message(
    value: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the existing worker lease envelope and its exact user payload."""

    if not isinstance(value, Mapping) or tuple(value.keys()) != LEASED_CHAT_LOG_FIELDS:
        raise ContractViolation("invalid_leased_chat_log_row")
    if value["role"] != "user":
        raise ContractViolation("invalid_leased_chat_log_role")
    content = value["content"]
    if not isinstance(content, str) or unicodedata.normalize("NFC", content) != content:
        raise ContractViolation("invalid_leased_chat_log_content")
    content_sha256 = require_sha256(
        value["content_sha256"], "invalid_leased_chat_log_content_sha256"
    )
    if sha256_text(content) != content_sha256:
        raise ContractViolation("leased_chat_log_content_sha256_mismatch")

    owner_user_id = _uuid(value["owner_user_id"], "invalid_leased_chat_log_owner")
    message_id = _uuid(value["message_id"], "invalid_leased_chat_log_message")
    thread_id = _uuid(value["thread_id"], "invalid_leased_chat_log_thread")
    exchange_id = _uuid(
        value["exchange_id"], "invalid_leased_chat_log_exchange"
    )
    window_id = _uuid(value["window_id"], "invalid_leased_chat_log_window")
    if exchange_id != message_id or window_id != message_id:
        raise ContractViolation("leased_chat_log_derived_lineage_mismatch")
    if type(value["window_ordinal"]) is not int or value["window_ordinal"] != 0:
        raise ContractViolation("invalid_leased_chat_log_window_ordinal")
    expected_window_sha256 = canonical_sha256(
        "governed_memory.ingest_window",
        {
            "owner_user_id": owner_user_id,
            "thread_id": thread_id,
            "exchange_id": exchange_id,
            "window_id": window_id,
            "message_id": message_id,
            "content_sha256": content_sha256,
        },
    )
    if require_sha256(
        value["window_sha256"], "invalid_leased_chat_log_window_sha256"
    ) != expected_window_sha256:
        raise ContractViolation("leased_chat_log_window_sha256_mismatch")

    lease_envelope = {field: value[field] for field in BRIDGE_LEASE_FIELDS}
    payload = {
        "owner_user_id": str(owner_user_id),
        "thread_id": str(thread_id),
        "message_id": str(message_id),
        "role": "user",
        "created_at": value["source_created_at"],
        "content_sha256": content_sha256,
        "content": content,
        "exchange_id": str(exchange_id),
        "window_id": str(window_id),
        "window_ordinal": 0,
        "window_sha256": expected_window_sha256,
    }
    return lease_envelope, payload


__all__ = [
    "LEASED_CHAT_LOG_FIELDS",
    "split_leased_chat_log_message",
]
