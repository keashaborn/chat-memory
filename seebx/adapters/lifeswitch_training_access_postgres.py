from __future__ import annotations

"""PostgreSQL permission-read boundary shared by LifeSwitch Training views."""

import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from starlette.requests import Request

from seebx.adapters.lifeswitch_postgres import connect_lifeswitch


ConnectionFactory = Callable[[Request], Awaitable[Any]]
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")


def resolve_people_schema(value: str | None = None) -> str:
    candidate = str(
        os.getenv("LIFESWITCH_PEOPLE_SCHEMA", "lifeswitch_people")
        if value is None
        else value
    ).strip()
    if _IDENTIFIER.fullmatch(candidate) is None:
        raise RuntimeError("invalid LIFESWITCH_PEOPLE_SCHEMA")
    return candidate


class PostgresLifeSwitchTrainingAccessRepository:
    """Own the delegated Training permission query, but no policy decision."""

    def __init__(self, connection: Any, *, people_schema: str) -> None:
        self._connection = connection
        self._people_schema = resolve_people_schema(people_schema)

    async def has_people_permission(
        self,
        *,
        grantor_user_id: str,
        grantee_user_id: str,
        scope: str,
    ) -> bool:
        row = await self._connection.fetchrow(
            f"""
            select rp.relationship_permission_id
            from {self._people_schema}.relationship_permission rp
            join {self._people_schema}.relationship r
              on r.relationship_id=rp.relationship_id
            where rp.grantor_user_id=$1::uuid
              and rp.grantee_user_id=$2::uuid
              and rp.permission_scope=$3
              and rp.is_enabled=true
              and r.status='accepted'
            limit 1
            """,
            grantor_user_id,
            grantee_user_id,
            scope,
        )
        return bool(row)


@asynccontextmanager
async def lifeswitch_training_access_repository(
    request: Request,
    *,
    connection_factory: ConnectionFactory = connect_lifeswitch,
    people_schema: str | None = None,
) -> AsyncIterator[PostgresLifeSwitchTrainingAccessRepository]:
    connection = await connection_factory(request)
    try:
        yield PostgresLifeSwitchTrainingAccessRepository(
            connection,
            people_schema=resolve_people_schema(people_schema),
        )
    finally:
        await connection.close()
