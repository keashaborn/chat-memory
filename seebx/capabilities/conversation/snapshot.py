from __future__ import annotations

"""Immutable, owner-scoped conversation snapshot contract.

This capability owns validation, content budgets, and hash-bound snapshot
construction. PostgreSQL reads belong to ``seebx.adapters.conversation_snapshot``.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rag_engine.chat_integrity import ATTESTED_ASSISTANT_SOURCE
from rag_engine.response_policy_v0_2 import (
    ConversationRole,
    ResponsePolicyConversationMessageV0_2,
)


SNAPSHOT_VERSION = "response_conversation_snapshot_v1"
USER_SOURCE = "frontend/chat:user"
VOICE_REALTIME_USER_SOURCE = "voice/realtime-preview:user"
ASSISTANT_SOURCE = "frontend/chat:assistant"
MAX_PRIOR_MESSAGES = 23
MAX_CURRENT_MESSAGE_BYTES = 32_768
MAX_PRIOR_CONTENT_BYTES = 48_000
MAX_TOTAL_CONTENT_BYTES = 80_768
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")


def is_valid_snapshot_request_id(value: Any) -> bool:
    """Return whether a request identifier is admissible to a snapshot."""

    return isinstance(value, str) and _REQUEST_ID_RE.fullmatch(value) is not None


class ConversationSnapshotError(RuntimeError):
    """Generic fail-closed transcript snapshot error."""


class ConversationSnapshotOutcome(str, Enum):
    CURRENT_REQUEST_BOUND = "current_request_bound"
    CURRENT_REQUEST_ABSENT = "current_request_absent"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
    )


def _canonical_json_bytes(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _text_sha256(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def normalize_snapshot_timestamp(value: Any, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ConversationSnapshotError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


class ConversationSnapshotV1(_StrictFrozenModel):
    contract_version: Literal[SNAPSHOT_VERSION] = SNAPSHOT_VERSION
    authenticated_actor_user_id: UUID = Field(repr=False)
    thread_id: UUID = Field(repr=False)
    current_request_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$",
        max_length=160,
        repr=False,
    )
    outcome: ConversationSnapshotOutcome
    current_log_id: UUID | None = Field(default=None, repr=False)
    cutoff_created_at: datetime | None = Field(default=None, repr=False)
    messages: tuple[ResponsePolicyConversationMessageV0_2, ...] = Field(
        min_length=1,
        max_length=MAX_PRIOR_MESSAGES + 1,
        repr=False,
    )
    prior_candidate_count: int = Field(ge=0, le=MAX_PRIOR_MESSAGES)
    prior_selected_count: int = Field(ge=0, le=MAX_PRIOR_MESSAGES)
    prior_budget_dropped_count: int = Field(ge=0, le=MAX_PRIOR_MESSAGES)
    prior_message_limit_truncated: bool
    prior_content_bytes: int = Field(ge=0, le=MAX_PRIOR_CONTENT_BYTES)
    total_content_bytes: int = Field(ge=1, le=MAX_TOTAL_CONTENT_BYTES)
    current_message_sha256: str
    conversation_sha256: str
    snapshot_sha256: str

    @field_validator("cutoff_created_at")
    @classmethod
    def cutoff_utc(cls, value: datetime | None) -> datetime | None:
        return (
            normalize_snapshot_timestamp(value, "cutoff_created_at")
            if value is not None
            else None
        )

    @field_validator(
        "current_message_sha256",
        "conversation_sha256",
        "snapshot_sha256",
    )
    @classmethod
    def hash_shape(cls, value: str) -> str:
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("snapshot hashes must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def exact_snapshot(self) -> "ConversationSnapshotV1":
        if self.messages[-1].role is not ConversationRole.USER:
            raise ValueError("snapshot must end with the current user message")
        if self.outcome is ConversationSnapshotOutcome.CURRENT_REQUEST_BOUND:
            if self.current_log_id is None or self.cutoff_created_at is None:
                raise ValueError("bound snapshot requires current row cutoff")
        elif self.current_log_id is not None or self.cutoff_created_at is not None:
            raise ValueError("absent current request cannot claim a cutoff")
        if self.outcome is ConversationSnapshotOutcome.CURRENT_REQUEST_ABSENT and (
            len(self.messages) != 1
            or self.prior_candidate_count != 0
            or self.prior_selected_count != 0
            or self.prior_budget_dropped_count != 0
            or self.prior_content_bytes != 0
        ):
            raise ValueError("absent current request must be a current-only snapshot")
        if (
            self.outcome is ConversationSnapshotOutcome.CURRENT_REQUEST_ABSENT
            and self.prior_message_limit_truncated
        ):
            raise ValueError("current-only snapshot cannot claim truncated history")
        if (
            self.prior_message_limit_truncated
            and self.prior_candidate_count != MAX_PRIOR_MESSAGES
        ):
            raise ValueError("message-limit truncation requires a full candidate window")
        if self.prior_selected_count != len(self.messages) - 1:
            raise ValueError("selected prior count differs from messages")
        if self.prior_candidate_count != (
            self.prior_selected_count + self.prior_budget_dropped_count
        ):
            raise ValueError("prior candidate counts do not reconcile")
        prior_bytes = sum(
            len(item.content.encode("utf-8")) for item in self.messages[:-1]
        )
        total_bytes = prior_bytes + len(self.messages[-1].content.encode("utf-8"))
        if self.prior_content_bytes != prior_bytes:
            raise ValueError("prior byte count differs from messages")
        if self.total_content_bytes != total_bytes:
            raise ValueError("total byte count differs from messages")
        if self.current_message_sha256 != _text_sha256(self.messages[-1].content):
            raise ValueError("current message hash mismatch")
        conversation_payload = [item.model_dump(mode="json") for item in self.messages]
        if self.conversation_sha256 != _sha256(conversation_payload):
            raise ValueError("conversation hash mismatch")
        payload = self.model_dump(mode="json", exclude={"snapshot_sha256"})
        if self.snapshot_sha256 != _sha256(payload):
            raise ValueError("snapshot manifest hash mismatch")
        return self

    def sanitized_report(self) -> dict[str, Any]:
        """Return operational counts without content-derived identifiers."""

        return {
            "contract_version": self.contract_version,
            "outcome": self.outcome.value,
            "prior_candidate_count": self.prior_candidate_count,
            "prior_selected_count": self.prior_selected_count,
            "prior_budget_dropped_count": self.prior_budget_dropped_count,
            "prior_message_limit_truncated": self.prior_message_limit_truncated,
            "prior_content_bytes": self.prior_content_bytes,
            "total_content_bytes": self.total_content_bytes,
        }


def create_conversation_snapshot_v1(
    *,
    actor: UUID,
    thread: UUID,
    request_id: str,
    outcome: ConversationSnapshotOutcome,
    current_log_id: UUID | None,
    cutoff: datetime | None,
    messages: tuple[ResponsePolicyConversationMessageV0_2, ...],
    candidate_count: int,
    dropped_count: int,
    message_limit_truncated: bool,
) -> ConversationSnapshotV1:
    prior_bytes = sum(len(item.content.encode("utf-8")) for item in messages[:-1])
    total_bytes = prior_bytes + len(messages[-1].content.encode("utf-8"))
    values: dict[str, Any] = {
        "contract_version": SNAPSHOT_VERSION,
        "authenticated_actor_user_id": actor,
        "thread_id": thread,
        "current_request_id": request_id,
        "outcome": outcome,
        "current_log_id": current_log_id,
        "cutoff_created_at": cutoff,
        "messages": messages,
        "prior_candidate_count": candidate_count,
        "prior_selected_count": len(messages) - 1,
        "prior_budget_dropped_count": dropped_count,
        "prior_message_limit_truncated": message_limit_truncated,
        "prior_content_bytes": prior_bytes,
        "total_content_bytes": total_bytes,
        "current_message_sha256": _text_sha256(messages[-1].content),
        "conversation_sha256": _sha256(
            [item.model_dump(mode="json") for item in messages]
        ),
    }
    serializable = ConversationSnapshotV1.model_construct(
        **values,
        snapshot_sha256="0" * 64,
    ).model_dump(mode="json", exclude={"snapshot_sha256"})
    return ConversationSnapshotV1(
        **values,
        snapshot_sha256=_sha256(serializable),
    )


def create_current_only_conversation_snapshot_v1(
    *,
    authenticated_actor_user_id: UUID,
    thread_id: UUID,
    current_request_id: str,
    current_message: str,
) -> ConversationSnapshotV1:
    """Create the safe no-history state when no bound transcript row exists."""

    if not isinstance(authenticated_actor_user_id, UUID) or not isinstance(
        thread_id, UUID
    ):
        raise ConversationSnapshotError("snapshot actor and thread must be UUIDs")
    if not is_valid_snapshot_request_id(current_request_id):
        raise ConversationSnapshotError("current request id is invalid")
    try:
        current = ResponsePolicyConversationMessageV0_2(
            role=ConversationRole.USER,
            content=current_message,
        )
    except Exception:
        raise ConversationSnapshotError("current message is invalid") from None
    if len(current_message.encode("utf-8")) > MAX_CURRENT_MESSAGE_BYTES:
        raise ConversationSnapshotError("current message exceeds snapshot budget")
    return create_conversation_snapshot_v1(
        actor=authenticated_actor_user_id,
        thread=thread_id,
        request_id=current_request_id,
        outcome=ConversationSnapshotOutcome.CURRENT_REQUEST_ABSENT,
        current_log_id=None,
        cutoff=None,
        messages=(current,),
        candidate_count=0,
        dropped_count=0,
        message_limit_truncated=False,
    )

__all__ = [
    "ASSISTANT_SOURCE",
    "ATTESTED_ASSISTANT_SOURCE",
    "ConversationSnapshotError",
    "ConversationSnapshotOutcome",
    "ConversationSnapshotV1",
    "MAX_CURRENT_MESSAGE_BYTES",
    "MAX_PRIOR_CONTENT_BYTES",
    "MAX_PRIOR_MESSAGES",
    "SNAPSHOT_VERSION",
    "USER_SOURCE",
    "VOICE_REALTIME_USER_SOURCE",
    "create_conversation_snapshot_v1",
    "create_current_only_conversation_snapshot_v1",
    "is_valid_snapshot_request_id",
    "normalize_snapshot_timestamp",
]
