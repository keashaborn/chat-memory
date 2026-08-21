from __future__ import annotations

"""PostgreSQL effect boundary for the optional LifeSwitch Forms capability."""

import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg
from starlette.requests import Request

from seebx.adapters.lifeswitch_postgres import connect_lifeswitch


ConnectionFactory = Callable[[Request], Awaitable[Any]]


class FormsVersionConflictError(RuntimeError):
    """The next immutable form version could not be inserted uniquely."""


class FormsTemplateNotFoundError(LookupError):
    """The owner-bound form template does not exist."""


@dataclass(frozen=True)
class DeletedTemplateCounts:
    entries: int
    versions: int
    templates: int


def _json_text(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _command_count(command: str) -> int:
    try:
        return int(str(command).strip().split()[-1])
    except (TypeError, ValueError, IndexError):
        return 0


class PostgresLifeSwitchFormsRepository:
    """Own every SQL statement and transaction used by Forms HTTP routes."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    async def publish_form(
        self,
        *,
        owner: uuid.UUID | str,
        template_id: uuid.UUID,
        version_id: uuid.UUID,
        name: str,
        json_schema: dict[str, Any],
        ui_schema: dict[str, Any],
        metadata: dict[str, Any],
    ) -> int:
        try:
            async with self._connection.transaction():
                template = await self._connection.fetchrow(
                    """
                    select form_template_id
                    from lifeswitch_forms.form_template
                    where form_template_id=$1 and owner_user_id=$2::uuid
                    for update
                    """,
                    template_id,
                    owner,
                )
                if template is None:
                    await self._connection.execute(
                        """
                        insert into lifeswitch_forms.form_template (
                          form_template_id, owner_user_id, name, status
                        ) values ($1,$2::uuid,$3,'published')
                        """,
                        template_id,
                        owner,
                        name,
                    )
                    next_version = 1
                else:
                    next_version = int(
                        await self._connection.fetchval(
                            """
                            select coalesce(max(version),0)+1
                            from lifeswitch_forms.form_version
                            where form_template_id=$1
                              and owner_user_id=$2::uuid
                            """,
                            template_id,
                            owner,
                        )
                    )
                    await self._connection.execute(
                        """
                        update lifeswitch_forms.form_template
                        set name=$3, status='published', updated_at=now()
                        where form_template_id=$1
                          and owner_user_id=$2::uuid
                        """,
                        template_id,
                        owner,
                        name,
                    )

                await self._connection.execute(
                    """
                    insert into lifeswitch_forms.form_version (
                      form_version_id, form_template_id, owner_user_id,
                      version, json_schema, ui_schema, metadata
                    ) values (
                      $1,$2,$3::uuid,$4,$5::jsonb,$6::jsonb,$7::jsonb
                    )
                    """,
                    version_id,
                    template_id,
                    owner,
                    next_version,
                    _json_text(json_schema),
                    _json_text(ui_schema),
                    _json_text(metadata),
                )
        except asyncpg.UniqueViolationError as exc:
            raise FormsVersionConflictError from exc
        return next_version

    async def list_templates(self, owner: uuid.UUID | str) -> list[Any]:
        rows = await self._connection.fetch(
            """
            select
              t.form_template_id as template_id,
              t.name,
              t.status,
              t.created_at,
              v.form_version_id as latest_version_id,
              v.version as latest_version,
              v.created_at as latest_version_created_at
            from lifeswitch_forms.form_template t
            left join lateral (
              select form_version_id, version, created_at
              from lifeswitch_forms.form_version
              where form_template_id=t.form_template_id
                and owner_user_id=t.owner_user_id
              order by version desc
              limit 1
            ) v on true
            where t.owner_user_id=$1::uuid
            order by t.created_at desc
            """,
            owner,
        )
        return list(rows)

    async def get_version(
        self,
        *,
        owner: uuid.UUID | str,
        version_id: uuid.UUID,
    ) -> Any | None:
        return await self._connection.fetchrow(
            """
            select
              v.form_version_id as version_id,
              v.form_template_id as template_id,
              v.version,
              v.json_schema,
              v.ui_schema,
              v.metadata,
              v.created_at
            from lifeswitch_forms.form_version v
            join lifeswitch_forms.form_template t
              on t.form_template_id=v.form_template_id
             and t.owner_user_id=v.owner_user_id
            where v.form_version_id=$1
              and v.owner_user_id=$2::uuid
            """,
            version_id,
            owner,
        )

    async def get_entry_version(
        self,
        *,
        owner: uuid.UUID | str,
        version_id: uuid.UUID,
    ) -> Any | None:
        return await self._connection.fetchrow(
            """
            select v.json_schema, t.status
            from lifeswitch_forms.form_version v
            join lifeswitch_forms.form_template t
              on t.form_template_id=v.form_template_id
             and t.owner_user_id=v.owner_user_id
            where v.form_version_id=$1
              and v.owner_user_id=$2::uuid
            """,
            version_id,
            owner,
        )

    async def create_entry(
        self,
        *,
        entry_id: uuid.UUID,
        owner: uuid.UUID | str,
        subject_id: str,
        version_id: uuid.UUID,
        occurred_at: datetime,
        data: dict[str, Any],
    ) -> None:
        await self._connection.execute(
            """
            insert into lifeswitch_forms.form_entry (
              form_entry_id, owner_user_id, subject_id,
              form_version_id, occurred_at, data
            ) values ($1,$2::uuid,$3,$4,$5,$6::jsonb)
            """,
            entry_id,
            owner,
            subject_id,
            version_id,
            occurred_at,
            _json_text(data),
        )

    async def list_entries(
        self,
        *,
        owner: uuid.UUID | str,
        subject_id: str | None,
        version_id: uuid.UUID | None,
        limit: int,
    ) -> list[Any]:
        query = [
            "select form_entry_id as id, owner_user_id, subject_id,",
            "form_version_id as template_version_id, occurred_at, data",
            "from lifeswitch_forms.form_entry",
            "where owner_user_id=$1::uuid",
        ]
        arguments: list[Any] = [owner]
        if subject_id is not None:
            arguments.append(subject_id)
            query.append(f"and subject_id=${len(arguments)}")
        if version_id is not None:
            arguments.append(version_id)
            query.append(f"and form_version_id=${len(arguments)}")
        arguments.append(limit)
        query.extend(["order by occurred_at desc", f"limit ${len(arguments)}"])
        rows: Sequence[Any] = await self._connection.fetch(
            "\n".join(query),
            *arguments,
        )
        return list(rows)

    async def delete_template(
        self,
        *,
        owner: uuid.UUID | str,
        template_id: uuid.UUID,
    ) -> DeletedTemplateCounts:
        async with self._connection.transaction():
            counts = await self._connection.fetchrow(
                """
                select
                  count(distinct v.form_version_id)::integer as version_count,
                  count(e.form_entry_id)::integer as entry_count
                from lifeswitch_forms.form_template t
                left join lifeswitch_forms.form_version v
                  on v.form_template_id=t.form_template_id
                 and v.owner_user_id=t.owner_user_id
                left join lifeswitch_forms.form_entry e
                  on e.form_version_id=v.form_version_id
                 and e.owner_user_id=v.owner_user_id
                where t.form_template_id=$1
                  and t.owner_user_id=$2::uuid
                """,
                template_id,
                owner,
            )
            version_count = int(counts["version_count"] or 0)
            entry_count = int(counts["entry_count"] or 0)
            if version_count == 0:
                existing = await self._connection.fetchval(
                    """
                    select form_template_id
                    from lifeswitch_forms.form_template
                    where form_template_id=$1 and owner_user_id=$2::uuid
                    """,
                    template_id,
                    owner,
                )
                if existing is None:
                    raise FormsTemplateNotFoundError

            deleted = await self._connection.execute(
                """
                delete from lifeswitch_forms.form_template
                where form_template_id=$1 and owner_user_id=$2::uuid
                """,
                template_id,
                owner,
            )
            deleted_templates = _command_count(deleted)
            if deleted_templates != 1:
                raise FormsTemplateNotFoundError

        return DeletedTemplateCounts(
            entries=entry_count,
            versions=version_count,
            templates=deleted_templates,
        )


@asynccontextmanager
async def lifeswitch_forms_repository(
    request: Request,
    *,
    connection_factory: ConnectionFactory = connect_lifeswitch,
) -> AsyncIterator[PostgresLifeSwitchFormsRepository]:
    connection = await connection_factory(request)
    try:
        yield PostgresLifeSwitchFormsRepository(connection)
    finally:
        await connection.close()


__all__ = [
    "DeletedTemplateCounts",
    "FormsTemplateNotFoundError",
    "FormsVersionConflictError",
    "PostgresLifeSwitchFormsRepository",
    "lifeswitch_forms_repository",
]
