from __future__ import annotations

"""Atomic PostgreSQL persistence for conversation responses and evidence."""

import json
from typing import Any
from uuid import UUID, uuid4

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
    "persist_conversation_response",
    "persist_search_exchange",
]
