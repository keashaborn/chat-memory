from __future__ import annotations

"""PostgreSQL adapter for immutable conversation snapshots."""

import hashlib
from datetime import datetime
from typing import Any
from uuid import UUID

from seebx.contracts.transcript_integrity import (
    ATTESTED_ASSISTANT_SOURCE,
    ATTESTED_ASSISTANT_SOURCES,
    validate_assistant_transcript_attestation_row_v1,
)
from seebx.capabilities.conversation.policy import (
    ConversationRole,
    ResponsePolicyConversationMessageV0_2,
)
from seebx.capabilities.conversation.snapshot import (
    MAX_CURRENT_MESSAGE_BYTES,
    MAX_PRIOR_CONTENT_BYTES,
    MAX_PRIOR_MESSAGES,
    USER_SOURCE,
    VOICE_REALTIME_USER_SOURCE,
    ConversationSnapshotError,
    ConversationSnapshotOutcome,
    ConversationSnapshotV1,
    create_conversation_snapshot_v1,
    create_current_only_conversation_snapshot_v1,
    is_valid_snapshot_request_id,
    normalize_snapshot_timestamp,
)
from seebx.contracts.conversation import WEB_ASSISTANT_SOURCE, WEB_USER_SOURCE


SOURCE_ROLE = {
    USER_SOURCE: ConversationRole.USER,
    VOICE_REALTIME_USER_SOURCE: ConversationRole.USER,
    **{
        source: ConversationRole.ASSISTANT for source in ATTESTED_ASSISTANT_SOURCES
    },
    WEB_USER_SOURCE: ConversationRole.USER,
    WEB_ASSISTANT_SOURCE: ConversationRole.ASSISTANT,
}


