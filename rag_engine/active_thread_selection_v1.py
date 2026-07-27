from __future__ import annotations

import uuid
from typing import Any

import asyncpg


class ActiveThreadSelectionV1Error(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _owner_uuid(owner_user_id: str | uuid.UUID) -> uuid.UUID:
    try:
        return uuid.UUID(str(owner_user_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ActiveThreadSelectionV1Error("invalid_owner_user_id") from exc


def _thread_uuid(thread_id: str | uuid.UUID) -> uuid.UUID:
    try:
        return uuid.UUID(str(thread_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ActiveThreadSelectionV1Error("invalid_thread_id") from exc


def _thread_payload(row: Any) -> dict[str, Any]:
    return {
        "thread_id": str(row["id"]),
        "title": row["title"],
        "updated_at": row["updated_at"].isoformat(),
    }


async def _lock_owner(
    conn: asyncpg.Connection,
    owner_user_id: uuid.UUID,
) -> None:
    await conn.execute(
        """
        SELECT pg_advisory_xact_lock(
          hashtextextended('active_thread_selection_v1:' || $1::text, 0)
        )
        """,
        str(owner_user_id),
    )


async def _visible_thread(
    conn: asyncpg.Connection,
    owner_user_id: uuid.UUID,
    thread_id: uuid.UUID,
) -> Any:
    return await conn.fetchrow(
        """
        SELECT id, title, updated_at
        FROM public.threads
        WHERE owner_user_id=$1
          AND id=$2
          AND archived=false
        """,
        owner_user_id,
        thread_id,
    )


async def _newest_visible_thread(
    conn: asyncpg.Connection,
    owner_user_id: uuid.UUID,
) -> Any:
    return await conn.fetchrow(
        """
        SELECT id, title, updated_at
        FROM public.threads
        WHERE owner_user_id=$1
          AND archived=false
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """,
        owner_user_id,
    )


async def _write_selection(
    conn: asyncpg.Connection,
    owner_user_id: uuid.UUID,
    thread_id: uuid.UUID | None,
) -> None:
    await conn.execute(
        """
        INSERT INTO public.active_thread_selection(
          owner_user_id,
          thread_id,
          selected_at,
          updated_at
        )
        VALUES ($1, $2, CASE WHEN $2::uuid IS NULL THEN NULL ELSE now() END, now())
        ON CONFLICT (owner_user_id) DO UPDATE
        SET thread_id=EXCLUDED.thread_id,
            selected_at=EXCLUDED.selected_at,
            updated_at=now()
        """,
        owner_user_id,
        thread_id,
    )


async def get_active_thread_v1(
    conn: asyncpg.Connection,
    *,
    owner_user_id: str | uuid.UUID,
) -> dict[str, Any] | None:
    owner = _owner_uuid(owner_user_id)
    async with conn.transaction():
        await _lock_owner(conn, owner)
        selection = await conn.fetchrow(
            """
            SELECT thread_id
            FROM public.active_thread_selection
            WHERE owner_user_id=$1
            FOR UPDATE
            """,
            owner,
        )

        if selection is not None:
            thread_id = selection["thread_id"]
            if thread_id is None:
                return None
            selected = await _visible_thread(conn, owner, thread_id)
            if selected is not None:
                return _thread_payload(selected)

        fallback = await _newest_visible_thread(conn, owner)
        fallback_id = fallback["id"] if fallback is not None else None
        await _write_selection(conn, owner, fallback_id)
        return _thread_payload(fallback) if fallback is not None else None


async def select_active_thread_v1(
    conn: asyncpg.Connection,
    *,
    owner_user_id: str | uuid.UUID,
    thread_id: str | uuid.UUID,
) -> dict[str, Any]:
    async with conn.transaction():
        return await promote_resume_thread_v1(
            conn,
            owner_user_id=owner_user_id,
            thread_id=thread_id,
        )


async def promote_resume_thread_v1(
    conn: asyncpg.Connection,
    *,
    owner_user_id: str | uuid.UUID,
    thread_id: str | uuid.UUID,
) -> dict[str, Any]:
    """Promote a verified thread inside the caller's existing transaction."""

    owner = _owner_uuid(owner_user_id)
    selected_id = _thread_uuid(thread_id)
    await _lock_owner(conn, owner)
    selected = await _visible_thread(conn, owner, selected_id)
    if selected is None:
        raise ActiveThreadSelectionV1Error("thread_not_found")
    await _write_selection(conn, owner, selected_id)
    return _thread_payload(selected)


async def clear_active_thread_v1(
    conn: asyncpg.Connection,
    *,
    owner_user_id: str | uuid.UUID,
) -> None:
    owner = _owner_uuid(owner_user_id)
    async with conn.transaction():
        await _lock_owner(conn, owner)
        await _write_selection(conn, owner, None)
