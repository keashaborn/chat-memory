from __future__ import annotations

"""Owner-scoped, request-cutoff transcript snapshot for response policy.

The Verbal Sage flow records the user turn before calling Brains. This reader
binds history to that exact request row and uses its ``(created_at, id)`` as the
cutoff. User rows retain user authority. Assistant rows are admitted only when
they were written by the backend and have an exact append-only attestation.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rag_engine.response_policy_v0_2 import (
    ConversationRole,
    ResponsePolicyConversationMessageV0_2,
)


SNAPSHOT_VERSION = "response_conversation_snapshot_v1"
USER_SOURCE = "frontend/chat:user"
ASSISTANT_SOURCE = "frontend/chat:assistant"
ATTESTED_ASSISTANT_SOURCE = "backend/resse:assistant:v1"
SOURCE_ROLE = {
    USER_SOURCE: ConversationRole.USER,
    ATTESTED_ASSISTANT_SOURCE: ConversationRole.ASSISTANT,
}

MAX_PRIOR_MESSAGES = 23
MAX_CURRENT_MESSAGE_BYTES = 32_768
MAX_PRIOR_CONTENT_BYTES = 48_000
MAX_TOTAL_CONTENT_BYTES = 80_768
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")


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


def _utc(value: Any, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
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
        return _utc(value, "cutoff_created_at") if value is not None else None

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


def _snapshot(
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
    if not isinstance(current_request_id, str) or not _REQUEST_ID_RE.fullmatch(
        current_request_id
    ):
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
    return _snapshot(
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


async def load_response_conversation_snapshot_v1(
    conn: Any,
    *,
    authenticated_actor_user_id: UUID,
    thread_id: UUID,
    current_request_id: str,
    current_message: str,
) -> ConversationSnapshotV1:
    """Load a fixed snapshot using an already authenticated DB connection."""

    if not isinstance(authenticated_actor_user_id, UUID) or not isinstance(thread_id, UUID):
        raise ConversationSnapshotError("snapshot actor and thread must be UUIDs")
    if not isinstance(current_request_id, str) or not _REQUEST_ID_RE.fullmatch(
        current_request_id
    ):
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

    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            await conn.execute(
                "SELECT set_config('app.user_id',$1,true)",
                str(authenticated_actor_user_id),
            )
            role = str(await conn.fetchval("SELECT current_user"))
            read_only = str(
                await conn.fetchval(
                    "SELECT current_setting('transaction_read_only')"
                )
            )
            if role != "brains_app" or read_only != "on":
                raise ConversationSnapshotError(
                    "snapshot requires brains_app in a read-only transaction"
                )
            owns_thread = bool(
                await conn.fetchval(
                    """
                    SELECT EXISTS(
                      SELECT 1 FROM public.threads
                      WHERE owner_user_id=$1 AND id=$2
                    )
                    """,
                    authenticated_actor_user_id,
                    thread_id,
                )
            )
            if not owns_thread:
                raise ConversationSnapshotError("owner thread is absent")
            current_rows = list(
                await conn.fetch(
                    """
                    SELECT id,owner_user_id,thread_id,source,request_id,text,created_at
                    FROM public.chat_log
                    WHERE owner_user_id=$1
                      AND thread_id=$2
                      AND request_id=$3
                      AND source=$4
                    ORDER BY created_at,id
                    LIMIT 2
                    """,
                    authenticated_actor_user_id,
                    thread_id,
                    current_request_id,
                    USER_SOURCE,
                )
            )
            if not current_rows:
                return create_current_only_conversation_snapshot_v1(
                    authenticated_actor_user_id=authenticated_actor_user_id,
                    thread_id=thread_id,
                    current_request_id=current_request_id,
                    current_message=current_message,
                )
            if len(current_rows) != 1:
                raise ConversationSnapshotError(
                    "current request has multiple user transcript rows"
                )
            current_row = dict(current_rows[0])
            if UUID(str(current_row.get("owner_user_id"))) != authenticated_actor_user_id:
                raise ConversationSnapshotError(
                    "current transcript row differs from authenticated owner"
                )
            if UUID(str(current_row.get("thread_id"))) != thread_id:
                raise ConversationSnapshotError(
                    "current transcript row differs from trusted thread"
                )
            if (
                current_row.get("source") != USER_SOURCE
                or current_row.get("request_id") != current_request_id
            ):
                raise ConversationSnapshotError(
                    "current transcript row differs from trusted request binding"
                )
            if not isinstance(current_row.get("text"), str) or current_row["text"] != current_message:
                raise ConversationSnapshotError(
                    "current transcript row differs from request message"
                )
            current_log_id = UUID(str(current_row["id"]))
            cutoff = _utc(current_row["created_at"], "current created_at")
            prior_rows = list(
                await conn.fetch(
                    """
                    SELECT log.id,log.owner_user_id,log.thread_id,log.source,
                           log.text,log.request_id,log.created_at,
                           attestation.assistant_text_sha256,
                           attestation.attestation_sha256
                    FROM public.chat_log AS log
                    LEFT JOIN memory.assistant_transcript_attestation_v1 AS attestation
                      ON attestation.owner_user_id=log.owner_user_id
                     AND attestation.thread_id=log.thread_id
                     AND attestation.chat_log_id=log.id
                     AND attestation.answer_id=log.id
                    WHERE log.owner_user_id=$1
                      AND log.thread_id=$2
                      AND log.request_id IS DISTINCT FROM $3
                      AND log.request_id IS NOT NULL
                      AND (
                        log.source=$4
                        OR (
                          log.source=$5
                          AND attestation.answer_id IS NOT NULL
                        )
                      )
                      AND (log.created_at,log.id)<($6,$7)
                    ORDER BY log.created_at DESC,log.id DESC
                    LIMIT $8
                    """,
                    authenticated_actor_user_id,
                    thread_id,
                    current_request_id,
                    USER_SOURCE,
                    ATTESTED_ASSISTANT_SOURCE,
                    cutoff,
                    current_log_id,
                    MAX_PRIOR_MESSAGES + 1,
                )
            )
    except ConversationSnapshotError:
        raise
    except Exception:
        raise ConversationSnapshotError("conversation snapshot read failed") from None

    message_limit_truncated = len(prior_rows) > MAX_PRIOR_MESSAGES
    prior_rows = prior_rows[:MAX_PRIOR_MESSAGES]
    newest_selected: list[ResponsePolicyConversationMessageV0_2] = []
    prior_bytes = 0
    dropped = 0
    seen_row_ids: set[UUID] = set()
    prior_order_key: tuple[datetime, int] | None = None
    for index, raw in enumerate(prior_rows):
        row = dict(raw)
        try:
            row_id = UUID(str(row.get("id")))
            owner = UUID(str(row.get("owner_user_id")))
            row_thread = UUID(str(row.get("thread_id")))
            created_at = _utc(row.get("created_at"), "prior created_at")
        except Exception:
            raise ConversationSnapshotError(
                "prior transcript row binding is invalid"
            ) from None
        if row_id in seen_row_ids:
            raise ConversationSnapshotError("prior transcript row id is duplicated")
        seen_row_ids.add(row_id)
        order_key = (created_at, row_id.int)
        if prior_order_key is not None and order_key >= prior_order_key:
            raise ConversationSnapshotError(
                "prior transcript rows are not strictly descending"
            )
        prior_order_key = order_key
        if order_key >= (cutoff, current_log_id.int):
            raise ConversationSnapshotError(
                "prior transcript row is outside the request cutoff"
            )
        if owner != authenticated_actor_user_id or row_thread != thread_id:
            raise ConversationSnapshotError(
                "prior transcript row differs from trusted owner or thread"
            )
        source = str(row.get("source") or "")
        role = SOURCE_ROLE.get(source)
        if role is None:
            raise ConversationSnapshotError("transcript source is not allowlisted")
        prior_request_id = row.get("request_id")
        if (
            not isinstance(prior_request_id, str)
            or not _REQUEST_ID_RE.fullmatch(prior_request_id)
            or prior_request_id == current_request_id
        ):
            raise ConversationSnapshotError("current request leaked into prior history")
        text = row.get("text")
        if not isinstance(text, str) or not text:
            raise ConversationSnapshotError("prior transcript text is empty")
        if source == ATTESTED_ASSISTANT_SOURCE:
            expected_text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if row.get("assistant_text_sha256") != expected_text_hash:
                raise ConversationSnapshotError(
                    "assistant transcript differs from its attestation"
                )
            attestation_hash = str(row.get("attestation_sha256") or "")
            if len(attestation_hash) != 64 or any(
                char not in "0123456789abcdef" for char in attestation_hash
            ):
                raise ConversationSnapshotError("assistant attestation is invalid")
        size = len(text.encode("utf-8"))
        if size > MAX_CURRENT_MESSAGE_BYTES:
            raise ConversationSnapshotError("one prior message exceeds snapshot budget")
        if prior_bytes + size > MAX_PRIOR_CONTENT_BYTES:
            # History is selected as one contiguous newest-first window.  Once
            # the budget is exhausted, do not skip a newer turn to admit an
            # older, smaller turn; that would create misleading context gaps.
            dropped += len(prior_rows) - index
            break
        newest_selected.append(
            ResponsePolicyConversationMessageV0_2(role=role, content=text)
        )
        prior_bytes += size
    messages = tuple(reversed(newest_selected)) + (current,)
    return _snapshot(
        actor=authenticated_actor_user_id,
        thread=thread_id,
        request_id=current_request_id,
        outcome=ConversationSnapshotOutcome.CURRENT_REQUEST_BOUND,
        current_log_id=current_log_id,
        cutoff=cutoff,
        messages=messages,
        candidate_count=len(prior_rows),
        dropped_count=dropped,
        message_limit_truncated=message_limit_truncated,
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
    "create_current_only_conversation_snapshot_v1",
    "load_response_conversation_snapshot_v1",
]
