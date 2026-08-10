from __future__ import annotations

"""Default-off gate for atomic conversation-to-Memory capture."""

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Protocol
import os
import unicodedata
from uuid import UUID

from .contracts import (
    ContractViolation,
    canonical_sha256,
    require_bounded_text,
    require_sha256,
    require_utc,
    sha256_text,
)
from .eligibility import EligibilityPolicy
from .exclusive_cutover import (
    ExclusiveMemoryConfigurationError,
    successor_pilot_is_exclusive,
)


CAPTURE_MODE_ENV = "GOVERNED_MEMORY_CAPTURE_MODE"
CAPTURE_OWNER_ALLOWLIST_ENV = "GOVERNED_MEMORY_CAPTURE_OWNER_ALLOWLIST"
CAPTURE_MODE_OFF = "off"
CAPTURE_MODE_PILOT = "pilot"
CAPTURE_TEXT_AUTHORITY = "supabase_access_token_v1"
CAPTURE_USER_SOURCE = "frontend/chat:user"
_MAX_PILOT_OWNERS = 1


class CaptureConfigurationError(RuntimeError):
    pass


class ConversationCaptureConnection(Protocol):
    async def fetchrow(self, query: str, *args: object) -> Mapping[str, object] | None:
        ...


@dataclass(frozen=True, slots=True)
class CaptureDecision:
    mode: str
    owner_user_id: UUID
    enabled: bool


@dataclass(frozen=True, slots=True)
class CaptureSettings:
    mode: str
    owner_user_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if self.mode == CAPTURE_MODE_OFF:
            valid = self.owner_user_ids == ()
        elif self.mode == CAPTURE_MODE_PILOT:
            valid = (
                len(self.owner_user_ids) == 1
                and isinstance(self.owner_user_ids[0], UUID)
            )
        else:
            valid = False
        if not valid:
            raise CaptureConfigurationError("invalid_capture_configuration")


@dataclass(frozen=True, slots=True)
class CaptureReceipt:
    outcome: str
    outbox_id: UUID
    policy_sha256: str


def _canonical_uuid(value: object, code: str) -> UUID:
    if not isinstance(value, str):
        raise CaptureConfigurationError(code)
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise CaptureConfigurationError(code) from exc
    if str(parsed) != value:
        raise CaptureConfigurationError(code)
    return parsed


def capture_settings_from_environment(
    environ: Mapping[str, str] | None = None,
) -> CaptureSettings:
    """Parse and validate the immutable process-level capture settings."""
    settings = os.environ if environ is None else environ
    mode = settings.get(CAPTURE_MODE_ENV, CAPTURE_MODE_OFF)
    if mode == CAPTURE_MODE_OFF:
        return CaptureSettings(mode=mode, owner_user_ids=())
    if mode != CAPTURE_MODE_PILOT:
        raise CaptureConfigurationError("invalid_governed_memory_capture_mode")
    try:
        successor_exclusive = successor_pilot_is_exclusive(settings)
    except ExclusiveMemoryConfigurationError as exc:
        raise CaptureConfigurationError(
            "invalid_governed_memory_exclusive_mode"
        ) from exc
    if not successor_exclusive:
        raise CaptureConfigurationError(
            "governed_memory_capture_requires_exclusive_successor"
        )

    raw_allowlist = settings.get(CAPTURE_OWNER_ALLOWLIST_ENV, "")
    raw_items = [item.strip() for item in raw_allowlist.split(",")]
    if (
        not raw_allowlist
        or any(not item for item in raw_items)
        or len(raw_items) > _MAX_PILOT_OWNERS
    ):
        raise CaptureConfigurationError("invalid_capture_owner_allowlist")
    owners = tuple(
        _canonical_uuid(item, "invalid_capture_owner_allowlist")
        for item in raw_items
    )
    if len(set(owners)) != len(owners):
        raise CaptureConfigurationError("duplicate_capture_owner_allowlist")
    return CaptureSettings(mode=mode, owner_user_ids=owners)


