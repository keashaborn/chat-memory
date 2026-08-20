from __future__ import annotations

"""PostgreSQL boundary for owner-scoped conversation thread metadata."""

from typing import Any, Protocol, Sequence
from uuid import UUID

from seebx.adapters.thread_selection import promote_resume_thread_v1


THREAD_BELONGS_TO_OWNER_SQL = (
    "SELECT 1 FROM threads WHERE id=$1 AND owner_user_id=$2"
)
CREATE_THREAD_SQL = (
    "INSERT INTO threads(owner_user_id, user_id, title) "
    "VALUES ($1,$2,$3) RETURNING id, title, updated_at"
)
LIST_VISIBLE_THREADS_SQL = """
SELECT id,
       title,
       updated_at,
       pinned_at,
       pinned_at IS NOT NULL AS pinned
FROM threads
WHERE owner_user_id=$1
  AND archived=false
ORDER BY (pinned_at IS NOT NULL) DESC,
         pinned_at DESC NULLS LAST,
         updated_at DESC
"""
RENAME_THREAD_MANUAL_SQL = """
UPDATE threads
SET title=$1, title_source='manual', updated_at=now()
WHERE owner_user_id=$2 AND id=$3
RETURNING title, title_source
"""
SET_THREAD_PINNED_SQL = """
UPDATE threads
SET pinned_at = CASE WHEN $1 THEN now() ELSE NULL END
WHERE owner_user_id=$2 AND id=$3
RETURNING pinned_at
"""
FETCH_THREAD_TITLE_STATE_SQL = """
SELECT title, title_source
FROM threads
WHERE owner_user_id=$1 AND id=$2
"""
FETCH_THREAD_TITLE_TRANSCRIPT_SQL = """
SELECT source, text, created_at, id
FROM chat_log
WHERE owner_user_id=$1 AND thread_id=$2
ORDER BY created_at ASC, id ASC
LIMIT 40
"""
UPDATE_THREAD_AUTOMATIC_TITLE_SQL = """
UPDATE threads
SET title=$1, title_source='automatic', updated_at=now()
WHERE owner_user_id=$2
  AND id=$3
  AND title_source='placeholder'
RETURNING title, title_source
"""
ARCHIVE_THREAD_SQL = (
    "UPDATE threads SET archived=true, updated_at=now() "
    "WHERE owner_user_id=$1 AND id=$2"
)


class ConversationThreadConnection(Protocol):
    async def execute(self, query: str, *args: object) -> str: ...

    async def fetch(self, query: str, *args: object) -> Sequence[Any]: ...

    async def fetchrow(self, query: str, *args: object) -> Any: ...

    async def fetchval(self, query: str, *args: object) -> Any: ...

    def transaction(self) -> Any: ...


async def thread_belongs_to_owner(
    connection: ConversationThreadConnection,
    *,
    owner_user_id: str | UUID,
    thread_id: UUID,
) -> bool:
    value = await connection.fetchval(
        THREAD_BELONGS_TO_OWNER_SQL,
        thread_id,
        owner_user_id,
    )
    return bool(value)


async def create_thread(
    connection: ConversationThreadConnection,
    *,
    owner_user_id: str | UUID,
    title: str,
) -> Any:
    return await connection.fetchrow(
        CREATE_THREAD_SQL,
        owner_user_id,
        owner_user_id,
        title,
    )


async def create_and_select_thread(
    connection: ConversationThreadConnection,
    *,
    owner_user_id: str | UUID,
    title: str,
) -> Any:
    """Create a thread and select it atomically inside the adapter boundary."""

    async with connection.transaction():
        row = await create_thread(
            connection,
            owner_user_id=owner_user_id,
            title=title,
        )
        await promote_resume_thread_v1(
            connection,
            owner_user_id=owner_user_id,
            thread_id=row["id"],
        )
    return row


async def list_visible_threads(
    connection: ConversationThreadConnection,
    *,
    owner_user_id: str | UUID,
) -> tuple[Any, ...]:
    rows = await connection.fetch(LIST_VISIBLE_THREADS_SQL, owner_user_id)
    return tuple(rows)


async def rename_thread_manual(
    connection: ConversationThreadConnection,
    *,
    owner_user_id: str | UUID,
    thread_id: UUID,
    title: str,
) -> Any:
    return await connection.fetchrow(
        RENAME_THREAD_MANUAL_SQL,
        title,
        owner_user_id,
        thread_id,
    )


async def set_thread_pinned(
    connection: ConversationThreadConnection,
    *,
    owner_user_id: str | UUID,
    thread_id: UUID,
    pinned: bool,
) -> Any:
    return await connection.fetchrow(
        SET_THREAD_PINNED_SQL,
        pinned,
        owner_user_id,
        thread_id,
    )


async def fetch_thread_title_state(
    connection: ConversationThreadConnection,
    *,
    owner_user_id: str | UUID,
    thread_id: UUID,
) -> Any:
    return await connection.fetchrow(
        FETCH_THREAD_TITLE_STATE_SQL,
        owner_user_id,
        thread_id,
    )


async def fetch_thread_title_transcript(
    connection: ConversationThreadConnection,
    *,
    owner_user_id: str | UUID,
    thread_id: UUID,
) -> tuple[Any, ...]:
    rows = await connection.fetch(
        FETCH_THREAD_TITLE_TRANSCRIPT_SQL,
        owner_user_id,
        thread_id,
    )
    return tuple(rows)


async def update_thread_automatic_title(
    connection: ConversationThreadConnection,
    *,
    owner_user_id: str | UUID,
    thread_id: UUID,
    title: str,
) -> Any:
    return await connection.fetchrow(
        UPDATE_THREAD_AUTOMATIC_TITLE_SQL,
        title,
        owner_user_id,
        thread_id,
    )


async def archive_thread(
    connection: ConversationThreadConnection,
    *,
    owner_user_id: str | UUID,
    thread_id: UUID,
) -> str:
    return await connection.execute(
        ARCHIVE_THREAD_SQL,
        owner_user_id,
        thread_id,
    )


__all__ = [
    "ARCHIVE_THREAD_SQL",
    "CREATE_THREAD_SQL",
    "ConversationThreadConnection",
    "FETCH_THREAD_TITLE_STATE_SQL",
    "FETCH_THREAD_TITLE_TRANSCRIPT_SQL",
    "LIST_VISIBLE_THREADS_SQL",
    "RENAME_THREAD_MANUAL_SQL",
    "SET_THREAD_PINNED_SQL",
    "THREAD_BELONGS_TO_OWNER_SQL",
    "UPDATE_THREAD_AUTOMATIC_TITLE_SQL",
    "archive_thread",
    "create_and_select_thread",
    "create_thread",
    "fetch_thread_title_state",
    "fetch_thread_title_transcript",
    "list_visible_threads",
    "rename_thread_manual",
    "set_thread_pinned",
    "thread_belongs_to_owner",
    "update_thread_automatic_title",
]
