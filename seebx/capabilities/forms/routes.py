from __future__ import annotations

"""Owner-bound HTTP contract for the optional SeeBx forms capability."""

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from seebx.adapters.lifeswitch_forms_postgres import (
    FormsTemplateNotFoundError,
    FormsVersionConflictError,
    lifeswitch_forms_repository,
)
from seebx.core.identity import require_actor, require_request_actor

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
    try:
        async with lifeswitch_forms_repository(req) as forms:
            next_version = await forms.publish_form(
                owner=owner,
                template_id=template_id,
                version_id=version_id,
                name=name,
                json_schema=payload.json_schema,
                ui_schema=payload.ui_schema,
                metadata=payload.metadata,
            )
    except FormsVersionConflictError:
        raise HTTPException(status_code=409, detail="form_version_conflict")

    return PublishFormResponse(
        template_id=str(template_id),
        version_id=str(version_id),
        version=next_version,
    )


@router.get(
    "/templates/{owner_user_id}",
    response_model=list[TemplateListItem],
)
async def list_templates(
    req: Request,
    owner_user_id: str,
) -> list[TemplateListItem]:
    owner = await require_actor(req, owner_user_id)
    async with lifeswitch_forms_repository(req) as forms:
        rows = await forms.list_templates(owner)
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
    async with lifeswitch_forms_repository(req) as forms:
        row = await forms.get_version(owner=actor, version_id=parsed_version_id)
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

    async with lifeswitch_forms_repository(req) as forms:
        version = await forms.get_entry_version(
            owner=owner,
            version_id=version_id,
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
        await forms.create_entry(
            entry_id=entry_id,
            owner=owner,
            subject_id=subject_id,
            version_id=version_id,
            occurred_at=occurred_at,
            data=payload.data,
        )
    return CreateEntryResponse(entry_id=str(entry_id), occurred_at=occurred_at)


@router.get("/entries/list", response_model=list[EntryListItem])
async def list_entries(
    req: Request,
    owner_user_id: str,
    subject_id: str | None = Query(None, max_length=200),
    template_version_id: str | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> list[EntryListItem]:
    owner = await require_actor(req, owner_user_id)
    clean_subject_id = subject_id.strip() if subject_id and subject_id.strip() else None
    parsed_version_id = (
        _uuid(template_version_id.strip(), "template_version_id")
        if template_version_id and template_version_id.strip()
        else None
    )
    async with lifeswitch_forms_repository(req) as forms:
        rows = await forms.list_entries(
            owner=owner,
            subject_id=clean_subject_id,
            version_id=parsed_version_id,
            limit=limit,
        )
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

    try:
        async with lifeswitch_forms_repository(req) as forms:
            deleted = await forms.delete_template(
                owner=owner,
                template_id=parsed_template_id,
            )
    except FormsTemplateNotFoundError:
        raise HTTPException(status_code=404, detail="template not found")

    return DeleteTemplateResponse(
        template_id=str(parsed_template_id),
        deleted_entries=deleted.entries,
        deleted_versions=deleted.versions,
        deleted_templates=deleted.templates,
    )
