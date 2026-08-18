from __future__ import annotations

"""Atomic PostgreSQL persistence for conversation responses and evidence."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence
from uuid import UUID, uuid4

import asyncpg

from seebx.adapters.conversation_attachments import (
    bind_attachments_to_message,
    fetch_attachment_bindings,
)
from seebx.adapters.thread_selection import promote_resume_thread_v1
from seebx.adapters.transcript_integrity import (
    insert_assistant_transcript_attestation_v1,
)
from seebx.capabilities.conversation.persistence import (
    ConversationPersistenceError,
    FinalizedConversationResponse,
    SearchTranscriptPersistenceError,
    normalize_finalized_response,
)
from seebx.contracts.conversation import WEB_ASSISTANT_SOURCE, WEB_USER_SOURCE
from seebx.contracts.transcript_integrity import (
    ATTESTED_ASSISTANT_SOURCE,
    text_sha256,
)


FETCH_USER_TRANSCRIPT_THREAD_SQL = (
    "SELECT owner_user_id FROM threads WHERE id=$1 AND owner_user_id=$2"
)
CREATE_USER_TRANSCRIPT_THREAD_SQL = (
    "INSERT INTO threads(id, owner_user_id, user_id, title) "
    "VALUES($1, $2, $3, $4)"
)
FETCH_ATTACHMENT_REPLAY_SQL = """
SELECT id FROM public.chat_log
WHERE id=$1 AND owner_user_id=$2 AND thread_id=$3
  AND source=$4 AND text=$5
"""
FETCH_EXACT_SUBMISSION_SQL = """
SELECT id FROM public.chat_log
WHERE id=$1 AND owner_user_id=$2
  AND thread_id IS NOT DISTINCT FROM $3
  AND source=$4 AND text=$5
