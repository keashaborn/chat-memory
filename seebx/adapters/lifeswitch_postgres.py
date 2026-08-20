from __future__ import annotations

import os

import asyncpg
from fastapi import Request

from seebx.core.identity import require_request_actor


def resolve_lifeswitch_postgres_dsn() -> str:
    """Resolve the isolated LifeSwitch DSN at connection time."""
    dsn = (os.getenv("LIFESWITCH_POSTGRES_DSN") or "").strip()
    if not dsn:
        raise RuntimeError("LIFESWITCH_POSTGRES_DSN missing")
    return dsn


async def connect_lifeswitch(req: Request) -> asyncpg.Connection:
    """Open one owner-bound LifeSwitch connection for the request."""
    actor = await require_request_actor(req)
    conn = await asyncpg.connect(resolve_lifeswitch_postgres_dsn())
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


__all__ = [
    "connect_lifeswitch",
    "resolve_lifeswitch_postgres_dsn",
]
