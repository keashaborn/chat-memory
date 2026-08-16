from __future__ import annotations

import os

import asyncpg
from fastapi import Request

from rag_engine.lifeswitch_auth import require_authenticated_actor


DSN = (
    os.getenv("LIFESWITCH_POSTGRES_DSN")
    or os.getenv("POSTGRES_DSN")
    or ""
).strip()

async def connect_lifeswitch(req: Request) -> asyncpg.Connection:
    """Open one owner-bound LifeSwitch connection for the request."""
    if not DSN:
        raise RuntimeError("LIFESWITCH_POSTGRES_DSN or POSTGRES_DSN missing")
    actor = require_authenticated_actor(req)
    conn = await asyncpg.connect(DSN)
    try:
        # This connection is request-scoped and always closed by the router.
        # Session scope is deliberate: many existing handlers use implicit
        # transactions, so transaction-local settings would disappear between
        # statements. Closing the connection prevents context reuse.
        await conn.execute(
            "select set_config('app.user_id',$1,false), "
            "set_config('app.lifeswitch_owner_id',$1,false)",
            actor,
        )
        return conn
    except BaseException:
        await conn.close()
        raise


__all__ = ["DSN", "connect_lifeswitch"]
