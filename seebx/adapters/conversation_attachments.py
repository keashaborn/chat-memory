from __future__ import annotations

"""PostgreSQL reads for owner-bound conversation attachments."""

from typing import Any, Protocol, Sequence
from uuid import UUID


OWNER_CONTEXT_SQL = "SELECT set_config('app.user_id', $1, false)"
READY_MESSAGE_ATTACHMENTS_SQL = """
SELECT attachment.id,attachment.filename,attachment.media_type,
       attachment.content,attachment.content_sha256,attachment.byte_size
FROM public.chat_attachments AS attachment
JOIN public.chat_log AS message
  ON message.id=attachment.message_id
 AND message.owner_user_id=attachment.owner_user_id
 AND message.thread_id=attachment.thread_id
WHERE attachment.owner_user_id=$1
  AND attachment.thread_id=$2
  AND attachment.message_id=$3
  AND attachment.id=ANY($4::uuid[])
  AND attachment.status='ready'
  AND attachment.deleted_at IS NULL
  AND attachment.content IS NOT NULL
ORDER BY array_position($4::uuid[], attachment.id)
"""


class AttachmentReadConnection(Protocol):
    async def execute(self, query: str, *args: object) -> str: ...

    async def fetch(self, query: str, *args: object) -> Sequence[Any]: ...


async def fetch_ready_message_attachments(
    connection: AttachmentReadConnection,
    *,
    owner_user_id: UUID,
    thread_id: UUID,
    message_id: UUID,
    attachment_ids: Sequence[UUID],
) -> tuple[Any, ...]:
    """Return only ready, intact attachments in caller-declared order."""

    await connection.execute(OWNER_CONTEXT_SQL, str(owner_user_id))
    rows = await connection.fetch(
        READY_MESSAGE_ATTACHMENTS_SQL,
        owner_user_id,
        thread_id,
        message_id,
        list(attachment_ids),
    )
    return tuple(rows)


__all__ = [
    "AttachmentReadConnection",
    "OWNER_CONTEXT_SQL",
    "READY_MESSAGE_ATTACHMENTS_SQL",
    "fetch_ready_message_attachments",
]