async def load_conversation_snapshot_v1(
    conn: Any,
    *,
    authenticated_actor_user_id: UUID,
    thread_id: UUID,
    current_request_id: str,
    current_message: str,
) -> ConversationSnapshotV1:
    """Load a fixed snapshot using an already authenticated DB connection."""

    current_only = create_current_only_conversation_snapshot_v1(
        authenticated_actor_user_id=authenticated_actor_user_id,
        thread_id=thread_id,
        current_request_id=current_request_id,
        current_message=current_message,
    )
    current = current_only.messages[-1]

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
                      AND source=ANY($4::text[])
                    ORDER BY created_at,id
                    LIMIT 2
                    """,
                    authenticated_actor_user_id,
                    thread_id,
                    current_request_id,
                    [USER_SOURCE, VOICE_REALTIME_USER_SOURCE],
                )
            )
            if not current_rows:
                return current_only
            if len(current_rows) != 1:
                raise ConversationSnapshotError(
                    "current request has multiple user transcript rows"
                )
            current_row = dict(current_rows[0])
            if (
                UUID(str(current_row.get("owner_user_id")))
                != authenticated_actor_user_id
            ):
                raise ConversationSnapshotError(
                    "current transcript row differs from authenticated owner"
                )
            if UUID(str(current_row.get("thread_id"))) != thread_id:
                raise ConversationSnapshotError(
                    "current transcript row differs from trusted thread"
                )
            if (
                current_row.get("source")
                not in {USER_SOURCE, VOICE_REALTIME_USER_SOURCE}
                or current_row.get("request_id") != current_request_id
            ):
                raise ConversationSnapshotError(
                    "current transcript row differs from trusted request binding"
                )
            if (
                not isinstance(current_row.get("text"), str)
                or current_row["text"] != current_message
            ):
                raise ConversationSnapshotError(
                    "current transcript row differs from request message"
                )
            current_log_id = UUID(str(current_row["id"]))
            cutoff = normalize_snapshot_timestamp(
                current_row["created_at"], "current created_at"
            )
            prior_rows = list(
                await conn.fetch(
                    """
                    SELECT log.id,log.owner_user_id,log.thread_id,log.source,
                           log.text,log.request_id,log.created_at,
                           attestation.answer_id AS attestation_answer_id,
                           attestation.owner_user_id AS attestation_owner_user_id,
                           attestation.thread_id AS attestation_thread_id,
                           attestation.chat_log_id AS attestation_chat_log_id,
                           attestation.request_id_sha256 AS attestation_request_id_sha256,
                           attestation.conversation_snapshot_sha256
                             AS attestation_conversation_snapshot_sha256,
                           attestation.trusted_plan_sha256
                             AS attestation_trusted_plan_sha256,
                           attestation.provider_request_sha256
                             AS attestation_provider_request_sha256,
                           attestation.provider_response_sha256
                             AS attestation_provider_response_sha256,
                           attestation.provider_response_id
                             AS attestation_provider_response_id,
                           attestation.output_kind AS attestation_output_kind,
                           attestation.assistant_text_sha256
                             AS attestation_assistant_text_sha256,
                           attestation.attestation_sha256,
                           attestation.created_at AS attestation_created_at,
                           web_binding.response_id AS web_response_id,
                           web_binding.query_sha256 AS web_query_sha256,
                           web_binding.answer_sha256 AS web_answer_sha256
                    FROM public.chat_log AS log
                    LEFT JOIN chat_integrity.assistant_transcript_attestation_v1
                      AS attestation
                      ON attestation.owner_user_id=log.owner_user_id
                     AND attestation.thread_id=log.thread_id
                     AND attestation.chat_log_id=log.id
                     AND attestation.answer_id=log.id
                    LEFT JOIN trusted_web.response_transcript_v1 AS web_binding
                      ON web_binding.owner_user_id=log.owner_user_id
                     AND web_binding.thread_id=log.thread_id
                     AND (
                       web_binding.user_chat_log_id=log.id
                       OR web_binding.assistant_chat_log_id=log.id
                     )
                    WHERE log.owner_user_id=$1
                      AND log.thread_id=$2
                      AND log.request_id IS DISTINCT FROM $3
                      AND log.request_id IS NOT NULL
                      AND (
                        log.source=ANY($4::text[])
                        OR (
                          log.source=ANY($5::text[])
                          AND attestation.answer_id IS NOT NULL
                        )
                        OR (
                          log.source IN ($6,$7)
                          AND web_binding.response_id IS NOT NULL
                        )
                      )
                      AND (log.created_at,log.id)<($8,$9)
                    ORDER BY log.created_at DESC,log.id DESC
                    LIMIT $10
                    """,
                    authenticated_actor_user_id,
                    thread_id,
                    current_request_id,
                    [USER_SOURCE, VOICE_REALTIME_USER_SOURCE],
                    sorted(ATTESTED_ASSISTANT_SOURCES),
                    WEB_USER_SOURCE,
                    WEB_ASSISTANT_SOURCE,
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
            created_at = normalize_snapshot_timestamp(
                row.get("created_at"), "prior created_at"
            )
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
            or not is_valid_snapshot_request_id(prior_request_id)
            or prior_request_id == current_request_id
        ):
            raise ConversationSnapshotError("current request leaked into prior history")
        text = row.get("text")
        if not isinstance(text, str) or not text:
            raise ConversationSnapshotError("prior transcript text is empty")
        if source in ATTESTED_ASSISTANT_SOURCES:
            try:
                validate_assistant_transcript_attestation_row_v1(
                    row,
                    owner_user_id=owner,
                    thread_id=row_thread,
                    chat_log_id=row_id,
                    request_id=prior_request_id,
                    assistant_text=text,
                    chat_created_at=created_at,
                )
            except Exception:
                raise ConversationSnapshotError(
                    "assistant transcript differs from its attestation"
                ) from None
        elif source in {WEB_USER_SOURCE, WEB_ASSISTANT_SOURCE}:
            if row.get("web_response_id") is None:
                raise ConversationSnapshotError(
                    "web transcript binding is absent"
                )
            expected_text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            bound_hash = (
                row.get("web_query_sha256")
                if source == WEB_USER_SOURCE
                else row.get("web_answer_sha256")
            )
            if bound_hash != expected_text_hash:
                raise ConversationSnapshotError(
                    "web transcript differs from its binding"
                )
        size = len(text.encode("utf-8"))
        if size > MAX_CURRENT_MESSAGE_BYTES:
            raise ConversationSnapshotError(
                "one prior message exceeds snapshot budget"
            )
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
    return create_conversation_snapshot_v1(
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

__all__ = ["load_conversation_snapshot_v1"]
