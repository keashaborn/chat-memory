from __future__ import annotations

"""Pure, closed primitives shared by the governed-memory successor.

This module deliberately has no configuration, filesystem, clock, database, or
network access.  Callers provide identifiers and timestamps so every material
hash and transition can be reproduced offline.
"""

from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import json
import re
import sys
from types import MappingProxyType
import unicodedata
from typing import Any, Mapping, Sequence
from uuid import UUID


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_:/.-]{0,127}$")
MINIMUM_PYTHON_VERSION = (3, 11)
PYTHON_REQUIRES = ">=3.11"
RELATIONAL_RENDERER_CONTRACT = {
    "name": "canonical_relational_fact",
    "encoding": "utf-8",
    "format": "canonical_json",
    "fields": ["object", "predicate", "subject"],
}

if sys.version_info[:2] < MINIMUM_PYTHON_VERSION:
    raise RuntimeError("python_3_11_or_newer_required")


class ContractViolation(ValueError):
    """A content-free deterministic contract failure."""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not _KEY_RE.fullmatch(code):
            code = "invalid_contract_error_code"
        self.code = code
        super().__init__(code)


class EligibilityDecision(str, Enum):
    SEND_EXTERNAL = "send_external"
    SKIP_ZERO_CALL = "skip_zero_call"
    ROUTE_INTERNAL = "route_internal"
    BLOCK_LOCAL = "block_local"
    REVIEW_CONTEXT = "review_context"