def capture_decision_for_owner(
    owner_user_id: str,
    *,
    authority: str,
    source: str,
    has_attachments: bool,
    is_voice_turn: bool,
    no_store: bool,
    has_search_authorization: bool,
    settings: CaptureSettings | None = None,
    environ: Mapping[str, str] | None = None,
) -> CaptureDecision:
    """Resolve the server-owned owner and eligible-input pilot gate."""

    if settings is not None and environ is not None:
        raise CaptureConfigurationError("duplicate_capture_configuration")
    checked_settings = (
        capture_settings_from_environment(environ)
        if settings is None
        else settings
    )
    if not isinstance(checked_settings, CaptureSettings):
        raise CaptureConfigurationError("invalid_capture_configuration")
    owner = _canonical_uuid(owner_user_id, "invalid_capture_owner")
    eligible_input = (
        authority == CAPTURE_TEXT_AUTHORITY
        and source == CAPTURE_USER_SOURCE
        and has_attachments is False
        and is_voice_turn is False
        and no_store is False
        and has_search_authorization is False
    )
    return CaptureDecision(
        mode=checked_settings.mode,
        owner_user_id=owner,
        enabled=owner in checked_settings.owner_user_ids and eligible_input,
    )


def normalize_capture_text(value: str) -> str:
    if not isinstance(value, str):
        raise ContractViolation("invalid_capture_text")
    return unicodedata.normalize("NFC", value)


def capture_policy_sha256(source_created_at: datetime) -> str:
    created_at = require_utc(
        source_created_at, "invalid_capture_source_created_at"
    )
    return EligibilityPolicy(ingest_after=created_at).policy_sha256


def capture_auth_context_sha256(
    *,
    owner_user_id: UUID,
    session_id: UUID,
    authentication_manifest_sha256: str,
    authority: str,
    request_id: str,
) -> str:
    """Bind the DB session marker to server-verified request authority."""

    if not isinstance(owner_user_id, UUID):
        raise ContractViolation("invalid_capture_auth_owner")
    if not isinstance(session_id, UUID):
        raise ContractViolation("invalid_capture_auth_session")
    checked_manifest_sha256 = require_sha256(
        authentication_manifest_sha256,
        "invalid_capture_authentication_manifest_sha256",
    )
    checked_authority = require_bounded_text(
        authority,
        code="invalid_capture_auth_authority",
        maximum_bytes=64,
    )
    checked_request_id = require_bounded_text(
        request_id,
        code="invalid_capture_auth_request_id",
        maximum_bytes=128,
    )
    return canonical_sha256(
        "governed_memory.conversation_capture_auth_context",
        {
            "authentication_manifest_sha256": checked_manifest_sha256,
            "authority": checked_authority,
            "owner_user_id": owner_user_id,
            "request_id_sha256": sha256_text(checked_request_id),
            "session_id": session_id,
        },
    )


async def enqueue_captured_chat_log_message(
    connection: ConversationCaptureConnection,
    *,
    decision: CaptureDecision,
    message_id: UUID,
    source_created_at: datetime,
) -> CaptureReceipt:
    if not isinstance(decision, CaptureDecision) or not decision.enabled:
        raise ContractViolation("governed_memory_capture_not_enabled")
    if not isinstance(message_id, UUID):
        raise ContractViolation("invalid_capture_message_id")
    policy_sha256 = require_sha256(
        capture_policy_sha256(source_created_at),
        "invalid_capture_policy_sha256",
    )
    row = await connection.fetchrow(
        "SELECT outcome,outbox_id FROM "
        "memory_ingest_private.enqueue_chat_log_message($1::uuid,$2::text)",
        message_id,
        policy_sha256,
    )
    if row is None or tuple(row.keys()) != ("outcome", "outbox_id"):
        raise ContractViolation("invalid_capture_enqueue_receipt")
    if row["outcome"] not in {"enqueued", "replayed"}:
        raise ContractViolation("invalid_capture_enqueue_outcome")
    outbox_id = row["outbox_id"]
    if not isinstance(outbox_id, UUID):
        raise ContractViolation("invalid_capture_outbox_id")
    return CaptureReceipt(
        outcome=str(row["outcome"]),
        outbox_id=outbox_id,
        policy_sha256=policy_sha256,
    )


__all__ = [
    "CAPTURE_MODE_ENV",
    "CAPTURE_OWNER_ALLOWLIST_ENV",
    "CAPTURE_MODE_OFF",
    "CAPTURE_MODE_PILOT",
    "CAPTURE_TEXT_AUTHORITY",
    "CAPTURE_USER_SOURCE",
    "CaptureConfigurationError",
    "CaptureSettings",
    "ConversationCaptureConnection",
    "CaptureDecision",
    "CaptureReceipt",
    "capture_decision_for_owner",
    "capture_settings_from_environment",
    "normalize_capture_text",
    "capture_policy_sha256",
    "capture_auth_context_sha256",
    "enqueue_captured_chat_log_message",
]
