from __future__ import annotations

"""Shared PostgreSQL connection lifetime and owner-session boundary."""

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import asyncpg


OWNER_SESSION_SQL = "SELECT set_config('app.user_id', $1, false)"
READINESS_SQL = "select 1"
ConnectionFactory = Callable[..., Awaitable[Any]]


async def set_connection_actor(
    connection: Any,
    owner_user_id: str | UUID,
) -> str:
    try:
        owner = UUID(str(owner_user_id))
    except (TypeError, ValueError) as exc:
        raise ValueError("owner_user_id must be a UUID") from exc
    canonical = str(owner)
    await connection.execute(OWNER_SESSION_SQL, canonical)
    return canonical


class PostgresConnectionProvider:
    def __init__(
        self,
        dsn: str,
        *,
        connect_factory: ConnectionFactory | None = None,
        connect_kwargs: Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(dsn, str) or not dsn:
            raise ValueError("dsn is required")
        options = dict(connect_kwargs or {})
        if any(not isinstance(key, str) or not key for key in options):
            raise ValueError("connect_kwargs keys must be non-empty strings")
        self.dsn = dsn
        self.connect_factory = connect_factory
        self.connect_kwargs = options

    async def _connect(self) -> Any:
        factory = self.connect_factory or asyncpg.connect
        return await factory(self.dsn, **self.connect_kwargs)

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Any]:
        connection = await self._connect()
        try:
            yield connection
        finally:
            await connection.close()

    @asynccontextmanager
    async def owner_connection(
        self,
        owner_user_id: str | UUID,
    ) -> AsyncIterator[Any]:
        async with self.connection() as connection:
            await set_connection_actor(connection, owner_user_id)
            yield connection

    async def readiness_value(self) -> object:
        async with self.connection() as connection:
            return await connection.fetchval(READINESS_SQL)


__all__ = [
    "ConnectionFactory",
    "OWNER_SESSION_SQL",
    "PostgresConnectionProvider",
    "READINESS_SQL",
    "set_connection_actor",
]
