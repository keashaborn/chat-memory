from __future__ import annotations

from datetime import timedelta
from typing import Sequence
import uuid

import asyncpg


MAX_AUTHENTICATED_OWNERS = 1000
DEFAULT_ACTIVITY_DAYS = 90


def explicit_owners(values: Sequence[str]) -> list[uuid.UUID]:
    try:
        owners = sorted({uuid.UUID(value) for value in values}, key=str)
    except ValueError as exc:
        raise RuntimeError("owner scope contains an invalid UUID") from exc
    if len(owners) > MAX_AUTHENTICATED_OWNERS:
        raise RuntimeError("owner scope exceeds the supported limit")
    return owners


async def resolve_authenticated_owners(
    dsn: str,
    values: Sequence[str],
    *,
    activity_days: int = DEFAULT_ACTIVITY_DAYS,
    limit: int = MAX_AUTHENTICATED_OWNERS,
) -> list[uuid.UUID]:
    if values:
        return explicit_owners(values)
    if not 1 <= activity_days <= 365:
        raise RuntimeError("authenticated owner activity window is invalid")
    if not 1 <= limit <= MAX_AUTHENTICATED_OWNERS:
        raise RuntimeError("authenticated owner limit is invalid")

    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("authenticated owner discovery requires brains_app")
        rows = await conn.fetch(
            """
            SELECT owner_user_id
            FROM memory.list_recent_authenticated_owners_v1($1,$2)
            """,
            timedelta(days=activity_days),
            limit,
        )
    finally:
        await conn.close()
    return [uuid.UUID(str(row["owner_user_id"])) for row in rows]
