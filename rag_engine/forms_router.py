from __future__ import annotations

from typing import Any, Dict, List, Optional
import os, uuid, json
from datetime import datetime, timezone

import asyncpg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

DSN = os.getenv("POSTGRES_DSN", "postgres://sage:strongpassword@localhost:5432/memory")

router = APIRouter()

# Optional JSON Schema validation (no hard dependency).
# If jsonschema isn't installed, endpoints still work (validation skipped).
try:
    import jsonschema  # type: ignore
except Exception:
    jsonschema = None  # type: ignore


async def _connect():
    conn = await asyncpg.connect(DSN)
    # Ensure asyncpg can accept/return dict/list for json/jsonb columns.
    await conn.set_type_codec('json', encoder=json.dumps, decoder=json.loads, schema='pg_catalog')
    await conn.set_type_codec('jsonb', encoder=json.dumps, decoder=json.loads, schema='pg_catalog')
    return conn

def _parse_uuid(s: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(s))
    except Exception:
        raise HTTPException(status_code=400, detail=f"invalid {field}")


class PublishFormReq(BaseModel):
    owner_user_id: str
    name: str
    json_schema: Dict[str, Any]
    ui_schema: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    template_id: Optional[str] = None  # uuid


class PublishFormResp(BaseModel):
    template_id: str
    version_id: str
    version: int


@router.post("/publish", response_model=PublishFormResp)
async def publish(req: PublishFormReq) -> PublishFormResp:
    owner = (req.owner_user_id or "").strip()
    name = (req.name or "").strip()
    if not owner:
        raise HTTPException(status_code=400, detail="owner_user_id required")
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    if not isinstance(req.json_schema, dict):
        raise HTTPException(status_code=400, detail="json_schema must be an object")

    if jsonschema is not None:
        try:
            jsonschema.Draft202012Validator.check_schema(req.json_schema)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"invalid json_schema: {e}")

    template_id = _parse_uuid(req.template_id, "template_id") if req.template_id else uuid.uuid4()
    version_id = uuid.uuid4()

    conn = await _connect()
    try:
        async with conn.transaction():
            tpl = await conn.fetchrow(
                "select id, owner_user_id from vb_form_templates where id=$1",
                template_id,
            )

            if tpl is None:
                await conn.execute(
                    "insert into vb_form_templates(id, owner_user_id, name, status) values($1,$2,$3,'published')",
                    template_id,
                    owner,
                    name,
                )
                next_version = 1
            else:
                if tpl["owner_user_id"] != owner:
                    raise HTTPException(status_code=403, detail="template_id does not belong to owner_user_id")

                row = await conn.fetchrow(
                    "select coalesce(max(version),0)+1 as v from vb_form_versions where template_id=$1",
                    template_id,
                )
                next_version = int(row["v"])

                await conn.execute(
                    "update vb_form_templates set name=$1, status='published' where id=$2",
                    name,
                    template_id,
                )

            try:
                await conn.execute(
                    "insert into vb_form_versions(id, template_id, version, json_schema, ui_schema, metadata) "
                    "values($1,$2,$3,$4::jsonb,$5::jsonb,$6::jsonb)",
                    version_id,
                    template_id,
                    next_version,
                    req.json_schema,
                    req.ui_schema,
                    req.metadata,
                )
            except asyncpg.UniqueViolationError:
                raise HTTPException(status_code=409, detail="version conflict; retry")

        return PublishFormResp(template_id=str(template_id), version_id=str(version_id), version=next_version)
    finally:
        await conn.close()


class TemplateListItem(BaseModel):
    template_id: str
    name: str
    status: str
    created_at: datetime
    latest_version_id: Optional[str] = None
    latest_version: Optional[int] = None
    latest_version_created_at: Optional[datetime] = None


@router.get("/templates/{owner_user_id}", response_model=List[TemplateListItem])
async def list_templates(owner_user_id: str) -> List[TemplateListItem]:
    owner = (owner_user_id or "").strip()
    if not owner:
        raise HTTPException(status_code=400, detail="owner_user_id required")

    conn = await _connect()
    try:
        rows = await conn.fetch(
            """
            select
              t.id as template_id,
              t.name,
              t.status,
              t.created_at,
              v.id as latest_version_id,
              v.version as latest_version,
              v.created_at as latest_version_created_at
            from vb_form_templates t
            left join lateral (
              select id, version, created_at
              from vb_form_versions
              where template_id=t.id
              order by version desc
              limit 1
            ) v on true
            where t.owner_user_id=$1
            order by t.created_at desc
            """,
            owner,
        )

        out: List[TemplateListItem] = []
        for r in rows:
            out.append(
                TemplateListItem(
                    template_id=str(r["template_id"]),
                    name=r["name"],
                    status=r["status"],
                    created_at=r["created_at"],
                    latest_version_id=str(r["latest_version_id"]) if r["latest_version_id"] else None,
                    latest_version=int(r["latest_version"]) if r["latest_version"] is not None else None,
                    latest_version_created_at=r["latest_version_created_at"],
                )
            )
        return out
    finally:
        await conn.close()