class ExtractionJobState(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    RETRYABLE = "retryable"
    FAILED_TERMINAL = "failed_terminal"


class ProposalState(str, Enum):
    PENDING_REVIEW = "pending_review"
    ADMITTED = "admitted"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ClaimLifecycleState(str, Enum):
    ACTIVE = "active"
    CORRECTION_PENDING = "correction_pending"
    RETRACTED = "retracted"
    DELETION_PENDING = "deletion_pending"
    DELETED = "deleted"


class EpistemicStatus(str, Enum):
    SUPPORTED = "supported"
    UNCERTAIN = "uncertain"
    DISPUTED = "disputed"


class Sensitivity(str, Enum):
    ORDINARY = "ordinary"
    SENSITIVE_SELF = "sensitive_self"
    SENSITIVE_THIRD_PARTY = "sensitive_third_party"


class EntityKind(str, Enum):
    SELF = "self"
    PERSON = "person"
    PET = "pet"
    PLACE = "place"
    ORGANIZATION = "organization"
    OTHER = "other"


class ObjectKind(str, Enum):
    LITERAL = "literal"
    ENTITY = "entity"


class ProjectionState(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    APPLIED = "applied"
    RETRYABLE = "retryable"
    FAILED_TERMINAL = "failed_terminal"
    SUPERSEDED = "superseded"


class OperationOutcome(str, Enum):
    APPLIED = "applied"
    REPLAYED = "replayed"


def require_sha256(value: object, code: str = "invalid_sha256") -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ContractViolation(code)
    return value


def require_uuid(value: object, code: str = "invalid_uuid") -> UUID:
    if not isinstance(value, UUID):
        raise ContractViolation(code)
    return value


def require_utc(value: object, code: str = "invalid_timestamp") -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ContractViolation(code)
    if value.utcoffset() is None:
        raise ContractViolation(code)
    return value.astimezone(UTC)


def utc_text(value: datetime) -> str:
    normalized = require_utc(value)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_utc_text(value: datetime | str, code: str) -> str:
    """Normalize a typed timestamp or PostgreSQL wire timestamp to UTC text."""

    if isinstance(value, datetime):
        return utc_text(require_utc(value, code))
    if not isinstance(value, str):
        raise ContractViolation(code)
    require_nfc(value, code)
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ContractViolation(code) from exc
    return utc_text(require_utc(parsed, code))


def require_nfc(value: object, code: str = "invalid_unicode") -> str:
    if not isinstance(value, str) or unicodedata.normalize("NFC", value) != value:
        raise ContractViolation(code)
    return value


def require_bounded_text(
    value: object,
    *,
    code: str,
    minimum_bytes: int = 1,
    maximum_bytes: int,
    strip: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ContractViolation(code)
    checked = value.strip() if strip else value
    require_nfc(checked, code)
    size = len(checked.encode("utf-8"))
    if size < minimum_bytes or size > maximum_bytes:
        raise ContractViolation(code)
    return checked


def require_exact_int(
    value: object,
    *,
    code: str,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if type(value) is not int or value < minimum:
        raise ContractViolation(code)
    if maximum is not None and value > maximum:
        raise ContractViolation(code)
    return value


def require_key(value: object, code: str = "invalid_key") -> str:
    if not isinstance(value, str) or not _KEY_RE.fullmatch(value):
        raise ContractViolation(code)
    return value


def require_role(value: object, code: str = "invalid_source_role") -> str:
    if not isinstance(value, str) or not _ROLE_RE.fullmatch(value):
        raise ContractViolation(code)
    return value


def require_sorted_unique(
    values: Sequence[Any],
    *,
    code: str,
    key: Any = None,
) -> tuple[Any, ...]:
    result = tuple(values)
    sort_key = key or (lambda item: item)
    try:
        if tuple(sorted(set(result), key=sort_key)) != result:
            raise ContractViolation(code)
    except TypeError as exc:
        raise ContractViolation(code) from exc
    return result


def canonical_decimal_text(value: object) -> str:
    try:
        decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ContractViolation("invalid_decimal") from exc
    if not decimal.is_finite():
        raise ContractViolation("invalid_decimal")
    if decimal == 0:
        return "0"
    rendered = format(decimal, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if isinstance(value, str):
        return require_nfc(value, "invalid_contract_value_unicode")
    if type(value) is int:
        return value
    if isinstance(value, float):
        raise ContractViolation("float_not_allowed_in_contract_json")
    if isinstance(value, Decimal):
        return canonical_decimal_text(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return utc_text(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _canonical_value(asdict(value))
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractViolation("non_string_contract_key")
            require_nfc(key, "invalid_contract_key_unicode")
            result[key] = _canonical_value(item)
        return result
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    raise ContractViolation("unsupported_contract_value")


def canonical_json_bytes(value: object) -> bytes:
    """Return the single canonical JSON representation used for contracts."""

    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_hex(value: bytes | bytearray | memoryview | str) -> str:
    if isinstance(value, str):
        data = value.encode("utf-8")
    elif isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
    else:
        raise ContractViolation("invalid_sha256_input")
    return hashlib.sha256(data).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return sha256_hex(value)


def sha256_text(value: str) -> str:
    return sha256_hex(value)


def canonical_sha256(domain: str, value: object) -> str:
    checked_domain = require_key(domain, "invalid_hash_domain")
    return sha256_hex(
        canonical_json_bytes({"domain": checked_domain, "material": value})
    )


def framed_material_bytes(
    domain: str,
    ordered_fields: Sequence[tuple[str, str | None]],
) -> bytes:
    """Encode fixed fields for byte-identical Python/PostgreSQL hashing.

    The framing is deliberately not JSON.  Each non-null value is NFC text and
    is length-prefixed by its UTF-8 byte count.  A null value uses ``-`` as its
    length.  Field order is contract material.
    """

    checked_domain = require_key(domain, "invalid_framed_hash_domain")
    parts = [f"{checked_domain}\n".encode("utf-8")]
    seen: set[str] = set()
    for item in ordered_fields:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ContractViolation("invalid_framed_hash_field")
        name, value = item
        checked_name = require_key(name, "invalid_framed_hash_field_name")
        if checked_name in seen:
            raise ContractViolation("duplicate_framed_hash_field")
        seen.add(checked_name)
        if value is None:
            parts.append(f"{checked_name}:-:\n".encode("utf-8"))
            continue
        checked_value = require_nfc(value, "invalid_framed_hash_field_value")
        encoded = checked_value.encode("utf-8")
        parts.append(
            f"{checked_name}:{len(encoded)}:".encode("utf-8")
            + encoded
            + b"\n"
        )
    return b"".join(parts)


def framed_sha256(
    domain: str,
    ordered_fields: Sequence[tuple[str, str | None]],
) -> str:
    return sha256_hex(framed_material_bytes(domain, ordered_fields))


BRIDGE_SOURCE_BINDING_DOMAIN = "governed_memory.bridge_source.v1"
BRIDGE_SOURCE_BINDING_FIELDS = (
    "owner_user_id",
    "message_id",
    "thread_id",
    "exchange_id",
    "window_id",
    "window_ordinal",
    "window_sha256",
    "content_sha256",
    "policy_sha256",
    "source_created_at",
)


def bridge_source_binding_material_bytes(
    *,
    owner_user_id: UUID,
    message_id: UUID,
    thread_id: UUID,
    exchange_id: UUID,
    window_id: UUID,
    window_ordinal: int,
    window_sha256: str,
    content_sha256: str,
    policy_sha256: str,
    source_created_at: datetime | str,
) -> bytes:
    """Return the exact PostgreSQL conversation-bridge source preimage.

    ``ingest_after`` is deliberately absent.  The effective cutover is bound
    through ``policy_sha256`` and is separately checked by the worker lease
    validator.
    """

    for value, code in (
        (owner_user_id, "invalid_bridge_source_owner"),
        (message_id, "invalid_bridge_source_message"),
        (thread_id, "invalid_bridge_source_thread"),
        (exchange_id, "invalid_bridge_source_exchange"),
        (window_id, "invalid_bridge_source_window"),
    ):
        require_uuid(value, code)
    ordinal = require_exact_int(
        window_ordinal,
        code="invalid_bridge_source_window_ordinal",
        maximum=10_000_000,
    )
    fields = (
        ("owner_user_id", str(owner_user_id)),
        ("message_id", str(message_id)),
        ("thread_id", str(thread_id)),
        ("exchange_id", str(exchange_id)),
        ("window_id", str(window_id)),
        ("window_ordinal", str(ordinal)),
        (
            "window_sha256",
            require_sha256(window_sha256, "invalid_bridge_source_window_sha256"),
        ),
        (
            "content_sha256",
            require_sha256(content_sha256, "invalid_bridge_source_content_sha256"),
        ),
        (
            "policy_sha256",
            require_sha256(policy_sha256, "invalid_bridge_source_policy_sha256"),
        ),
        (
            "source_created_at",
            _canonical_utc_text(
                source_created_at,
                "invalid_bridge_source_created_at",
            ),
        ),
    )
    if tuple(name for name, _ in fields) != BRIDGE_SOURCE_BINDING_FIELDS:
        raise ContractViolation("bridge_source_binding_field_order_mismatch")
    return framed_material_bytes(BRIDGE_SOURCE_BINDING_DOMAIN, fields)


def bridge_source_binding_sha256(
    *,
    owner_user_id: UUID,
    message_id: UUID,
    thread_id: UUID,
    exchange_id: UUID,
    window_id: UUID,
    window_ordinal: int,
    window_sha256: str,
    content_sha256: str,
    policy_sha256: str,
    source_created_at: datetime | str,
) -> str:
    return sha256_hex(
        bridge_source_binding_material_bytes(
            owner_user_id=owner_user_id,
            message_id=message_id,
            thread_id=thread_id,
            exchange_id=exchange_id,
            window_id=window_id,
            window_ordinal=window_ordinal,
            window_sha256=window_sha256,
            content_sha256=content_sha256,
            policy_sha256=policy_sha256,
            source_created_at=source_created_at,
        )
    )


BRIDGE_SOURCE_HASH_TEST_VECTOR = MappingProxyType(
    {
        "domain": BRIDGE_SOURCE_BINDING_DOMAIN,
        "ordered_fields": BRIDGE_SOURCE_BINDING_FIELDS,
        "material": MappingProxyType(
            {
                "owner_user_id": "00000000-0000-4000-8000-000000000001",
                "message_id": "00000000-0000-4000-8000-000000000002",
                "thread_id": "00000000-0000-4000-8000-000000000003",
                "exchange_id": "00000000-0000-4000-8000-000000000004",
                "window_id": "00000000-0000-4000-8000-000000000005",
                "window_ordinal": 7,
                "window_sha256": "1" * 64,
                "content_sha256": "2" * 64,
                "policy_sha256": "3" * 64,
                "source_created_at": "2026-08-09T12:34:56.123456Z",
            }
        ),
        "preimage": (
            "governed_memory.bridge_source.v1\n"
            "owner_user_id:36:00000000-0000-4000-8000-000000000001\n"
            "message_id:36:00000000-0000-4000-8000-000000000002\n"
            "thread_id:36:00000000-0000-4000-8000-000000000003\n"
            "exchange_id:36:00000000-0000-4000-8000-000000000004\n"
            "window_id:36:00000000-0000-4000-8000-000000000005\n"
            "window_ordinal:1:7\n"
            "window_sha256:64:1111111111111111111111111111111111111111111111111111111111111111\n"
            "content_sha256:64:2222222222222222222222222222222222222222222222222222222222222222\n"
            "policy_sha256:64:3333333333333333333333333333333333333333333333333333333333333333\n"
            "source_created_at:27:2026-08-09T12:34:56.123456Z\n"
        ).encode("utf-8"),
        "sha256": "f1ed425554af394ea4a0b12cfcbf5dbd069b36131a55cb5411b3fc34d49ec279",
    }
)


def selection_binding_material_bytes(
    *,
    owner_user_id: UUID,
    source_kind: str,
    source_message_id: UUID,
    source_thread_id: UUID,
    source_window_id: UUID,
    window_sha256: str,
    source_sha256: str,
    selected_sha256: str,
    start_utf8: int,
    end_utf8: int,
    context_message_id: UUID | None,
    context_sha256: str | None,
) -> bytes:
    require_uuid(owner_user_id, "invalid_selection_owner")
    checked_source_kind = require_key(
        source_kind, "invalid_selection_source_kind"
    )
    require_uuid(source_message_id, "invalid_selection_source_message")
    require_uuid(source_thread_id, "invalid_selection_source_thread")
    require_uuid(source_window_id, "invalid_selection_source_window")
    checked_window_sha256 = require_sha256(
        window_sha256, "invalid_selection_window_sha256"
    )
    checked_source_sha256 = require_sha256(
        source_sha256, "invalid_selection_source_sha256"
    )
    checked_selected_sha256 = require_sha256(
        selected_sha256, "invalid_selection_selected_sha256"
    )
    start = require_exact_int(
        start_utf8, code="invalid_selection_start", maximum=10_000_000
    )
    end = require_exact_int(
        end_utf8, code="invalid_selection_end", maximum=10_000_000
    )
    if end <= start:
        raise ContractViolation("invalid_selection_range")
    if (context_message_id is None) != (context_sha256 is None):
        raise ContractViolation("incomplete_selection_context")
    if context_message_id is not None:
        require_uuid(context_message_id, "invalid_selection_context_message")
        checked_context_sha256 = require_sha256(
            context_sha256, "invalid_selection_context_sha256"
        )
    else:
        checked_context_sha256 = None
    return framed_material_bytes(
        "governed_memory.selection_binding.v1",
        (
            ("owner_user_id", str(owner_user_id)),
            ("source_kind", checked_source_kind),
            ("source_message_id", str(source_message_id)),
            ("source_thread_id", str(source_thread_id)),
            ("source_window_id", str(source_window_id)),
            ("window_sha256", checked_window_sha256),
            ("source_sha256", checked_source_sha256),
            ("selected_sha256", checked_selected_sha256),
            ("start_utf8", str(start)),
            ("end_utf8", str(end)),
            (
                "context_message_id",
                str(context_message_id) if context_message_id is not None else None,
            ),
            ("context_sha256", checked_context_sha256),
        ),
    )


def selection_binding_sha256(
    *,
    owner_user_id: UUID,
    source_kind: str,
    source_message_id: UUID,
    source_thread_id: UUID,
    source_window_id: UUID,
    window_sha256: str,
    source_sha256: str,
    selected_sha256: str,
    start_utf8: int,
    end_utf8: int,
    context_message_id: UUID | None,
    context_sha256: str | None,
) -> str:
    """Bind a selected span and optional bounded context without storing text."""

    return sha256_hex(
        selection_binding_material_bytes(
            owner_user_id=owner_user_id,
            source_kind=source_kind,
            source_message_id=source_message_id,
            source_thread_id=source_thread_id,
            source_window_id=source_window_id,
            window_sha256=window_sha256,
            source_sha256=source_sha256,
            selected_sha256=selected_sha256,
            start_utf8=start_utf8,
            end_utf8=end_utf8,
            context_message_id=context_message_id,
            context_sha256=context_sha256,
        )
    )


def semantic_key_material_bytes(
    *,
    subject_entity_key: str,
    predicate: str,
    object_kind: ObjectKind | str,
    object_entity_key: str | None,
    object_literal: str | None,
) -> bytes:
    checked_subject_key = require_key(
        subject_entity_key, "invalid_semantic_subject_entity_key"
    )
    checked_predicate = require_key(predicate, "invalid_semantic_predicate")
    try:
        checked_object_kind = ObjectKind(object_kind)
    except (TypeError, ValueError) as exc:
        raise ContractViolation("invalid_semantic_object_kind") from exc
    if checked_object_kind is ObjectKind.ENTITY:
        checked_object_entity_key = require_key(
            object_entity_key, "invalid_semantic_object_entity_key"
        )
        if object_literal is not None:
            raise ContractViolation("invalid_semantic_object_value")
        checked_object_literal = None
    else:
        if object_entity_key is not None:
            raise ContractViolation("invalid_semantic_object_value")
        checked_object_entity_key = None
        checked_object_literal = require_bounded_text(
            object_literal,
            code="invalid_semantic_object_literal",
            maximum_bytes=2_000,
        )
    return framed_material_bytes(
        "governed_memory.semantic_key.v1",
        (
            ("subject_entity_key", checked_subject_key),
            ("predicate", checked_predicate),
            ("object_kind", checked_object_kind.value),
            ("object_entity_key", checked_object_entity_key),
            ("object_literal", checked_object_literal),
        ),
    )


def semantic_key_sha256(
    *,
    subject_entity_key: str,
    predicate: str,
    object_kind: ObjectKind | str,
    object_entity_key: str | None,
    object_literal: str | None,
) -> str:
    return sha256_hex(
        semantic_key_material_bytes(
            subject_entity_key=subject_entity_key,
            predicate=predicate,
            object_kind=object_kind,
            object_entity_key=object_entity_key,
            object_literal=object_literal,
        )
    )


_SELECTION_VECTOR_BASE_FIELDS = (
    ("owner_user_id", "00000000-0000-0000-0000-000000000001"),
    ("source_kind", "conversation_message"),
    ("source_message_id", "00000000-0000-0000-0000-000000000002"),
    ("source_thread_id", "00000000-0000-0000-0000-000000000003"),
    ("source_window_id", "00000000-0000-0000-0000-000000000004"),
    ("window_sha256", "1" * 64),
    ("source_sha256", "2" * 64),
    ("selected_sha256", "3" * 64),
    ("start_utf8", "7"),
    ("end_utf8", "19"),
)
_SELECTION_VECTOR_PREFIX = (
    "governed_memory.selection_binding.v1\n"
    "owner_user_id:36:00000000-0000-0000-0000-000000000001\n"
    "source_kind:20:conversation_message\n"
    "source_message_id:36:00000000-0000-0000-0000-000000000002\n"
    "source_thread_id:36:00000000-0000-0000-0000-000000000003\n"
    "source_window_id:36:00000000-0000-0000-0000-000000000004\n"
    f"window_sha256:64:{'1' * 64}\n"
    f"source_sha256:64:{'2' * 64}\n"
    f"selected_sha256:64:{'3' * 64}\n"
    "start_utf8:1:7\n"
    "end_utf8:2:19\n"
)
FRAMED_HASH_TEST_VECTORS = MappingProxyType(
    {
        "selection_without_context": MappingProxyType(
            {
                "domain": "governed_memory.selection_binding.v1",
                "ordered_fields": _SELECTION_VECTOR_BASE_FIELDS
                + (("context_message_id", None), ("context_sha256", None)),
                "preimage": (
                    _SELECTION_VECTOR_PREFIX
                    + "context_message_id:-:\ncontext_sha256:-:\n"
                ).encode("utf-8"),
                "sha256": (
                    "20057004136f64e54333d53d4e46c85d85eba494c3140596f197572b0d47b619"
                ),
            }
        ),
        "selection_with_context": MappingProxyType(
            {
                "domain": "governed_memory.selection_binding.v1",
                "ordered_fields": _SELECTION_VECTOR_BASE_FIELDS
                + (
                    (
                        "context_message_id",
                        "00000000-0000-0000-0000-000000000005",
                    ),
                    ("context_sha256", "4" * 64),
                ),
                "preimage": (
                    _SELECTION_VECTOR_PREFIX
                    + "context_message_id:36:00000000-0000-0000-0000-000000000005\n"
                    + f"context_sha256:64:{'4' * 64}\n"
                ).encode("utf-8"),
                "sha256": (
                    "88c6ea7d5e4aa020ca5f5acca6586e2b2dfe260c4d0b693ad06f27004ab2d860"
                ),
            }
        ),
        "semantic_literal_delimiter_newline_unicode": MappingProxyType(
            {
                "domain": "governed_memory.semantic_key.v1",
                "ordered_fields": (
                    ("subject_entity_key", "self"),
                    ("predicate", "preference.personal"),
                    ("object_kind", "literal"),
                    ("object_entity_key", None),
                    ("object_literal", "café:\n猫"),
                ),
                "preimage": (
                    "governed_memory.semantic_key.v1\n"
                    "subject_entity_key:4:self\n"
                    "predicate:19:preference.personal\n"
                    "object_kind:7:literal\n"
                    "object_entity_key:-:\n"
                    "object_literal:10:café:\n猫\n"
                ).encode("utf-8"),
                "sha256": (
                    "3395c6bd0e42b2fc8340a5fcb0ce437b11d97d1811b32c544574ea8b0c360fdc"
                ),
            }
        ),
        "semantic_entity": MappingProxyType(
            {
                "domain": "governed_memory.semantic_key.v1",
                "ordered_fields": (
                    ("subject_entity_key", "person:alice"),
                    ("predicate", "relationship.kind"),
                    ("object_kind", "entity"),
                    ("object_entity_key", "person:bob"),
                    ("object_literal", None),
                ),
                "preimage": (
                    "governed_memory.semantic_key.v1\n"
                    "subject_entity_key:12:person:alice\n"
                    "predicate:17:relationship.kind\n"
                    "object_kind:6:entity\n"
                    "object_entity_key:10:person:bob\n"
                    "object_literal:-:\n"
                ).encode("utf-8"),
                "sha256": (
                    "0717334b30e69d2d6f0aeb5b2d057c9a038d75f137108440083aabfb5e582149"
                ),
            }
        ),
    }
)


def render_relational_fact(
    *,
    subject: Mapping[str, object],
    predicate: str,
    object_value: Mapping[str, object],
) -> str:
    """Render the only semantic text surface used for retrieval and embedding."""

    require_key(predicate, "invalid_relational_predicate")
    if not isinstance(subject, Mapping) or not isinstance(object_value, Mapping):
        raise ContractViolation("invalid_relational_fact_shape")
    return canonical_json_bytes(
        {
            "object": object_value,
            "predicate": predicate,
            "subject": subject,
        }
    ).decode("utf-8")


@dataclass(frozen=True, slots=True, kw_only=True)
class IngestEnvelope:
    owner_user_id: UUID
    message_id: UUID
    thread_id: UUID
    created_at: datetime
    source_role: str
    content: str
    content_sha256: str
    exchange_id: UUID
    window_id: UUID
    window_ordinal: int
    window_sha256: str

    def __post_init__(self) -> None:
        require_uuid(self.owner_user_id, "invalid_ingest_owner")
        require_uuid(self.message_id, "invalid_ingest_message")
        require_uuid(self.thread_id, "invalid_ingest_thread")
        require_uuid(self.exchange_id, "invalid_ingest_exchange")
        require_uuid(self.window_id, "invalid_ingest_window")
        require_exact_int(
            self.window_ordinal,
            code="invalid_ingest_window_ordinal",
            maximum=10_000_000,
        )
        require_utc(self.created_at, "invalid_ingest_timestamp")
        require_role(self.source_role)
        if not isinstance(self.content, str):
            raise ContractViolation("invalid_ingest_content")
        require_nfc(self.content, "invalid_ingest_content_unicode")
        require_sha256(self.content_sha256, "invalid_ingest_content_sha256")
        require_sha256(self.window_sha256, "invalid_ingest_window_sha256")
        if sha256_text(self.content) != self.content_sha256:
            raise ContractViolation("ingest_content_sha256_mismatch")

@dataclass(frozen=True, slots=True, kw_only=True)
class SelectedEvidence:
    source_kind: str
    source_message_id: UUID
    source_thread_id: UUID
    source_window_id: UUID
    char_start: int
    char_end: int
    content_sha256: str
    category: str
    assertion_mode: str
    subject_hint: str
    sensitivity: str

    def __post_init__(self) -> None:
        require_key(self.source_kind, "invalid_evidence_source_kind")
        require_uuid(self.source_message_id, "invalid_evidence_source_message")
        require_uuid(self.source_thread_id, "invalid_evidence_source_thread")
        require_uuid(self.source_window_id, "invalid_evidence_source_window")
        require_exact_int(
            self.char_start, code="invalid_evidence_start", maximum=10_000_000
        )
        require_exact_int(
            self.char_end, code="invalid_evidence_end", maximum=10_000_000
        )
        if self.char_end <= self.char_start:
            raise ContractViolation("invalid_evidence_range")
        require_sha256(self.content_sha256, "invalid_evidence_content_sha256")
        require_key(self.category, "invalid_evidence_category")
        require_key(self.assertion_mode, "invalid_evidence_assertion_mode")
        require_key(self.subject_hint, "invalid_evidence_subject_hint")
        try:
            Sensitivity(self.sensitivity)
        except (TypeError, ValueError) as exc:
            raise ContractViolation("invalid_evidence_sensitivity") from exc

    @property
    def binding_sha256(self) -> str:
        return canonical_sha256("governed_memory.selected_evidence", self)


__all__ = [
    "BRIDGE_SOURCE_BINDING_DOMAIN",
    "BRIDGE_SOURCE_BINDING_FIELDS",
    "BRIDGE_SOURCE_HASH_TEST_VECTOR",
    "ClaimLifecycleState",
    "ContractViolation",
    "EligibilityDecision",
    "EntityKind",
    "EpistemicStatus",
    "ExtractionJobState",
    "FRAMED_HASH_TEST_VECTORS",
    "IngestEnvelope",
    "OperationOutcome",
    "ObjectKind",
    "MINIMUM_PYTHON_VERSION",
    "PYTHON_REQUIRES",
    "RELATIONAL_RENDERER_CONTRACT",
    "ProjectionState",
    "ProposalState",
    "SelectedEvidence",
    "Sensitivity",
    "canonical_decimal_text",
    "canonical_json_bytes",
    "canonical_sha256",
    "bridge_source_binding_material_bytes",
    "bridge_source_binding_sha256",
    "framed_material_bytes",
    "framed_sha256",
    "require_bounded_text",
    "require_exact_int",
    "require_key",
    "require_nfc",
    "require_role",
    "require_sha256",
    "require_sorted_unique",
    "require_utc",
    "require_uuid",
    "render_relational_fact",
    "semantic_key_material_bytes",
    "semantic_key_sha256",
    "selection_binding_material_bytes",
    "selection_binding_sha256",
    "sha256_bytes",
    "sha256_hex",
    "sha256_text",
    "utc_text",
]
