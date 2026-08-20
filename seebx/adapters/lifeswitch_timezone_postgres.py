from __future__ import annotations

"""PostgreSQL authority for owner-scoped LifeSwitch timezone settings."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import asyncpg
from fastapi import Request

from seebx.adapters.lifeswitch_postgres import connect_lifeswitch


ACCOUNT_WRITER_ROLE = "lifeswitch_chat_account_writer_v1"
ConnectionFactory = Callable[[Request], Awaitable[Any]]


class AccountTimezoneRepositoryError(RuntimeError):
    """Stable content-free repository failure."""


class AccountTimezoneRepositoryUnavailable(AccountTimezoneRepositoryError):
    pass


class AccountTimezoneRepositoryOwnerDenied(AccountTimezoneRepositoryError):
    pass


class AccountTimezoneRepositoryConflict(AccountTimezoneRepositoryError):
    pass


class AccountTimezoneRepositoryInvalid(AccountTimezoneRepositoryError):
    pass


@dataclass(frozen=True, slots=True)
class AccountTimezoneRecord:
    timezone_name: str
    timezone_source: Literal["account_setting", "reviewed_migration"]
    revision: int
    updated_at: datetime
    changed: bool


def _record(row: Any) -> AccountTimezoneRecord | None:
    if row is None:
        return None
    return AccountTimezoneRecord(
        timezone_name=str(row["timezone_name"]),
        timezone_source=str(row["timezone_source"]),
        revision=int(row["revision"]),
        updated_at=row["updated_at"],
        changed=bool(row["changed"] if "changed" in row.keys() else False),
    )


def _translate(error: asyncpg.PostgresError) -> AccountTimezoneRepositoryError:
    sqlstate = getattr(error, "sqlstate", None)
    if sqlstate in {"40001", "23505"}:
        return AccountTimezoneRepositoryConflict("timezone_revision_conflict")
    if sqlstate == "22023":
        return AccountTimezoneRepositoryInvalid("invalid_timezone")
    if sqlstate == "42501":
        return AccountTimezoneRepositoryOwnerDenied("owner_scope_denied")
    return AccountTimezoneRepositoryUnavailable("timezone_repository_unavailable")


class PostgresAccountTimezoneRepository:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    async def _set_owner(self, actor: UUID) -> None:
        await self._connection.execute(
            "select set_config('app.user_id',$1,true)",
            str(actor),
        )
        await self._connection.execute(
            "select set_config('app.lifeswitch_owner_id',$1,true)",
            str(actor),
        )
        await self._connection.execute(f"set local role {ACCOUNT_WRITER_ROLE}")

    async def get(self, actor: UUID) -> AccountTimezoneRecord | None:
        try:
            async with self._connection.transaction(readonly=True):
                await self._set_owner(actor)
                row = await self._connection.fetchrow(
                    "select * from lifeswitch_chat.read_account_timezone_setting_v1($1)",
                    actor,
                )
                return _record(row)
        except asyncpg.PostgresError as error:
            raise _translate(error) from error

    async def put(
        self,
        actor: UUID,
        timezone_name: str,
        expected_revision: int,
        request_hash: str,
    ) -> AccountTimezoneRecord:
        try:
            async with self._connection.transaction():
                await self._set_owner(actor)
                row = await self._connection.fetchrow(
                    """
                    select * from lifeswitch_chat.write_account_timezone_setting_v1(
                      $1,$2,$3,$4
                    )
                    """,
                    actor,
                    timezone_name,
                    expected_revision,
                    request_hash,
                )
                record = _record(row)
                if record is None:
                    raise AccountTimezoneRepositoryUnavailable(
                        "timezone_repository_returned_no_record"
                    )
                return record
        except asyncpg.PostgresError as error:
            raise _translate(error) from error


@asynccontextmanager
async def account_timezone_repository(
    request: Request,
    *,
    connection_factory: ConnectionFactory = connect_lifeswitch,
) -> AsyncIterator[PostgresAccountTimezoneRepository]:
    try:
        connection = await connection_factory(request)
    except RuntimeError as error:
        raise AccountTimezoneRepositoryUnavailable(
            "timezone_repository_unavailable"
        ) from error
    try:
        yield PostgresAccountTimezoneRepository(connection)
    finally:
        await connection.close()


__all__ = [
    "AccountTimezoneRecord",
    "AccountTimezoneRepositoryConflict",
    "AccountTimezoneRepositoryError",
    "AccountTimezoneRepositoryInvalid",
    "AccountTimezoneRepositoryOwnerDenied",
    "AccountTimezoneRepositoryUnavailable",
    "PostgresAccountTimezoneRepository",
    "account_timezone_repository",
]