class FormVersionResp(BaseModel):
    version_id: str
    template_id: str
    version: int
    json_schema: Dict[str, Any]
    ui_schema: Dict[str, Any]
    metadata: Dict[str, Any]
    created_at: datetime


@router.get("/versions/{version_id}", response_model=FormVersionResp)
async def get_version(version_id: str) -> FormVersionResp:
    vid = _parse_uuid(version_id, "version_id")

    conn = await _connect()
    try:
        row = await conn.fetchrow(
            "select id, template_id, version, json_schema, ui_schema, metadata, created_at "
            "from vb_form_versions where id=$1",
            vid,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="version not found")

        return FormVersionResp(
            version_id=str(row["id"]),
            template_id=str(row["template_id"]),
            version=int(row["version"]),
            json_schema=row["json_schema"],
            ui_schema=row["ui_schema"] or {},
            metadata=row["metadata"] or {},
            created_at=row["created_at"],
        )
    finally:
        await conn.close()


class CreateEntryReq(BaseModel):
    owner_user_id: str
    subject_id: str
    template_version_id: str
    occurred_at: Optional[datetime] = None
    data: Dict[str, Any]


class CreateEntryResp(BaseModel):
    entry_id: str
    occurred_at: datetime


@router.post("/entries", response_model=CreateEntryResp)
async def create_entry(req: CreateEntryReq) -> CreateEntryResp:
    owner = (req.owner_user_id or "").strip()
    subject = (req.subject_id or "").strip()
    if not owner:
        raise HTTPException(status_code=400, detail="owner_user_id required")
    if not subject:
        raise HTTPException(status_code=400, detail="subject_id required")

    vid = _parse_uuid(req.template_version_id, "template_version_id")
    occurred_at = req.occurred_at or datetime.now(timezone.utc)

    conn = await _connect()
    try:
        row = await conn.fetchrow(
            """
            select
              v.id as version_id,
              v.json_schema,
              t.owner_user_id,
              t.status
            from vb_form_versions v
            join vb_form_templates t on t.id=v.template_id
            where v.id=$1
            """,
            vid,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="template_version_id not found")
        if row["owner_user_id"] != owner:
            raise HTTPException(status_code=403, detail="template_version_id does not belong to owner_user_id")
        if row["status"] != "published":
            raise HTTPException(status_code=409, detail="template is not published")

        schema = row["json_schema"]
        if jsonschema is not None:
            try:
                jsonschema.validate(instance=req.data, schema=schema)
            except Exception as e:
                raise HTTPException(status_code=422, detail=f"schema validation failed: {e}")

        entry_id = uuid.uuid4()
        await conn.execute(
            "insert into vb_form_entries(id, owner_user_id, subject_id, template_version_id, occurred_at, data) "
            "values($1,$2,$3,$4,$5,$6::jsonb)",
            entry_id,
            owner,
            subject,
            vid,
            occurred_at,
            req.data,
        )

        return CreateEntryResp(entry_id=str(entry_id), occurred_at=occurred_at)
    finally:
        await conn.close()


class ListEntriesResp(BaseModel):
    id: str
    owner_user_id: str
    subject_id: str
    template_version_id: str
    occurred_at: datetime
    data: Dict[str, Any]


@router.get("/entries/list", response_model=List[ListEntriesResp])
async def list_entries(
    owner_user_id: str,
    subject_id: Optional[str] = None,
    template_version_id: Optional[str] = None,
    limit: int = 50,
) -> List[ListEntriesResp]:
    owner = (owner_user_id or "").strip()
    if not owner:
        raise HTTPException(status_code=400, detail="owner_user_id required")

    if limit < 1:
        limit = 1
    if limit > 500:
        limit = 500

    conn = await _connect()
    try:
        q = [
            "select id, owner_user_id, subject_id, template_version_id, occurred_at, data",
            "from vb_form_entries",
            "where owner_user_id=$1",
        ]
        args = [owner]
        argi = 2

        if subject_id and subject_id.strip():
            q.append(f"and subject_id=${argi}")
            args.append(subject_id.strip())
            argi += 1

        if template_version_id and template_version_id.strip():
            # validate uuid
            _ = _parse_uuid(template_version_id.strip(), "template_version_id")
            q.append(f"and template_version_id=${argi}::uuid")
            args.append(template_version_id.strip())
            argi += 1

        q.append("order by occurred_at desc")
        q.append(f"limit ${argi}")
        args.append(limit)

        rows = await conn.fetch("\n".join(q), *args)

        out: List[ListEntriesResp] = []
        for r in rows:
            out.append(
                ListEntriesResp(
                    id=str(r["id"]),
                    owner_user_id=r["owner_user_id"],
                    subject_id=r["subject_id"],
                    template_version_id=str(r["template_version_id"]),
                    occurred_at=r["occurred_at"],
                    data=r["data"] or {},
                )
            )
        return out
    finally:
        await conn.close()
