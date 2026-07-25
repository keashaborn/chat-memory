from __future__ import annotations

"""Atomic conversation-only persistence for externally sourced answers."""

import hashlib
import json
from typing import Any
from uuid import UUID, uuid4


WEB_USER_SOURCE = "backend/web:user:v1"
WEB_ASSISTANT_SOURCE = "backend/web:assistant:v1"


class WebTranscriptPersistenceError(RuntimeError):
    pass


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def persist_web_exchange_v1(
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
                _sha256(query),
                _sha256(answer),
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
    except Exception:
        raise WebTranscriptPersistenceError(
            "web transcript persistence failed"
        ) from None
    return answer_id


__all__ = [
    "WEB_ASSISTANT_SOURCE",
    "WEB_USER_SOURCE",
    "WebTranscriptPersistenceError",
    "persist_web_exchange_v1",
]
