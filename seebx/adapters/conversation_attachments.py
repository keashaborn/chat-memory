from __future__ import annotations

"""PostgreSQL persistence for owner-bound conversation attachments."""

import os
from contextlib import asynccontextmanager
from typing import Any, Protocol, Sequence
from uuid import UUID

import asyncpg


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


CREATE_ATTACHMENT_SQL = """
INSERT INTO public.chat_attachments(
    id,owner_user_id,thread_id,filename,media_type,content,
    content_sha256,byte_size,status
)
SELECT $1,$2,thread.id,$4,$5,$6,$7,$8,'ready'
FROM public.threads AS thread
WHERE thread.id=$3 AND thread.owner_user_id=$2
RETURNING id,thread_id,filename,media_type,content_sha256,byte_size,
          status,created_at
"""
FETCH_ATTACHMENT_STATUS_SQL = """
SELECT id,thread_id,message_id,filename,media_type,content_sha256,
       byte_size,status,created_at,updated_at,deleted_at
FROM public.chat_attachments
WHERE id=$1 AND owner_user_id=$2
"""
FETCH_RETRY_CANDIDATE_SQL = """
SELECT id,content,content_sha256,byte_size
FROM public.chat_attachments
WHERE id=$1 AND owner_user_id=$2 AND deleted_at IS NULL
"""
UPDATE_RETRY_STATUS_SQL = """
UPDATE public.chat_attachments
SET status=$3,updated_at=now()
WHERE id=$1 AND owner_user_id=$2 AND deleted_at IS NULL
RETURNING id,status
"""
DELETE_ATTACHMENT_SQL = """
UPDATE public.chat_attachments
SET content=NULL,status='deleted',deleted_at=COALESCE(deleted_at,now()),
    updated_at=now()
WHERE id=$1 AND owner_user_id=$2
RETURNING id,deleted_at
"""


class AttachmentConnection(Protocol):
    async def execute(self, query: str, *args: object) -> str: ...

    async def fetchrow(self, query: str, *args: object) -> Any: ...

    async def close(self) -> None: ...


class ConversationAttachmentPostgresStore:
    def __init__(
        self,
        *,
        dsn: str,
        connect_factory: Any = asyncpg.connect,
    ) -> None:
        if not dsn.strip():
            raise ValueError("attachment PostgreSQL DSN is required")
        self._dsn = dsn
        self._connect_factory = connect_factory

    @classmethod
    def from_environment(cls) -> "ConversationAttachmentPostgresStore":
        return cls(dsn=os.environ["POSTGRES_DSN"])

    @asynccontextmanager
    async def owner_connection(self, owner_user_id: UUID):
        connection = await self._connect_factory(self._dsn)
        try:
            await connection.execute(
                OWNER_CONTEXT_SQL,
                str(owner_user_id),
            )
            yield connection
        finally:
            await connection.close()

    async def create_attachment(
        self,
        connection: AttachmentConnection,
        *,
        attachment_id: UUID,
        owner_user_id: UUID,
        thread_id: UUID,
        filename: str,
        media_type: str,
        content: str,
        content_sha256: str,
        byte_size: int,
    ) -> Any:
        return await connection.fetchrow(
            CREATE_ATTACHMENT_SQL,
            attachment_id,
            owner_user_id,
            thread_id,
            filename,
            media_type,
            content,
            content_sha256,
            byte_size,
        )

    async def fetch_attachment_status(
        self,
        connection: AttachmentConnection,
        *,
        attachment_id: UUID,
        owner_user_id: UUID,
    ) -> Any:
        return await connection.fetchrow(
            FETCH_ATTACHMENT_STATUS_SQL,
            attachment_id,
            owner_user_id,
        )

    async def fetch_retry_candidate(
        self,
        connection: AttachmentConnection,
        *,
        attachment_id: UUID,
        owner_user_id: UUID,
    ) -> Any:
        return await connection.fetchrow(
            FETCH_RETRY_CANDIDATE_SQL,
            attachment_id,
            owner_user_id,
        )

    async def update_retry_status(
        self,
        connection: AttachmentConnection,
        *,
        attachment_id: UUID,
        owner_user_id: UUID,
        status: str,
    ) -> Any:
        return await connection.fetchrow(
            UPDATE_RETRY_STATUS_SQL,
            attachment_id,
            owner_user_id,
            status,
        )

    async def delete_attachment(
        self,
        connection: AttachmentConnection,
        *,
        attachment_id: UUID,
        owner_user_id: UUID,
    ) -> Any:
        return await connection.fetchrow(
            DELETE_ATTACHMENT_SQL,
            attachment_id,
            owner_user_id,
        )


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
    "AttachmentConnection",
    "AttachmentReadConnection",
    "CREATE_ATTACHMENT_SQL",
    "ConversationAttachmentPostgresStore",
    "DELETE_ATTACHMENT_SQL",
    "FETCH_ATTACHMENT_STATUS_SQL",
    "FETCH_RETRY_CANDIDATE_SQL",
    "OWNER_CONTEXT_SQL",
    "READY_MESSAGE_ATTACHMENTS_SQL",
    "UPDATE_RETRY_STATUS_SQL",
    "fetch_ready_message_attachments",
]
