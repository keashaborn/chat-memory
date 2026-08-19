from __future__ import annotations

"""Owner-bound HTTP contract for the optional SeeBx forms capability."""

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from seebx.adapters.lifeswitch_postgres import connect_lifeswitch
from seebx.core.identity import (
    require_actor,
    require_request_actor,
)

try:
    import jsonschema
except ImportError:  # pragma: no cover - exercised by the fail-closed test
    jsonschema = None  # type: ignore[assignment]


router = APIRouter()


def forms_enabled() -> bool:
    """Keep the new capability unmounted until schema/data deployment approval."""
    return os.getenv("SEEBX_FORMS_ENABLED", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(status_code=400, detail=f"invalid {field}")


def _json_text(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    raise HTTPException(status_code=500, detail="invalid_forms_json_storage")


def _require_validator() -> Any:
    if jsonschema is None:
        raise HTTPException(
            status_code=503,
            detail="forms_schema_validator_unavailable",
        )
    return jsonschema


def _validate_json_schema(schema: dict[str, Any]) -> None:
    validator = _require_validator()
    try:
        validator.Draft202012Validator.check_schema(schema)
    except validator.SchemaError:
        raise HTTPException(status_code=422, detail="invalid_json_schema")


def _validate_entry(schema: dict[str, Any], data: dict[str, Any]) -> None:
    validator = _require_validator()
    try:
        validator.Draft202012Validator(schema).validate(data)
    except validator.ValidationError:
        raise HTTPException(status_code=422, detail="schema_validation_failed")


def _command_count(command: str) -> int:
    try:
        return int(str(command).strip().split()[-1])
    except (TypeError, ValueError, IndexError):
        return 0


class PublishFormRequest(BaseModel):
    owner_user_id: str
    name: str = Field(min_length=1, max_length=200)
    json_schema: dict[str, Any]
    ui_schema: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    template_id: str | None = None


class PublishFormResponse(BaseModel):
    template_id: str
    version_id: str
    version: int


class TemplateListItem(BaseModel):
    template_id: str
    name: str
    status: str
    created_at: datetime
    latest_version_id: str | None = None
    latest_version: int | None = None
    latest_version_created_at: datetime | None = None


class FormVersionResponse(BaseModel):
    version_id: str
    template_id: str
    version: int
    json_schema: dict[str, Any]
    ui_schema: dict[str, Any]
    metadata: dict[str, Any]
    created_at: datetime


class CreateEntryRequest(BaseModel):
    owner_user_id: str
    subject_id: str = Field(min_length=1, max_length=200)
    template_version_id: str
    occurred_at: datetime | None = None
    data: dict[str, Any]


class CreateEntryResponse(BaseModel):
    entry_id: str
    occurred_at: datetime


class EntryListItem(BaseModel):
    id: str
    owner_user_id: str
    subject_id: str
    template_version_id: str
    occurred_at: datetime
    data: dict[str, Any]


class DeleteTemplateResponse(BaseModel):
    template_id: str
    deleted_entries: int
    deleted_versions: int
    deleted_templates: int


@router.post("/publish", response_model=PublishFormResponse)
async def publish_form(
    req: Request,
    payload: PublishFormRequest,
) -> PublishFormResponse:
    owner = await require_actor(req, payload.owner_user_id)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    _validate_json_schema(payload.json_schema)

    template_id = (
        _uuid(payload.template_id, "template_id")
        if payload.template_id
        else uuid.uuid4()
    )
    version_id = uuid.uuid4()
    conn = await connect_lifeswitch(req)
    try:
        try:
            async with conn.transaction():
                template = await conn.fetchrow(
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
                    await conn.execute(
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
                        await conn.fetchval(
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
                    await conn.execute(
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

                await conn.execute(
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
                    _json_text(payload.json_schema),
                    _json_text(payload.ui_schema),
                    _json_text(payload.metadata),
                )
        except asyncpg.UniqueViolationError:
            raise HTTPException(status_code=409, detail="form_version_conflict")

        return PublishFormResponse(
            template_id=str(template_id),
            version_id=str(version_id),
            version=next_version,
        )
    finally:
        await conn.close()


@router.get(
    "/templates/{owner_user_id}",
    response_model=list[TemplateListItem],
)
async def list_templates(
    req: Request,
    owner_user_id: str,
) -> list[TemplateListItem]:
    owner = await require_actor(req, owner_user_id)
    conn = await connect_lifeswitch(req)
    try:
        rows = await conn.fetch(
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
        return [
            TemplateListItem(
                template_id=str(row["template_id"]),
                name=row["name"],
                status=row["status"],
                created_at=row["created_at"],
                latest_version_id=(
                    str(row["latest_version_id"])
                    if row["latest_version_id"] is not None
                    else None
                ),
                latest_version=row["latest_version"],
                latest_version_created_at=row["latest_version_created_at"],
            )
            for row in rows
        ]
    finally:
        await conn.close()


@router.get(
    "/versions/{version_id}",
    response_model=FormVersionResponse,
)
async def get_version(
    req: Request,
    version_id: str,
) -> FormVersionResponse:
    actor = await require_request_actor(req)
    parsed_version_id = _uuid(version_id, "version_id")
    conn = await connect_lifeswitch(req)
    try:
        row = await conn.fetchrow(
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
            parsed_version_id,
            actor,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="version not found")
        return FormVersionResponse(
            version_id=str(row["version_id"]),
            template_id=str(row["template_id"]),
            version=int(row["version"]),
            json_schema=_json_object(row["json_schema"]),
            ui_schema=_json_object(row["ui_schema"]),
            metadata=_json_object(row["metadata"]),
            created_at=row["created_at"],
        )
    finally:
        await conn.close()


@router.post("/entries", response_model=CreateEntryResponse)
async def create_entry(
    req: Request,
    payload: CreateEntryRequest,
) -> CreateEntryResponse:
    owner = await require_actor(req, payload.owner_user_id)
    version_id = _uuid(payload.template_version_id, "template_version_id")
    occurred_at = payload.occurred_at or datetime.now(timezone.utc)
    subject_id = payload.subject_id.strip()
    if not subject_id:
        raise HTTPException(status_code=400, detail="subject_id required")

    conn = await connect_lifeswitch(req)
    try:
        version = await conn.fetchrow(
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
        if version is None:
            raise HTTPException(
                status_code=404,
                detail="template_version_id not found",
            )
        if version["status"] != "published":
            raise HTTPException(status_code=409, detail="template is not published")
        _validate_entry(_json_object(version["json_schema"]), payload.data)

        entry_id = uuid.uuid4()
        await conn.execute(
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
            _json_text(payload.data),
        )
        return CreateEntryResponse(
            entry_id=str(entry_id),
            occurred_at=occurred_at,
        )
    finally:
        await conn.close()


@router.get("/entries/list", response_model=list[EntryListItem])
async def list_entries(
    req: Request,
    owner_user_id: str,
    subject_id: str | None = Query(None, max_length=200),
    template_version_id: str | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> list[EntryListItem]:
    owner = await require_actor(req, owner_user_id)
    query = [
        "select form_entry_id as id, owner_user_id, subject_id,",
        "form_version_id as template_version_id, occurred_at, data",
        "from lifeswitch_forms.form_entry",
        "where owner_user_id=$1::uuid",
    ]
    arguments: list[Any] = [owner]
    if subject_id and subject_id.strip():
        arguments.append(subject_id.strip())
        query.append(f"and subject_id=${len(arguments)}")
    if template_version_id and template_version_id.strip():
        parsed_version_id = _uuid(
            template_version_id.strip(),
            "template_version_id",
        )
        arguments.append(parsed_version_id)
        query.append(f"and form_version_id=${len(arguments)}")
    arguments.append(limit)
    query.extend(["order by occurred_at desc", f"limit ${len(arguments)}"])

    conn = await connect_lifeswitch(req)
    try:
        rows = await conn.fetch("\n".join(query), *arguments)
        return [
            EntryListItem(
                id=str(row["id"]),
                owner_user_id=str(row["owner_user_id"]),
                subject_id=row["subject_id"],
                template_version_id=str(row["template_version_id"]),
                occurred_at=row["occurred_at"],
                data=_json_object(row["data"]),
            )
            for row in rows
        ]
    finally:
        await conn.close()


@router.delete(
    "/templates/{owner_user_id}/{template_id}",
    response_model=DeleteTemplateResponse,
)
async def delete_template(
    req: Request,
    owner_user_id: str,
    template_id: str,
    confirm: bool = False,
) -> DeleteTemplateResponse:
    owner = await require_actor(req, owner_user_id)
    if not confirm:
        raise HTTPException(status_code=400, detail="confirm=true required")
    parsed_template_id = _uuid(template_id, "template_id")

    conn = await connect_lifeswitch(req)
    try:
        async with conn.transaction():
            counts = await conn.fetchrow(
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
                parsed_template_id,
                owner,
            )
            if counts is None or int(counts["version_count"] or 0) == 0:
                template = await conn.fetchval(
                    """
                    select form_template_id
                    from lifeswitch_forms.form_template
                    where form_template_id=$1 and owner_user_id=$2::uuid
                    """,
                    parsed_template_id,
                    owner,
                )
                if template is None:
                    raise HTTPException(status_code=404, detail="template not found")

            deleted = await conn.execute(
                """
                delete from lifeswitch_forms.form_template
                where form_template_id=$1 and owner_user_id=$2::uuid
                """,
                parsed_template_id,
                owner,
            )
            deleted_templates = _command_count(deleted)
            if deleted_templates != 1:
                raise HTTPException(status_code=404, detail="template not found")

        return DeleteTemplateResponse(
            template_id=str(parsed_template_id),
            deleted_entries=int(counts["entry_count"] or 0),
            deleted_versions=int(counts["version_count"] or 0),
            deleted_templates=deleted_templates,
        )
    finally:
        await conn.close()
