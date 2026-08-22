from __future__ import annotations

"""PostgreSQL read boundary for owner-bound conversation history."""

from typing import Any, Protocol, Sequence
from uuid import UUID


THREAD_MESSAGE_ROWS_SQL = """
SELECT log.id,log.source,log.text,log.created_at,
       web.cited_sources,web.admitted_sources,
       COALESCE(attachment_set.attachments,'[]'::jsonb) AS attachments
FROM chat_log AS log
LEFT JOIN trusted_web.response_transcript_v1 AS web
  ON web.owner_user_id=log.owner_user_id
 AND web.thread_id=log.thread_id
 AND web.assistant_chat_log_id=log.id
LEFT JOIN LATERAL (
  SELECT jsonb_agg(
           jsonb_build_object(
             'id',attachment.id,
             'filename',attachment.filename,
             'media_type',attachment.media_type,
             'content_sha256',attachment.content_sha256,
             'byte_size',attachment.byte_size,
             'processing_status',attachment.status,
             'deleted_at',attachment.deleted_at
           ) ORDER BY attachment.created_at,attachment.id
         ) AS attachments
  FROM conversation.chat_attachments AS attachment
  WHERE attachment.owner_user_id=log.owner_user_id
    AND attachment.thread_id=log.thread_id
    AND attachment.message_id=log.id
) AS attachment_set ON TRUE
WHERE log.owner_user_id=$1 AND log.thread_id=$2
ORDER BY log.created_at ASC
LIMIT $3
"""


class ConversationHistoryReadConnection(Protocol):
    async def fetch(self, query: str, *args: object) -> Sequence[Any]: ...


async def fetch_thread_message_rows(
    connection: ConversationHistoryReadConnection,
    *,
    owner_user_id: str | UUID,
    thread_id: UUID,
    limit: int,
) -> tuple[Any, ...]:
    """Return an owner's ordered message, web, and attachment projections."""

    rows = await connection.fetch(
        THREAD_MESSAGE_ROWS_SQL,
        owner_user_id,
        thread_id,
        limit,
    )
    return tuple(rows)


__all__ = [
    "ConversationHistoryReadConnection",
    "THREAD_MESSAGE_ROWS_SQL",
    "fetch_thread_message_rows",
]