"""
FETCH_CONFLICTING_SUBMISSION_SQL = """
SELECT id FROM public.chat_log
WHERE id=$1 AND owner_user_id=$2
"""
INSERT_USER_TRANSCRIPT_SQL = (
    "INSERT INTO chat_log("
    "id,owner_user_id,user_id,user_id_alias,source,text,tags,thread_id,"
    "vantage_id,request_id,created_at"
    ") VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)"
)
TOUCH_USER_TRANSCRIPT_THREAD_SQL = (
    "UPDATE threads SET updated_at=now() WHERE id=$1 AND owner_user_id=$2"
)


@dataclass(frozen=True)
class UserTranscriptPersistenceResult:
    message_id: UUID
    replayed: bool


class UserTranscriptPersistenceError(RuntimeError):
    def __init__(self, code: str, *, conflict: bool) -> None:
        super().__init__(code)
        self.code = code
        self.conflict = conflict


async def persist_user_transcript(
    conn: Any,
    *,
    owner_user_id: UUID,
    user_id_alias: str,
    source: str,
    text: str,
    tags: list[Any],
    thread_id: UUID | None,
    vantage_id: str,
    request_id: str,
    message_id: UUID,
    submission_id: UUID | None,
    created_at: datetime,
    attachment_ids: Sequence[UUID],
) -> UserTranscriptPersistenceResult:
    """Persist one user transcript row and attachment bindings atomically."""

    owner = str(owner_user_id)
    effective_thread_id = thread_id
    transaction: Any | None = None
    try:
        transaction = conn.transaction()
        await transaction.start()

        if effective_thread_id:
            thread_row = await conn.fetchrow(
                FETCH_USER_TRANSCRIPT_THREAD_SQL,
                effective_thread_id,
                owner,
            )
            if thread_row is None:
                await conn.execute(
                    CREATE_USER_TRANSCRIPT_THREAD_SQL,
                    effective_thread_id,
                    owner,
                    owner,
                    "New chat",
                )
            elif str(thread_row["owner_user_id"] or "") != owner:
                effective_thread_id = None

        if attachment_ids:
            attachment_rows = await fetch_attachment_bindings(
                conn,
                owner_user_id=owner_user_id,
                thread_id=effective_thread_id,
                attachment_ids=attachment_ids,
            )
            if (
                len(attachment_rows) != len(attachment_ids)
                or any(
                    row["status"] != "ready" or row["deleted_at"] is not None
                    for row in attachment_rows
                )
            ):
                raise ValueError("attachment_binding_failed")
            bound_message_ids = {row["message_id"] for row in attachment_rows}
            if None not in bound_message_ids:
                if len(bound_message_ids) != 1:
                    raise ValueError("attachment_binding_failed")
                existing_message_id = next(iter(bound_message_ids))
                existing_message = await conn.fetchrow(
                    FETCH_ATTACHMENT_REPLAY_SQL,
                    existing_message_id,
                    owner_user_id,
                    effective_thread_id,
                    source,
                    text,
                )
                if existing_message is None:
                    raise ValueError("attachment_binding_failed")
                await transaction.commit()
                transaction = None
                return UserTranscriptPersistenceResult(
                    message_id=UUID(str(existing_message["id"])),
                    replayed=True,
                )
            if bound_message_ids != {None}:
                raise ValueError("attachment_binding_failed")

        if submission_id is not None:
            existing_submission = await conn.fetchrow(
                FETCH_EXACT_SUBMISSION_SQL,
                submission_id,
                owner_user_id,
                effective_thread_id,
                source,
                text,
            )
            if existing_submission is not None:
                await transaction.commit()
                transaction = None
                return UserTranscriptPersistenceResult(
                    message_id=UUID(str(existing_submission["id"])),
                    replayed=True,
                )
            conflicting_submission = await conn.fetchrow(
                FETCH_CONFLICTING_SUBMISSION_SQL,
                submission_id,
                owner_user_id,
            )
            if conflicting_submission is not None:
                raise ValueError("submission_id_conflict")

        await conn.execute(
            INSERT_USER_TRANSCRIPT_SQL,
            str(message_id),
            owner,
            owner,
            user_id_alias,
            source,
            text,
            tags,
            effective_thread_id,
            vantage_id,
            request_id,
            created_at,
        )

        if attachment_ids:
            bound_rows = await bind_attachments_to_message(
                conn,
                message_id=message_id,
                owner_user_id=owner_user_id,
                thread_id=effective_thread_id,
                attachment_ids=attachment_ids,
            )
            if len(bound_rows) != len(attachment_ids):
                raise ValueError("attachment_binding_failed")

        if effective_thread_id:
            await conn.execute(
                TOUCH_USER_TRANSCRIPT_THREAD_SQL,
                effective_thread_id,
                owner,
            )

        await transaction.commit()
        transaction = None
        return UserTranscriptPersistenceResult(
            message_id=message_id,
            replayed=False,
        )
    except Exception as exc:
        if transaction is not None:
            try:
                await transaction.rollback()
            except Exception:
                pass
        conflict = str(exc) in {
            "attachment_binding_failed",
            "submission_id_conflict",
        } or (
            submission_id is not None
            and isinstance(exc, asyncpg.UniqueViolationError)
        )
        detail = (
            "attachment_binding_failed"
            if str(exc) == "attachment_binding_failed"
            else "submission_conflict"
            if conflict
            else "transcript_write_failed"
        )
        raise UserTranscriptPersistenceError(
            detail,
            conflict=conflict,
        ) from exc


async def persist_conversation_response(
    conn: object,
    *,
    owner_user_id: UUID,
    thread_id: UUID,
    request_id: str,
    finalized: FinalizedConversationResponse,
) -> None:
    """Persist one exact finalized response through the shared transaction."""

    stage = "validation"
    try:
        value = normalize_finalized_response(
            finalized,
            owner_user_id=owner_user_id,
        )
        attestation = value.attestation
        if attestation.authenticated_actor_user_id != owner_user_id:
            raise ValueError("attestation owner mismatch")
        if attestation.thread_id != thread_id:
            raise ValueError("attestation thread mismatch")
        if attestation.request_id_sha256 != text_sha256(request_id):
            raise ValueError("request differs from attestation")

        stage = "transaction"
        async with conn.transaction():
            stage = "actor_scope"
            await conn.execute(
                "SELECT set_config('app.user_id',$1,true)",
                str(owner_user_id),
            )
            stage = "thread_owner_check"
            owns_thread = await conn.fetchval(
                """
                SELECT EXISTS(
                  SELECT 1 FROM public.threads
                  WHERE owner_user_id=$1 AND id=$2
                )
                """,
                owner_user_id,
                thread_id,
            )
            if not owns_thread:
                raise ValueError("owner thread is absent")

            stage = "chat_log_insert"
            await conn.execute(
                """
                INSERT INTO public.chat_log(
                  id,owner_user_id,user_id,user_id_alias,source,text,tags,
                  thread_id,vantage_id,request_id,created_at
                )
                VALUES($1,$2,$3,NULL,$4,$5,$6,$7,'RESSE',$8,$9)
                """,
                value.answer_id,
                owner_user_id,
                str(owner_user_id),
                ATTESTED_ASSISTANT_SOURCE,
                value.assistant_text,
                list(value.tags),
                thread_id,
                request_id,
                attestation.created_at,
            )
            stage = "attestation_insert"
            await insert_assistant_transcript_attestation_v1(conn, attestation)

            stage = "thread_touch"
            await conn.execute(
                "UPDATE public.threads SET updated_at=now() "
                "WHERE owner_user_id=$1 AND id=$2",
                owner_user_id,
                thread_id,
            )
            stage = "resume_target_promotion"
            await promote_resume_thread_v1(
                conn,
                owner_user_id=owner_user_id,
                thread_id=thread_id,
            )
    except ConversationPersistenceError:
        raise
    except Exception:
        raise ConversationPersistenceError(stage) from None




async def persist_search_exchange(
    conn: Any,
    *,
    owner_user_id: UUID,
    thread_id: UUID,
    request_id: str,
    query: str,
    answer: str,
    search_id: UUID,
    route: str,
    policy_version: str,
    decision: str,
    cited_sources: list[dict[str, Any]],
    admitted_sources: list[dict[str, Any]],
    consulted_source_count: int,
) -> UUID:
    """Persist a search exchange and its exact evidence receipt atomically."""

    user_log_id = uuid4()
    answer_id = uuid4()
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.user_id',$1,true)",
                str(owner_user_id),
            )
            owns_thread = await conn.fetchval(
                """
                SELECT EXISTS(
                  SELECT 1 FROM public.threads
                  WHERE owner_user_id=$1 AND id=$2
                )
                """,
                owner_user_id,
                thread_id,
            )
            if not owns_thread:
                raise ValueError("owner thread is absent")
            await conn.execute(
                """
                INSERT INTO public.chat_log(
                  id,owner_user_id,user_id,user_id_alias,source,text,tags,
                  thread_id,vantage_id,request_id,created_at
                )
                VALUES
                  ($1,$2,$3,NULL,$4,$5,$6,$7,'default',$8,clock_timestamp()),
                  (
                    $9,$2,$3,NULL,$10,$11,$12,$7,'default',$8,
                    clock_timestamp() + interval '1 microsecond'
                  )
                """,
                user_log_id,
                owner_user_id,
                str(owner_user_id),
                WEB_USER_SOURCE,
                query,
                ["user", "chat", "web_search", "memory_ineligible"],
                thread_id,
                request_id,
                answer_id,
                WEB_ASSISTANT_SOURCE,
                answer,
                ["assistant", "chat", "web_search", "memory_ineligible"],
            )
            await conn.execute(
                """
                INSERT INTO trusted_web.response_transcript_v1(
                  response_id,owner_user_id,thread_id,user_chat_log_id,
                  assistant_chat_log_id,request_id,search_id,route,
                  policy_version,decision,query_sha256,answer_sha256,
                  cited_sources,admitted_sources,consulted_source_count,
                  created_at
                )
                VALUES(
                  $1,$2,$3,$4,$1,$5,$6,$7,$8,$9,$10,$11,
                  $12::jsonb,$13::jsonb,$14,clock_timestamp()
                )
                """,
                answer_id,
                owner_user_id,
                thread_id,
                user_log_id,
                request_id,
                search_id,
                route,
                policy_version,
                decision,
                text_sha256(query),
                text_sha256(answer),
                json.dumps(cited_sources, separators=(",", ":"), sort_keys=True),
                json.dumps(admitted_sources, separators=(",", ":"), sort_keys=True),
                consulted_source_count,
            )
            await conn.execute(
                """
                UPDATE public.threads
                SET updated_at=clock_timestamp()
                WHERE owner_user_id=$1 AND id=$2
                """,
                owner_user_id,
                thread_id,
            )
            await promote_resume_thread_v1(
                conn,
                owner_user_id=owner_user_id,
                thread_id=thread_id,
            )
    except Exception:
        raise SearchTranscriptPersistenceError(
            "search transcript persistence failed"
        ) from None
    return answer_id


__all__ = [
    "ConversationPersistenceError",
    "SearchTranscriptPersistenceError",
    "UserTranscriptPersistenceError",
    "UserTranscriptPersistenceResult",
    "persist_conversation_response",
    "persist_search_exchange",
    "persist_user_transcript",
]
